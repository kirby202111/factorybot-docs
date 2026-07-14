"""B chunk 与 locator。

**chunk 不可变**（核心设计，绕开 ChromaDB 多记录翻转无事务弱点）：
- 写入 ChromaDB 后所有 metadata 字段永远不修改；
- 版本升版 = 追加新版本 chunk（带新 ``version``），不翻转老 chunk 的 state；
- 版本隔离靠查询 ``where={"state":"PUBLISHED","version_kind":..,"version":..}`` 过滤；
- 单条软删（文档撤回）允许 upsert 改 state=DEPRECATED（单条原子，ChromaDB 可接受），
  区别于"批量翻转"（不做）。

版本锚点通用化：``version_kind``/``version_ref_id``/``version`` 三字段替代原 route-specific
的 ``route_version``/``route_id``/``binding_asset_id``，覆盖工艺路线/设备资产/通用标准等所有版本类型。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.shared.events.version_contract import VersionAnchor, VersionKind


class ChunkLocator(BaseModel):
    """chunk 定位（值对象）。JSON 序列化入 ChromaDB metadata 的 ``locator`` 字段。"""

    page: int | None = None
    offset: int | None = None
    heading_path: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | dict | None) -> "ChunkLocator":
        if raw is None:
            return cls()
        if isinstance(raw, dict):
            return cls.model_validate(raw)
        return cls.model_validate(json.loads(raw))


class DocumentChunk(BaseModel):
    """文档 chunk（实体）。

    metadata 字段写入 ChromaDB 后不可变：``version_kind``/``version_ref_id``/``version``/
    ``state``/``tenant_scope``/``doc_id``/``doc_type``/``chunk_seq``/``locator``/``file_content_hash``。
    chunk_id = ``{doc_id}:{version_id}:{chunk_seq}``（幂等键，重建安全）。
    ``version_id`` 是文档自身版本；``version`` 是被绑定目标的版本（工艺路线/设备资产/标准）。
    """

    chunk_id: str
    version_id: str
    doc_id: str
    chunk_seq: int
    text: str
    embedding: list[float] = Field(default_factory=list)   # bge-m3 1024 维
    locator: ChunkLocator = Field(default_factory=ChunkLocator)
    section_type: str = "NOTE"                              # STEP | FAULT_CODE | PARAM | NOTE

    # 以下字段同步写入 ChromaDB metadata，写入后不可变
    version_kind: str = ""          # route|bom|rule|asset|standard|""
    version_ref_id: str = ""        # route_id / asset_id / standard_id / ...
    version: str = ""               # 锁定的版本号 v3 / RevH / ""
    state: str = "PUBLISHED"                                # 写入时固定 PUBLISHED
    tenant_scope: str = ""
    doc_type: str = ""
    file_content_hash: str = ""

    @property
    def version_anchor(self) -> VersionAnchor | None:
        """从三字段构造版本锚点；无 kind/version 返回 None。"""
        if not self.version_kind or not self.version:
            return None
        try:
            return VersionAnchor(kind=VersionKind(self.version_kind), ref_id=self.version_ref_id, version=self.version)
        except ValueError:
            return None

    def to_metadata_dict(self) -> dict[str, Any]:
        """ChromaDB metadata（不可变字段）。"""
        return {
            "doc_id": self.doc_id,
            "version_id": self.version_id,
            "doc_type": self.doc_type,
            "state": self.state,
            "version_kind": self.version_kind,
            "version_ref_id": self.version_ref_id,
            "version": self.version,
            "tenant_scope": self.tenant_scope,
            "chunk_seq": self.chunk_seq,
            "section_type": self.section_type,
            "locator": self.locator.to_json(),
            "file_content_hash": self.file_content_hash,
        }

    @classmethod
    def from_chroma(
        cls, *, chunk_id: str, document: str, embedding: list[float], metadata: dict[str, Any]
    ) -> "DocumentChunk":
        """从 ChromaDB 查询结果重建（检索返回用）。"""
        return cls(
            chunk_id=chunk_id,
            version_id=metadata.get("version_id", ""),
            doc_id=metadata.get("doc_id", ""),
            chunk_seq=int(metadata.get("chunk_seq", 0)),
            text=document,
            embedding=embedding,
            locator=ChunkLocator.from_json(metadata.get("locator")),
            section_type=metadata.get("section_type", "NOTE"),
            version_kind=metadata.get("version_kind") or "",
            version_ref_id=metadata.get("version_ref_id") or "",
            version=metadata.get("version") or "",
            state=metadata.get("state", "PUBLISHED"),
            tenant_scope=metadata.get("tenant_scope", ""),
            doc_type=metadata.get("doc_type", ""),
            file_content_hash=metadata.get("file_content_hash", ""),
        )
