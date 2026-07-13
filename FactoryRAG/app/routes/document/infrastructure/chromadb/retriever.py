"""ChromaDB 向量检索器（稠密路）。

构建 ``where`` pre-filter（强制带 state=PUBLISHED + route_version 等值 + tenant_scope + doc_type），
调用 ``collection.query`` 返回 ``list[ChunkHit]``。

过滤判据收敛到 ``ChunkFilter``，与 BM25 稀疏路共享同一真相源（见 ``infrastructure.chunk_filter``）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.routes.document.domain.answer import ChunkHit
from app.routes.document.infrastructure.chunk_filter import ChunkFilter
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class VectorRetriever:
    """B 向量检索器（ChromaDB，稠密路）。满足 ``RetrieverPort``。"""

    def __init__(self, collection: Any, embedder: Any) -> None:
        self._collection = collection
        self._embedder = embedder

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
        query_vec = await self._embedder.embed_one(query)
        where = ChunkFilter(
            tenant=tenant,
            route_version=route_version,
            asset_id=asset_id,
            doc_types=tuple(doc_types) if doc_types else (),
        ).to_where()

        started = time.perf_counter()
        result = self._collection.query(
            query_embeddings=[query_vec],
            n_results=top_k,
            where=where,
            include=["metadatas", "documents", "distances"],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.debug("ChromaDB 检索 latency=%dms where=%s", latency_ms, where)

        return self._map_hits(result)

    def _map_hits(self, result: dict[str, Any]) -> list[ChunkHit]:
        ids_batch = result.get("ids", [[]])
        docs_batch = result.get("documents", [[]])
        meta_batch = result.get("metadatas", [[]])
        dist_batch = result.get("distances", [[]])
        if not ids_batch:
            return []
        hits: list[ChunkHit] = []
        for cid, doc, meta, dist in zip(
            ids_batch[0], docs_batch[0], meta_batch[0], dist_batch[0]
        ):
            import json

            locator_raw = meta.get("locator", "{}")
            try:
                locator = json.loads(locator_raw) if isinstance(locator_raw, str) else locator_raw
            except Exception:
                locator = {}
            # cosine distance -> similarity（ChromaDB 返回 distance，越小越相似）
            score = 1.0 - float(dist) if dist is not None else 0.0
            hits.append(
                ChunkHit(
                    chunk_id=cid,
                    doc_id=meta.get("doc_id", ""),
                    version_id=meta.get("version_id", ""),
                    text=doc,
                    locator=locator,
                    section_type=meta.get("section_type", "NOTE"),
                    route_version=meta.get("route_version") or None,
                    state=meta.get("state", "PUBLISHED"),
                    score=score,
                )
            )
        return hits
