"""B 文档检索服务。

**强制带版本红线**（§1.2）：工艺绑定型（PROCESS_BOUND）需 ROUTE 版本锚点（``version``+
``version_kind="route"``）必填，入口校验拒绝缺失，**绝不退回"查最新 ACTIVE"**（避开在制品不切换
工艺语义陷阱：工单绑 v3，最新 ACTIVE 是 v4，退回查 v4 会答出不适用 SOP）。设备绑定型需 ASSET
锚点（``version_ref_id`` 必填，``version`` 可选），通用知识型不带版本。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.routes.document.domain.answer import (
    ChunkHit,
    DocAnswer,
    DocCitation,
    DocQuery,
    DocSearch,
)
from app.routes.document.domain.document import DocumentCategory
from app.routes.document.domain.retriever_port import RetrieverPort
from app.shared.events.version_contract import VersionKind
from app.shared.obs.port import ObservabilityPort
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class DocumentRetrievalService:
    """文档检索 application service。

    SRP：只管"检索 + rerank + 综合 + 缓存"；摄入/重索引分别在 IngestionService/ReindexCoordinator。
    """

    def __init__(
        self,
        *,
        retriever: RetrieverPort,
        reranker: Any,
        llm: Any,
        redis: Any,
        cache_ttl: int,
        obs: ObservabilityPort,
        top_k: int = 20,
        top_n: int = 5,
    ) -> None:
        self._retriever = retriever
        self._reranker = reranker
        self._llm = llm
        self._redis = redis
        self._cache_ttl = cache_ttl
        self._obs = obs
        self._top_k = top_k
        self._top_n = top_n

    # ── Port 契约 ──
    async def retrieve_and_synthesize(self, req: DocQuery, tenant: TenantContext) -> DocAnswer:
        return await self.query(req, tenant)

    async def search_chunks(self, req: DocSearch, tenant: TenantContext) -> list[ChunkHit]:
        hits = await self._retriever.retrieve(
            query=req.question,
            tenant=tenant,
            version_anchor=req.version_anchor(),
            doc_types=[dt.value for dt in req.doc_types] if req.doc_types else None,
            top_k=req.top_k,
        )
        return hits

    # ── 检索 + 综合 ──
    async def query(self, req: DocQuery, tenant: TenantContext) -> DocAnswer:
        # 0. 强制版本校验（安全红线）
        self._enforce_version_anchor(req)

        cache_key = self._cache_key(req, tenant)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        import time

        started = time.perf_counter()
        # 1. 向量检索（ChromaDB where pre-filter）
        hits = await self._retriever.retrieve(
            query=req.question,
            tenant=tenant,
            version_anchor=req.version_anchor(),
            doc_types=[dt.value for dt in req.doc_types] if req.doc_types else None,
            top_k=req.top_k,
        )
        # 1b. DEPRECATED 泄漏兜底（双保险：ChromaDB where 已过滤，这里再校验）
        hits = self._filter_deprecated_leak(hits)
        # 2. rerank（bge-reranker-v2-m3 cross-encoder）
        ranked = await self._rerank(req.question, hits, req.top_n)
        # 3. LLM 综合
        answer = await self._synthesize(req, ranked)
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            self._obs.record_retrieval(route="B", hits=len(ranked), latency_ms=latency_ms)
        except Exception:
            pass
        # 4. 置信度兜底：低置信或无引用 -> 转人工
        if answer.confidence < 0.6 or not answer.citations:
            answer.needs_human_review = True

        await self._cache_set(cache_key, answer)
        return answer

    def _enforce_version_anchor(self, req: DocQuery) -> None:
        """强制带版本红线（§1.2 红线 #1）。"""
        if req.doc_category == DocumentCategory.PROCESS_BOUND:
            if not req.version or req.version_kind != VersionKind.ROUTE.value:
                raise ValueError(
                    "工艺绑定型文档检索必须指定 ROUTE 版本锚点（version + version_kind='route'），"
                    "禁止退回'查最新 ACTIVE'（避开在制品不切换工艺语义陷阱）"
                )
        elif req.doc_category == DocumentCategory.ASSET_BOUND:
            if not req.version_ref_id or req.version_kind != VersionKind.ASSET.value:
                raise ValueError(
                    "设备绑定型文档检索必须指定 ASSET 版本锚点（version_kind='asset' + version_ref_id）"
                )

    def _filter_deprecated_leak(self, hits: list[ChunkHit]) -> list[ChunkHit]:
        leaked = [h for h in hits if h.state != "PUBLISHED"]
        if leaked:
            try:
                self._obs.metrics.deprecated_leak.inc(len(leaked))
            except Exception:
                pass
            logger.warning("DEPRECATED 泄漏 %d 条，已过滤", len(leaked))
        return [h for h in hits if h.state == "PUBLISHED"]

    async def _rerank(self, question: str, hits: list[ChunkHit], top_n: int) -> list[ChunkHit]:
        if not hits:
            return []
        ranked_idx = await self._reranker.rerank(
            query=question, docs=[h.text for h in hits], top_k=top_n
        )
        return [hits[idx] for idx, _ in ranked_idx]

    async def _synthesize(self, req: DocQuery, ranked: list[ChunkHit]) -> DocAnswer:
        if not ranked:
            return DocAnswer(
                answer="未检索到相关文档片段，建议转人工或确认查询条件。",
                confidence=0.0,
                version_filter=req.version,
                version_kind_filter=req.version_kind,
                needs_human_review=True,
            )
        context = "\n---\n".join(
            f"[{i+1}] {h.text}" for i, h in enumerate(ranked)
        )
        prompt = [
            {"role": "system", "content": "你是车间文档助手。仅依据给定的文档片段作答，不得编造。"},
            {"role": "user", "content": f"问题：{req.question}\n\n文档片段：\n{context}\n\n请作答并标注引用。"},
        ]
        try:
            result = await self._llm.achat(prompt)
            answer_text = result.content
            confidence = 0.75  # MVP：固定置信度；生产接 LLM 自评或忠实度评测
        except Exception as exc:
            logger.warning("LLM 综合失败，降级返回片段拼接: %s", exc)
            answer_text = "\n---\n".join(h.text for h in ranked)
            confidence = 0.4

        citations = [
            DocCitation(
                chunk_id=h.chunk_id,
                document_id=h.doc_id,
                version_no=h.version_id,
                title="",
                locator=h.locator,
                quoted_text=h.text[:120],
            )
            for h in ranked
        ]
        return DocAnswer(
            answer=answer_text,
            citations=citations,
            confidence=confidence,
            version_filter=req.version,
            version_kind_filter=req.version_kind,
        )

    # ── Redis 缓存 ──
    def _cache_key(self, req: DocQuery, tenant: TenantContext) -> str:
        raw = f"{tenant.tenant_id}|{req.doc_category}|{req.version_kind}|{req.version_ref_id}|{req.version}|{req.question}"
        return f"rag:doc:cache:{hashlib.sha256(raw.encode()).hexdigest()}"

    async def _cache_get(self, key: str) -> DocAnswer | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if raw:
                return DocAnswer.model_validate_json(raw)
        except Exception:
            pass
        return None

    async def _cache_set(self, key: str, answer: DocAnswer) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(key, self._cache_ttl, answer.model_dump_json())
        except Exception:
            pass
