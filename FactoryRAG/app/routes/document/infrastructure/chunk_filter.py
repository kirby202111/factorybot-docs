"""共享过滤规格：稠密路（ChromaDB ``where``）与稀疏路（内存谓词）的单一真相源。

混合召回的硬约束：两路过滤必须语义等价（state=PUBLISHED + route_version 等值 +
tenant_scope + doc_type + asset_id）。把判据收敛到 ``ChunkFilter`` 一个值对象，
``to_where()`` 给 ChromaDB pre-filter、``matches()`` 给 BM25 内存过滤，二者由同一组
字段推导，杜绝漂移与 DEPRECATED/版本/租户泄漏。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.routes.document.domain.chunk import DocumentChunk
from app.shared.tenant.context import TenantContext


@dataclass(frozen=True)
class ChunkFilter:
    """chunk 过滤规格（值对象）。

    所有字段均可空：空表示"不过滤该维度"（与 ChromaDB 不写该 where 键等价）。
    """

    tenant: TenantContext
    route_version: str | None = None
    asset_id: str | None = None
    doc_types: tuple[str, ...] = field(default_factory=tuple)

    def to_where(self) -> dict[str, Any]:
        """ChromaDB ``where`` pre-filter（与现 ``_build_where`` 等价）。

        ChromaDB 中 ``route_version``/``binding_asset_id`` 写入时已 ``or ""``，
        故等值过滤对 None 写成 "" 的记录天然不命中，与 ``matches`` 一致。
        """
        where: dict[str, Any] = {"state": "PUBLISHED"}
        if self.route_version:
            where["route_version"] = self.route_version
        if self.asset_id:
            where["binding_asset_id"] = self.asset_id
        scopes = self.tenant.chroma_scopes()
        if scopes:
            where["tenant_scope"] = {"$in": scopes}
        if self.doc_types:
            where["doc_type"] = {"$in": list(self.doc_types)}
        return where

    def matches(self, chunk: DocumentChunk) -> bool:
        """内存谓词（BM25 路用）。语义与 ``to_where`` 一一对应。"""
        if chunk.state != "PUBLISHED":
            return False
        if self.route_version and chunk.route_version != self.route_version:
            return False
        if self.asset_id and chunk.binding_asset_id != self.asset_id:
            return False
        scopes = self.tenant.chroma_scopes()
        if scopes and chunk.tenant_scope not in scopes:
            return False
        if self.doc_types and chunk.doc_type not in self.doc_types:
            return False
        return True
