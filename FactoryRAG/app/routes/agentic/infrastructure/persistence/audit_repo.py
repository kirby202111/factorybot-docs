"""E 答案审计 + 路由 trace 仓库。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.routes.agentic.infrastructure.persistence.models import (
    AnswerAuditModel,
    RouteTraceModel,
)

logger = logging.getLogger(__name__)


class AnswerAuditRepo:
    """答案审计仓库。"""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    async def record(self, request: Any, intent: Any, answer: Any, tenant: Any) -> str:
        audit_id = str(uuid4())
        async with self._sf() as session:
            session.add(
                AnswerAuditModel(
                    id=audit_id,
                    question=request.question,
                    intent=intent.value,
                    route_taken=answer.route_taken,
                    tool_chain=answer.tool_chain,
                    summary=answer.summary,
                    detail=answer.detail,
                    confidence=answer.confidence,
                    trace_id=answer.trace_id,
                    needs_human_review=1 if answer.needs_human_review else 0,
                    session_id=request.session_id or "",
                    tenant_id=tenant.tenant_id,
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
        return audit_id

    async def find_by_id(self, audit_id: str) -> dict | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AnswerAuditModel).where(AnswerAuditModel.id == audit_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            traces = (
                await session.execute(
                    select(RouteTraceModel).where(RouteTraceModel.audit_id == audit_id)
                )
            ).scalars().all()
            return {
                "audit_id": row.id,
                "question": row.question,
                "intent": row.intent,
                "route_taken": row.route_taken,
                "tool_chain": row.tool_chain,
                "summary": row.summary,
                "confidence": row.confidence,
                "trace_id": row.trace_id,
                "needs_human_review": bool(row.needs_human_review),
                "route_traces": [
                    {
                        "tool_name": t.tool_name,
                        "status": t.status,
                        "latency_ms": t.latency_ms,
                        "view_summary": t.view_summary,
                        "error_message": t.error_message,
                    }
                    for t in traces
                ],
            }


class RouteTraceRepo:
    """路由 trace 仓库（每次工具/委托调用记录一行）。"""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    async def _save(self, *, tool_name: str, status: str, latency_ms: int,
                    view_summary: str | None, error_message: str | None) -> None:
        async with self._sf() as session:
            session.add(
                RouteTraceModel(
                    audit_id="",  # 与 answer_audit 关联由 AnswerAuditRepo.record 后回填（MVP 简化）
                    tool_name=tool_name,
                    status=status,
                    latency_ms=latency_ms,
                    view_summary=view_summary,
                    error_message=error_message,
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

    async def save_ok(self, tool_name: str, view: Any, latency_ms: int) -> None:
        summary = str(view)[:500] if view is not None else None
        await self._save(tool_name=tool_name, status="ok", latency_ms=latency_ms,
                         view_summary=summary, error_message=None)

    async def save_error(self, tool_name: str, message: str, latency_ms: int) -> None:
        await self._save(tool_name=tool_name, status="error", latency_ms=latency_ms,
                         view_summary=None, error_message=message)

    async def save_denied(self, tool_name: str, reason: str) -> None:
        await self._save(tool_name=tool_name, status="denied", latency_ms=0,
                         view_summary=None, error_message=reason)
