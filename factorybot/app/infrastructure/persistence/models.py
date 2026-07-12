"""业务可观测表 + L3 表的 SQLAlchemy 模型（real 模式落 MySQL 用）。

mock 模式下不导入此模块（用进程内仓库）。表结构对齐可观测文档 §7.1 / 长程任务 §2.2：
tool_call_trace / llm_call_log / diagnosis_session / diagnosis_report / draft_trace /
node_trace / l3_session / gate_decision / model_pricing ...
"""
from __future__ import annotations

# SQLAlchemy 为可选依赖，real 模式才需要
try:
    from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
    from sqlalchemy.dialects.mysql import JSON, LONGBLOB
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    class Base(DeclarativeBase):
        pass

    class ToolCallTrace(Base):
        __tablename__ = "tool_call_trace"
        trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
        session_id: Mapped[str] = mapped_column(String(64), index=True)
        step_no: Mapped[int] = mapped_column(Integer)
        tool_name: Mapped[str] = mapped_column(String(128))
        bounded_context: Mapped[str] = mapped_column(String(128))
        input_payload = mapped_column(JSON)
        output_payload = mapped_column(JSON)
        status: Mapped[str] = mapped_column(String(16))
        latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
        tenant_id: Mapped[str] = mapped_column(String(64))
        occurred_at = mapped_column(DateTime)

    class LlmCallLog(Base):
        __tablename__ = "llm_call_log"
        call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
        session_id: Mapped[str] = mapped_column(String(64), index=True)
        step_no: Mapped[int] = mapped_column(Integer)
        model: Mapped[str] = mapped_column(String(128))
        prompt_version: Mapped[str] = mapped_column(String(64))
        prompt_token_count: Mapped[int] = mapped_column(Integer)
        completion_token_count: Mapped[int] = mapped_column(Integer)
        latency_ms: Mapped[int] = mapped_column(Integer)
        finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
        occurred_at = mapped_column(DateTime)

    class L3SessionRow(Base):
        __tablename__ = "l3_session"
        session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
        scenario: Mapped[str] = mapped_column(String(32))
        status: Mapped[str] = mapped_column(String(16))
        current_step: Mapped[str] = mapped_column(String(64), default="")
        suspend_reason = mapped_column(Text, default="")
        created_at = mapped_column(DateTime)
        updated_at = mapped_column(DateTime)

    _HAS_SQLALCHEMY = True
except ImportError:  # pragma: no cover
    _HAS_SQLALCHEMY = False
    Base = None  # type: ignore
