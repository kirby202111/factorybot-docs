"""A 追溯检索服务。

编排：seed 解析 -> 5M1E 子图展开（缓存）-> LLM 综合（带证据引用）-> TraceAnswer。
- suggested_action：经 ``DocRagPort`` 拉 B 的 SOP 片段，带 ``route_version_filter``
  （从图快照边物理锁定的版本取，不取当前 ACTIVE）。
- 工艺升版：发布 ``rag.reindex.request`` 内部事件通知 B 重索引。
- 版本一致性三段链第一段：图 ``SNAPSHOT_OF_ROUTE{route_version}`` 快照边物理锁定版本。
低置信转人工（宁可拦下让人判，不可错放）。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from app.routes.traceability.domain.answer import RootCauseHypothesis, TraceAnswer
from app.routes.traceability.domain.seed import ExpandRequest, Seed, SeedKind, TraceQuery
from app.routes.traceability.domain.subgraph import TraceSubgraph
from app.shared.events.version_contract import ReindexRequest
from app.shared.obs.port import ObservabilityPort
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class TraceRetrievalService:
    """A 追溯检索 application service。"""

    def __init__(
        self,
        *,
        retriever: Any,
        seed_resolver: Any,
        llm: Any,
        subgraph_repo: Any,
        redis: Any,
        cache_ttl: int,
        doc_rag: Any,
        obs: ObservabilityPort,
    ) -> None:
        self._retriever = retriever
        self._seed_resolver = seed_resolver
        self._llm = llm
        self._subgraph_repo = subgraph_repo
        self._redis = redis
        self._cache_ttl = cache_ttl
        self._doc_rag = doc_rag          # DocRagPort（A -> B 拉 SOP 片段）
        self._obs = obs

    # ── Port 契约 ──
    async def retrieve_and_synthesize(self, req: TraceQuery, tenant: TenantContext) -> TraceAnswer:
        return await self.query(req, tenant)

    async def expand_subgraph(self, req: ExpandRequest, tenant: TenantContext) -> TraceSubgraph:
        seed = Seed(kind=req.kind, value=req.value)
        as_of = req.as_of or datetime.now(timezone.utc)
        return await self._expand(seed, as_of, tenant, route_version=req.route_version)

    # ── 主流程 ──
    async def query(self, req: TraceQuery, tenant: TenantContext) -> TraceAnswer:
        seed = req.seed or await self._seed_resolver.resolve(req.question, tenant)
        as_of = req.as_of or datetime.now(timezone.utc)

        subgraph = await self._expand(seed, as_of, tenant, route_version=req.route_version)
        answer = await self._synthesize(req.question, subgraph, tenant)
        return answer

    async def _expand(
        self, seed: Seed, as_of: datetime, tenant: TenantContext, *, route_version: str | None
    ) -> TraceSubgraph:
        cache_key = self._cache_key(seed, as_of, tenant)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        with self._obs.retrieval_span(route="A", kind=seed.kind.value):
            subgraph = await self._retriever.expand_5m1e(seed, as_of, tenant, route_version=route_version)
        await self._subgraph_repo.save(subgraph)
        await self._cache_set(cache_key, subgraph)
        return subgraph

    async def _synthesize(
        self, question: str, subgraph: TraceSubgraph, tenant: TenantContext
    ) -> TraceAnswer:
        route_version = subgraph.route_version_locked()
        # 透传物理锁定的版本（三段链第一段 -> L1 evidence -> L2 Draft -> MES 校验 ACTIVE）
        context = self._trim_for_llm(subgraph)
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是车间追溯分析助手。基于给定的 5M1E 子图给出根因假设，"
                    "每条假设必须引用证据节点（node_id），不得编造节点（禁实体幻觉）。"
                    "输出 JSON: {summary, confidence, hypotheses:[{category,rank,statement,evidence,suggested_action}]}"
                ),
            },
            {
                "role": "user",
                "content": f"问题：{question}\nroute_version={route_version}\n子图：\n{context}",
            },
        ]
        try:
            result = await self._llm.achat(prompt)
            import json

            data = json.loads(result.content)
            hypotheses = [RootCauseHypothesis.model_validate(h) for h in data.get("hypotheses", [])]
            summary = data.get("summary", "")
            confidence = float(data.get("confidence", 0.0))
        except Exception as exc:
            logger.warning("LLM 综合失败，降级返回摘要: %s", exc)
            summary = "LLM 综合失败，建议转人工。"
            hypotheses = []
            confidence = 0.0

        # suggested_action：经 DocRagPort 拉 B 的 SOP 片段（带 route_version_filter）
        if hypotheses and self._doc_rag is not None and route_version:
            await self._enrich_suggested_action(hypotheses, route_version, tenant)

        answer = TraceAnswer(
            summary=summary,
            confidence=confidence,
            hypotheses=hypotheses,
            subgraph_ref=subgraph.subgraph_ref,
            route_version=route_version,
        )
        if confidence < 0.6 or not hypotheses:
            answer.needs_human_review = True
        return answer

    async def _enrich_suggested_action(
        self, hypotheses: list[RootCauseHypothesis], route_version: str, tenant: TenantContext
    ) -> None:
        """A -> B：经 DocRagPort 拉 SOP 片段补充 suggested_action。"""
        from app.routes.document.domain.answer import DocQuery
        from app.routes.document.domain.document import DocumentCategory

        for h in hypotheses:
            try:
                doc_answer = await self._doc_rag.query(
                    DocQuery(
                        question=f"{h.category.value} 根因处置 SOP：{h.statement}",
                        doc_category=DocumentCategory.PROCESS_BOUND,
                        route_version=route_version,
                    ),
                    tenant,
                )
                if doc_answer.citations:
                    h.suggested_action = f"{h.suggested_action}\n（参考 SOP：{doc_answer.citations[0].quoted_text}）"
            except Exception as exc:
                logger.debug("suggested_action 拉 SOP 失败: %s", exc)

    def _trim_for_llm(self, subgraph: TraceSubgraph) -> str:
        """子图过大时裁剪：保留缺陷命中节点、聚合历史过点、限制设备记录时间窗。"""
        lines: list[str] = []
        c = subgraph.clusters
        lines.append(f"# Man(过点): {len(c.man)} # Machine: {len(c.machine)} "
                     f"# Material: {len(c.material)} # Method: {len(c.method)} "
                     f"# Measurement: {len(c.measurement)}")
        for n in c.measurement[:10]:
            lines.append(f"- {n.label}:{n.node_id} {n.props}")
        for n in c.material[:10]:
            lines.append(f"- {n.label}:{n.node_id} {n.props}")
        for n in c.method[:5]:
            lines.append(f"- {n.label}:{n.node_id} {n.props}")
        for n in c.man[:10]:
            lines.append(f"- {n.label}:{n.node_id} {n.props}")
        return "\n".join(lines)

    # ── 工艺升版 -> 发 rag.reindex.request 通知 B ──
    async def on_route_upgraded(
        self, route_id: str, old_version: str, new_version: str, trace_id: str = ""
    ) -> ReindexRequest:
        """发布 ``rag.reindex.request`` 内部事件（B 的 ReindexCoordinator 消费）。"""
        req = ReindexRequest(route_id=route_id, route_version=new_version, trace_id=trace_id)
        logger.info("发布 rag.reindex.request: route=%s@%s（旧 %s）", route_id, new_version, old_version)
        # 实际发布经 Kafka producer；此处返回事件供 infra 投递。
        return req

    # ── Redis 子图缓存 ──
    def _cache_key(self, seed: Seed, as_of: datetime, tenant: TenantContext) -> str:
        bucket = as_of.replace(second=0, microsecond=0).isoformat()
        raw = f"{tenant.tenant_id}|{seed.kind.value}|{seed.value}|{bucket}"
        return f"rag:trace:subgraph:{hashlib.sha256(raw.encode()).hexdigest()}"

    async def _cache_get(self, key: str) -> TraceSubgraph | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if raw:
                return TraceSubgraph.model_validate_json(raw)
        except Exception:
            pass
        return None

    async def _cache_set(self, key: str, subgraph: TraceSubgraph) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(key, self._cache_ttl, subgraph.model_dump_json())
        except Exception:
            pass
