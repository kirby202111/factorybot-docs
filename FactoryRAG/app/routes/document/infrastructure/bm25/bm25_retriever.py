"""BM25 稀疏检索器（关键词精确匹配，专有名词友好）。

满足 ``RetrieverPort``：复用 ``ChunkFilter`` 内存谓词（与稠密路等价）过滤候选，
``Bm25Index.search`` 打分，映射 ``ChunkHit``。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.routes.document.domain.answer import ChunkHit
from app.routes.document.infrastructure.bm25.bm25_index import Bm25Index
from app.routes.document.infrastructure.chunk_filter import ChunkFilter
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class Bm25Retriever:
    """B 稀疏检索器（BM25）。满足 ``RetrieverPort``。"""

    def __init__(self, index: Bm25Index) -> None:
        self._index = index

    async def retrieve(
        self,
        *,
        query: str,
        tenant: TenantContext,
        route_version: str | None = None,
        asset_id: str | None = None,
        doc_types: list[str] | None = None,
        top_k: int = 20,
    ) -> list[ChunkHit]:
        chunk_filter = ChunkFilter(
            tenant=tenant,
            route_version=route_version,
            asset_id=asset_id,
            doc_types=tuple(doc_types) if doc_types else (),
        )
        started = time.perf_counter()
        results = await self._index.search(
            query,
            predicate=chunk_filter.matches,
            top_k=top_k,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.debug("BM25 检索 latency=%dms hits=%d", latency_ms, len(results))
        return [self._to_hit(chunk, score) for chunk, score in results]

    @staticmethod
    def _to_hit(chunk: Any, score: float) -> ChunkHit:
        return ChunkHit(
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            version_id=chunk.version_id,
            text=chunk.text,
            locator=chunk.locator.model_dump(mode="json"),
            section_type=chunk.section_type,
            route_version=chunk.route_version,
            state=chunk.state,
            score=float(score),
        )
