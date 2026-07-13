"""ChromaDB 向量检索器。

构建 ``where`` pre-filter（强制带 state=PUBLISHED + route_version 等值 + tenant_scope + doc_type），
调用 ``collection.query`` 返回 ``list[ChunkHit]``。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.routes.document.domain.answer import ChunkHit
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class VectorRetriever:
    """B 向量检索器（ChromaDB）。"""

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
        where = self._build_where(tenant, route_version, asset_id, doc_types)

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

    def _build_where(
        self,
        tenant: TenantContext,
        route_version: str | None,
        asset_id: str | None,
        doc_types: list[str] | None,
    ) -> dict[str, Any]:
        """``where`` pre-filter（强制带 state=PUBLISHED + 版本等值 + tenant_scope）。

        版本过滤退化成单字段等值（ChromaDB ``where`` 能做且 pre-filter）。
        """
        where: dict[str, Any] = {"state": "PUBLISHED"}
        if route_version:
            where["route_version"] = route_version
        if asset_id:
            where["binding_asset_id"] = asset_id
        scopes = tenant.chroma_scopes()
        if scopes:
            where["tenant_scope"] = {"$in": scopes}
        if doc_types:
            where["doc_type"] = {"$in": doc_types}
        return where

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
