"""RAG 服务 ACL：文档型检索（SOP/手册/8D 历史）-- L2 草拟时检索历史同类。"""
from __future__ import annotations

from typing import Optional

from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor
from app.infrastructure.acl.base import BaseAclClient


class DocRagAclClient(BaseAclClient):
    """RAG 服务·文档型（只读）。"""

    async def search_docs(
        self, query: str, tenant: Optional[TenantContext] = None,
        version_anchor: Optional[VersionAnchor] = None,
    ) -> list[dict]:
        """GET /rag/docs/search -- 返回 DocSearchHit 形状列表。

        version_anchor 确保检索到的 SOP/工艺/手册文档与版本一致（route/bom/rule/asset/standard）。
        """
        params: dict = {"query": query}
        if version_anchor is not None:
            params["version"] = version_anchor.version
            params["version_kind"] = version_anchor.kind.value
            params["version_ref_id"] = version_anchor.ref_id
        dto = await self._get(
            "/rag/docs/search",
            tenant=tenant,
            params=params,
            fixture_rel="rag/docs", fixture_key="_default",
        )
        # fixture 可能是 {documents: [...]} 或 list
        if isinstance(dto, dict):
            docs = dto.get("documents", dto.get("results", []))
        else:
            docs = dto or []
        # 版本过滤（mock 下也按锚点三字段过滤）
        if version_anchor is not None and version_anchor.is_bound:
            docs = [
                d for d in docs
                if not d.get("version") or (
                    d.get("version") == version_anchor.version
                    and (not d.get("version_kind")
                         or d.get("version_kind") == version_anchor.kind.value)
                )
            ]
        return docs
