"""RAG 服务 ACL：文档型检索（SOP/手册/8D 历史）-- 草稿 草拟时检索历史同类。"""
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
        """POST /rag/docs/search -- 返回 ChunkHit 形状列表。

        版本一致性由服务端按 version/version_kind/version_ref_id 锚点过滤
        （FactoryRAG DocumentRetrievalService 已实现），ACL 侧不再重复过滤。
        body 字段对齐 FactoryRAG DocSearch（question + 版本锚点三字段）。
        """
        body: dict = {"question": query}
        if version_anchor is not None:
            body["version"] = version_anchor.version
            body["version_kind"] = version_anchor.kind.value
            body["version_ref_id"] = version_anchor.ref_id
        dto = await self._post_read(
            "/rag/docs/search", body, tenant=tenant,
            fixture_rel="rag/docs", fixture_key="_default",
        )
        # fixture 可能是 {documents: [...]} / {results: [...]} / list；real 返回 list[ChunkHit]
        if isinstance(dto, dict):
            return dto.get("documents", dto.get("results", []))
        return dto or []
