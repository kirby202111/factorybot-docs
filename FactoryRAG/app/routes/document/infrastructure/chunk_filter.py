"""共享过滤规格：稠密路（ChromaDB ``where``）与稀疏路（内存谓词）的单一真相源。

混合召回的硬约束：两路过滤必须语义等价（state=PUBLISHED + 版本锚点等值 + tenant_scope +
doc_type）。把判据收敛到 ``ChunkFilter`` 一个值对象，``to_where()`` 给 ChromaDB pre-filter、
``matches()`` 给 BM25 内存过滤，二者由同一 ``VersionAnchor`` 推导，杜绝漂移与
DEPRECATED/版本/租户泄漏。

版本锚点统一为 ``VersionAnchor(kind, ref_id, version)``，覆盖 route/bom/rule/asset/standard。
锚点各分量独立过滤：``version_kind`` 总写（锚点非空时），``version``/``version_ref_id`` 非空才写
-- 兼容「工艺路线仅按 version」「设备资产按 ref_id±version」两种用法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.routes.document.domain.chunk import DocumentChunk
from app.shared.events.version_contract import VersionAnchor
from app.shared.tenant.context import TenantContext


@dataclass(frozen=True)
class ChunkFilter:
    """chunk 过滤规格（值对象）。

    ``version_anchor`` 为 None 表示不带版本过滤（与 ChromaDB 不写该 where 键等价）。
    """

    tenant: TenantContext
    version_anchor: VersionAnchor | None = None
    doc_types: tuple[str, ...] = field(default_factory=tuple)

    def to_where(self) -> dict[str, Any]:
        """ChromaDB ``where`` pre-filter。

        ChromaDB 中 ``version_kind``/``version_ref_id``/``version`` 写入时空值存 ""，
        故等值过滤对 "" 写的记录天然不命中，与 ``matches`` 一致。
        """
        where: dict[str, Any] = {"state": "PUBLISHED"}
        if self.version_anchor is not None:
            a = self.version_anchor
            where["version_kind"] = a.kind.value
            if a.version:
                where["version"] = a.version
            if a.ref_id:
                where["version_ref_id"] = a.ref_id
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
        if self.version_anchor is not None:
            a = self.version_anchor
            if chunk.version_kind != a.kind.value:
                return False
            if a.version and chunk.version != a.version:
                return False
            if a.ref_id and chunk.version_ref_id != a.ref_id:
                return False
        scopes = self.tenant.chroma_scopes()
        if scopes and chunk.tenant_scope not in scopes:
            return False
        if self.doc_types and chunk.doc_type not in self.doc_types:
            return False
        return True
