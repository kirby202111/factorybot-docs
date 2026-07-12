"""RAG 服务 ACL：文档型检索（SOP/手册/8D 历史）-- L2 草拟时检索历史同类。"""
from __future__ import annotations

from typing import Optional

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient


class DocRagAclClient(BaseAclClient):
    """RAG 服务·文档型（只读）。"""

    async def search_docs(
        self, query: str, tenant: Optional[TenantContext] = None,
        route_version_filter: Optional[str] = None,
    ) -> list[dict]:
        """GET /rag/docs/search -- 返回 DocSearchHit 形状列表。

        route_version_filter 确保检索到的 SOP/工艺文档与版本一致。
        """
        dto = await self._get(
            "/rag/docs/search",
            tenant=tenant,
            params={"query": query, "route_version": route_version_filter},
            fixture_rel="rag/docs", fixture_key="_default",
        )
        # fixture 可能是 {documents: [...]} 或 list
        if isinstance(dto, dict):
            docs = dto.get("documents", dto.get("results", []))
        else:
            docs = dto
        # 版本过滤（mock 下也可按 route_version 过滤）
        if route_version_filter:
            docs = [
                d for d in docs
                if not d.get("route_version") or d.get("route_version") == route_version_filter
            ]
        return docs
