"""草稿：写意图草稿（返工单/8D/SOP），不落库，人确认后下达。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.domain.version import VersionAnchor


class DraftKind(str, Enum):
    REWORK_ORDER = "REWORK_ORDER"
    EIGHT_D = "EIGHT_D"
    SOP = "SOP"


class Draft(BaseModel):
    """草稿产出。requires_confirmation 恒 True（草稿 不落库）。"""

    draft_id: str = ""
    draft_kind: DraftKind
    intent: str                               # 一句话：要做什么、再入点在哪
    payload: dict = Field(default_factory=dict)   # 草稿结构化内容
    evidence_refs: list[str] = Field(default_factory=list)  # subgraph_ref + trace_id
    # 版本一致性三段链第三段：物理锁定的版本锚点，透传自 诊断（扁平三字段 + version_anchor() 属性）
    version: str | None = None
    version_kind: str | None = None           # route|bom|rule|asset|standard
    version_ref_id: str | None = None         # route_id / asset_id / standard_id ...
    confidence: float = 0.0
    requires_confirmation: bool = True        # 草稿 不变式：恒 True
    needs_review: bool = False                # confidence < 0.5
    disclaimer: str = "本草稿为辅助草拟，下达需工程师在正式界面确认"

    def version_anchor(self) -> VersionAnchor | None:
        """构造版本锚点；无 version_kind/version 返回 None。"""
        return VersionAnchor.from_flat(self.version, self.version_kind, self.version_ref_id)
