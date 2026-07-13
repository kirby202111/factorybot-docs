"""B chunk 与 locator。

**chunk 不可变**（核心设计，绕开 ChromaDB 多记录翻转无事务弱点）：
- 写入 ChromaDB 后所有 metadata 字段永远不修改；
- 工艺升版 = 追加新版本 chunk（带新 route_version），不翻转老 chunk 的 state；
- 版本隔离靠查询 ``where={"state":"PUBLISHED","route_version":rv}`` 过滤；
- 单条软删（文档撤回）允许 upsert 改 state=DEPRECATED（单条原子，ChromaDB 可接受），
  区别于"批量翻转"（不做）。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


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

    metadata 字段写入 ChromaDB 后不可变：``route_version``/``state``/``tenant_scope``/
    ``doc_id``/``doc_type``/``chunk_seq``/``locator``/``file_content_hash``。
    chunk_id = ``{doc_id}:{version_id}:{chunk_seq}``（幂等键，重建安全）。
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
    route_version: str | None = None
    route_id: str | None = None
    state: str = "PUBLISHED"                                # 写入时固定 PUBLISHED
    tenant_scope: str = ""
    doc_type: str = ""
    binding_asset_id: str | None = None
    file_content_hash: str = ""

    def to_metadata_dict(self) -> dict[str, Any]:
        """ChromaDB metadata（不可变字段）。"""
        return {
            "doc_id": self.doc_id,
            "version_id": self.version_id,
            "doc_type": self.doc_type,
            "state": self.state,
            "route_version": self.route_version or "",
            "route_id": self.route_id or "",
            "tenant_scope": self.tenant_scope,
            "binding_asset_id": self.binding_asset_id or "",
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
            route_version=metadata.get("route_version") or None,
            route_id=metadata.get("route_id") or None,
            state=metadata.get("state", "PUBLISHED"),
            tenant_scope=metadata.get("tenant_scope", ""),
            doc_type=metadata.get("doc_type", ""),
            binding_asset_id=metadata.get("binding_asset_id") or None,
            file_content_hash=metadata.get("file_content_hash", ""),
        )
