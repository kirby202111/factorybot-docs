"""草稿 证据视图：RAG 子图节点的防腐层视图。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain.version import VersionAnchor


class TraceNodeView(BaseModel):
    """追溯子图节点（草稿 按 subgraph_ref 回查，不重查图）。

    对应 Neo4j 节点：CheckpointRecord / WorkOrder / WipUnit / InventoryBatch /
    Asset / RouteVersion / TestResult / QualityVerdict ...
    """

    label: str                       # 节点标签，如 "CheckpointRecord"
    bounded_context: str             # 归属限界上下文
    node_id: str
    props: dict[str, Any] = Field(default_factory=dict)
    source_event_id: str = ""


class DocSearchHit(BaseModel):
    """文档型 RAG 检索单条（SOP/手册/8D 历史）。"""

    doc_id: str
    doc_type: str                    # SOP | MANUAL | EIGHT_D
    title: str
    content_snippet: str = ""
    # 版本锚点扁平三字段（与 RAG ChunkHit 对齐）+ version_anchor() 属性
    version: str | None = None
    version_kind: str | None = None
    version_ref_id: str | None = None
    score: float = 0.0

    def version_anchor(self) -> VersionAnchor | None:
        return VersionAnchor.from_flat(self.version, self.version_kind, self.version_ref_id)
