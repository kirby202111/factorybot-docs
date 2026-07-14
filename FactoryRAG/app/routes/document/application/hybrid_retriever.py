"""混合检索器（粗排召回）：BM25 稀疏 + Embedding 稠密 + RRF 融合。

满足 ``RetrieverPort``。两路并发召回各 ``candidate_k`` 条（过取），用 Reciprocal Rank
Fusion 融合排名（避开 BM25 与 cosine 分数不可比的问题），截断到 ``top_k`` 交给精排。

降级：任一路异常/空 -> 退回另一路排名（RRF 单路退化为该路 rank 分），不整体失败。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from app.routes.document.domain.answer import ChunkHit
from app.routes.document.domain.retriever_port import RetrieverPort
from app.shared.events.version_contract import VersionAnchor
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class HybridRetriever:
    """B 混合检索器（RRF 融合）。满足 ``RetrieverPort``。"""

    def __init__(
        self,
        *,
        dense: RetrieverPort,
        sparse: RetrieverPort,
        rrf_k: int = 60,
        dense_weight: float = 1.0,
        bm25_weight: float = 1.0,
        candidate_k: int = 50,
    ) -> None:
        self._dense = dense
        self._sparse = sparse
        self._rrf_k = rrf_k
        self._dense_weight = dense_weight
        self._bm25_weight = bm25_weight
        self._candidate_k = candidate_k

    async def retrieve(
        self,
        *,
        query: str,
        tenant: TenantContext,
        version_anchor: VersionAnchor | None = None,
        doc_types: list[str] | None = None,
        top_k: int = 20,
    ) -> list[ChunkHit]:
        fetch_k = max(top_k, self._candidate_k)
        dense_hits, sparse_hits = await self._gather(
            query=query,
            tenant=tenant,
            version_anchor=version_anchor,
            doc_types=doc_types,
            fetch_k=fetch_k,
        )
        fused = self._rrf(dense_hits, sparse_hits)
        return fused[:top_k]

    async def _gather(
        self,
        *,
        query: str,
        tenant: TenantContext,
        version_anchor: VersionAnchor | None,
        doc_types: list[str] | None,
        fetch_k: int,
    ) -> tuple[list[ChunkHit], list[ChunkHit]]:
        """两路并发召回；任一路异常降级为空，不影响另一路。"""
        dense_hits, sparse_hits = await asyncio.gather(
            self._safe(self._dense.retrieve(
                query=query, tenant=tenant, version_anchor=version_anchor,
                doc_types=doc_types, top_k=fetch_k,
            )),
            self._safe(self._sparse.retrieve(
                query=query, tenant=tenant, version_anchor=version_anchor,
                doc_types=doc_types, top_k=fetch_k,
            )),
        )
        return dense_hits, sparse_hits

    @staticmethod
    async def _safe(coro: "asyncio.Future[list[ChunkHit]]") -> list[ChunkHit]:
        try:
            return await coro
        except Exception as exc:
            logger.warning("混合召回某路失败，降级为空: %s", exc)
            return []

    def _rrf(
        self, dense_hits: list[ChunkHit], sparse_hits: list[ChunkHit]
    ) -> list[ChunkHit]:
        """Reciprocal Rank Fusion：``score(d) = Σ w_i / (rrf_k + rank_i(d))``。

        仅用排名（1-indexed），单路命中的 chunk 另一路不计该项。返回按融合分降序的
        ``ChunkHit`` 列表，``score`` 字段更新为 RRF 融合分。
        """
        scores: dict[str, float] = defaultdict(float)
        carrier: dict[str, ChunkHit] = {}

        for rank, hit in enumerate(dense_hits, start=1):
            scores[hit.chunk_id] += self._dense_weight / (self._rrf_k + rank)
            carrier[hit.chunk_id] = hit
        for rank, hit in enumerate(sparse_hits, start=1):
            scores[hit.chunk_id] += self._bm25_weight / (self._rrf_k + rank)
            carrier.setdefault(hit.chunk_id, hit)

        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [
            carrier[cid].model_copy(update={"score": score}) for cid, score in ordered
        ]
