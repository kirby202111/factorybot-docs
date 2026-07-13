"""A 检索应答模型。

LLM 综合返回 ``TraceAnswer``，含 5M1E 假设（必须带证据引用）+ ``subgraph_ref``。
低置信转人工（与 MES 防错理念一致：宁可拦下让人判，不可错放）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.routes.traceability.domain.subgraph import FiveM1ECategory


class RootCauseHypothesis(BaseModel):
    """LLM 生成的 5M1E 假设（必须带证据引用，禁实体幻觉）。"""

    category: FiveM1ECategory          # 模型必须从枚举值中选
    rank: int
    statement: str
    evidence: list[str] = Field(default_factory=list)   # ["node_id=CheckpointRecord:xxx", "defect_code=SW-001"]
    suggested_action: str


class TraceAnswer(BaseModel):
    """``POST /rag/trace/query`` 应答。"""

    summary: str
    confidence: float = 0.0             # 0.0 ~ 1.0
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)
    subgraph_ref: str                   # 指向持久化的 TraceSubgraph（seed.node_id + as_of）
    route_version: str | None = None    # 物理锁定的版本（从快照边透传，供 L1/L2/MES 三段链）
    disclaimer: str = "本答案为追溯型 RAG 的辅助假设，最终处置需工程师确认"
    needs_human_review: bool = False
