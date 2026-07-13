"""E 应答模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AnswerSource(BaseModel):
    """答案来源（节点/SOP/L1 假设/L2 草稿）。"""

    source_type: str          # "trace_node" | "sop_doc" | "l1_hypothesis" | "l2_draft"
    ref: str                  # "node_id=CheckpointRecord:xxx" | "SOP:WELD-014@v3" | "audit_id=..."
    route: str                # "A" | "B" | "L1" | "L2"


class AgentAnswer(BaseModel):
    """``POST /agent/chat`` 应答。"""

    question: str
    intent: str                                  # IntentCategory value
    route_taken: str                             # "A" | "B" | "L1" | "L2" | "A+B" | "HUMAN"
    summary: str
    detail: dict = Field(default_factory=dict)   # 路线相关的结构化详情
    sources: list[AnswerSource] = Field(default_factory=list)
    confidence: float = 0.0
    tool_chain: list[str] = Field(default_factory=list)
    trace_id: str = ""
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"


class ChatRequest(BaseModel):
    """``POST /agent/chat`` 请求。"""

    question: str
    session_id: str | None = None                # 会话连续性，映射 LangGraph thread_id


class AnswerAuditView(BaseModel):
    """``GET /agent/explain/{audit_id}`` 应答。"""

    audit_id: str
    question: str
    intent: str
    route_taken: str
    tool_chain: list[str]
    summary: str
    confidence: float
    trace_id: str
    needs_human_review: bool
    route_traces: list[dict] = Field(default_factory=list)
