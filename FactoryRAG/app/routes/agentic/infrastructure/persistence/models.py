"""E 答案审计 + 路由 trace 表（rag_agentic schema）。

工程师 UI 证据链回溯：``/agent/explain/{audit_id}`` 从 ``answer_audit`` +
``route_trace`` 重建路由决策与工具链。同源 ``trace_id`` 串联 agent-service 与 MES。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.persistence.base import Base


class AnswerAuditModel(Base):
    """答案审计表（rag_agentic）。``id`` = ``audit_id``。"""

    __tablename__ = "answer_audit"
    __table_args__ = {"schema": "rag_agentic"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    route_taken: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_chain: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    needs_human_review: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RouteTraceModel(Base):
    """路由 trace 表（rag_agentic）。每个工具/委托调用一行。"""

    __tablename__ = "route_trace"
    __table_args__ = {"schema": "rag_agentic"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)       # ok | denied | error | timeout
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    view_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    traceparent: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
