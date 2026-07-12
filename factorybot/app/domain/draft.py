"""L2 草稿：写意图草稿（返工单/8D/SOP），不落库，人确认后下达。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DraftKind(str, Enum):
    REWORK_ORDER = "REWORK_ORDER"
    EIGHT_D = "EIGHT_D"
    SOP = "SOP"


class Draft(BaseModel):
    """L2 草稿产出。requires_confirmation 恒 True（L2 不落库）。"""

    draft_id: str = ""
    draft_kind: DraftKind
    intent: str                               # 一句话：要做什么、再入点在哪
    payload: dict = Field(default_factory=dict)   # 草稿结构化内容
    evidence_refs: list[str] = Field(default_factory=list)  # subgraph_ref + trace_id
    route_version: str | None = None          # 版本一致性三段链的第三段，透传自 L1
    confidence: float = 0.0
    requires_confirmation: bool = True        # L2 不变式：恒 True
    needs_review: bool = False                # confidence < 0.5
    disclaimer: str = "本草稿为辅助草拟，下达需工程师在正式界面确认"
