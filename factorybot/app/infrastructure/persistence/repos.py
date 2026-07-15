"""进程内仓库（mock 模式默认）。

real 模式下可替换为 SQLAlchemy 实现，接口保持一致。仓库承载证据链平铺表
（tool_call_trace 等），给工程师 UI 回溯用。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ToolCallTrace:
    trace_id: str
    session_id: str
    step_no: int
    tool_name: str
    bounded_context: str
    input_payload: dict
    output_payload: dict            # FULL view（证据链全文，非压缩）
    status: str                     # OK | DENIED | ERROR
    latency_ms: int = 0
    tenant_id: str = ""
    occurred_at: datetime = field(default_factory=datetime.now)


class ToolCallTraceRepo:
    def __init__(self) -> None:
        self._rows: list[ToolCallTrace] = []

    async def save_ok(self, *, tool_name, bounded_context, args, view, latency_ms,
                      session_id, step_no, tenant_id, trace_id=None) -> str:
        tid = trace_id or f"T-{uuid.uuid4().hex[:8]}"
        self._rows.append(ToolCallTrace(
            trace_id=tid, session_id=session_id, step_no=step_no,
            tool_name=tool_name, bounded_context=bounded_context,
            input_payload=args, output_payload=view, status="OK",
            latency_ms=latency_ms, tenant_id=tenant_id,
        ))
        return tid

    async def save_denied(self, *, tool_name, session_id, step_no, tenant_id) -> str:
        tid = f"T-{uuid.uuid4().hex[:8]}"
        self._rows.append(ToolCallTrace(
            trace_id=tid, session_id=session_id, step_no=step_no,
            tool_name=tool_name, bounded_context="", input_payload={},
            output_payload={}, status="DENIED", tenant_id=tenant_id,
        ))
        return tid

    async def save_error(self, *, tool_name, session_id, step_no, tenant_id, error) -> str:
        tid = f"T-{uuid.uuid4().hex[:8]}"
        self._rows.append(ToolCallTrace(
            trace_id=tid, session_id=session_id, step_no=step_no,
            tool_name=tool_name, bounded_context="", input_payload={},
            output_payload={"error": str(error)}, status="ERROR", tenant_id=tenant_id,
        ))
        return tid

    async def list_for_session(self, session_id: str) -> list[ToolCallTrace]:
        return [r for r in self._rows if r.session_id == session_id]


class DraftRepo:
    """草稿归档（draft_trace），草稿 只落草稿不落库。

    归档时记录草稿归属租户（draft_id -> tenant_id），供 get_evidence 做多租户归属校验。
    """
    def __init__(self) -> None:
        self._drafts: dict[str, Any] = {}
        self._tenant_owners: dict[str, str] = {}   # draft_id -> tenant_id

    async def archive(self, draft, tenant_id: str) -> str:
        if not draft.draft_id:
            draft.draft_id = f"D-{uuid.uuid4().hex[:8]}"
        self._drafts[draft.draft_id] = draft
        self._tenant_owners[draft.draft_id] = tenant_id
        return draft.draft_id

    async def get(self, draft_id: str):
        return self._drafts.get(draft_id)

    async def owner_tenant_id(self, draft_id: str) -> str | None:
        """草稿归属租户；不存在返回 None（供 service 区分不存在 vs 跨租户）。"""
        return self._tenant_owners.get(draft_id)

    async def get_evidence(self, draft_id: str) -> list[dict]:
        d = self._drafts.get(draft_id)
        return d.evidence_refs if d else []


class DraftTraceRepo:
    def __init__(self) -> None:
        self._rows: list[dict] = []

    async def save_ok(self, draft_kind, draft, t0: float) -> None:
        self._rows.append({
            "draft_id": draft.draft_id, "draft_kind": draft_kind,
            "latency_ms": int((datetime.now().timestamp() - t0) * 1000),
            "created_at": datetime.now(),
        })


class NodeTraceRepo:
    """编排 node_trace：CODE/AGENT 节点执行记录。"""
    def __init__(self) -> None:
        self._rows: list[dict] = []

    async def save(self, *, session_id, step, node_type, capability=None,
                   status="OK", result=None, agent_hypothesis=None,
                   agent_confidence=None, tool_call_traces=None) -> str:
        rid = f"NT-{uuid.uuid4().hex[:8]}"
        self._rows.append({
            "record_id": rid, "session_id": session_id, "step": step,
            "node_type": node_type, "capability": capability, "status": status,
            "result": result, "agent_hypothesis": agent_hypothesis,
            "agent_confidence": agent_confidence,
            "tool_call_traces": tool_call_traces or [],
            "occurred_at": datetime.now(),
        })
        return rid

    async def list_for_session(self, session_id: str) -> list[dict]:
        return [r for r in self._rows if r["session_id"] == session_id]


class OrchestrationRepo:
    """编排 会话/步骤/gate/失败计数 仓储。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self._steps: list[dict] = []
        self._gates: list[dict] = []
        self._failure_counts: dict[tuple[str, str], int] = {}

    # ---- session ----
    async def create_session(self, session) -> None:
        self._sessions[session.session_id] = session

    async def get_session(self, session_id: str):
        return self._sessions.get(session_id)

    async def update_status(self, session_id: str, status: str,
                            current_step: str | None = None,
                            suspend_reason: str = "") -> None:
        from app.domain.orchestration_state import SessionStatus
        s = self._sessions.get(session_id)
        if s:
            try:
                s.status = SessionStatus(status)
            except ValueError:
                s.status = status  # type: ignore[assignment]
            if current_step is not None:
                s.current_step = current_step
            if suspend_reason:
                s.suspend_reason = suspend_reason
            s.updated_at = datetime.now()

    async def mark_done(self, session_id: str) -> None:
        await self.update_status(session_id, "DONE", "DONE")

    async def mark_failed(self, session_id: str, reason: str) -> None:
        await self.update_status(session_id, "FAILED", suspend_reason=reason)

    # ---- steps ----
    async def save_step(self, session_id, step, node_type, result=None,
                        status="OK", capability=None) -> str:
        rid = f"ST-{uuid.uuid4().hex[:8]}"
        self._steps.append({
            "record_id": rid, "session_id": session_id, "step": step,
            "node_type": node_type, "capability": capability, "status": status,
            "result": result, "occurred_at": datetime.now(),
        })
        return rid

    async def save_agent_step(self, state, capability, result) -> None:
        self._steps.append({
            "record_id": f"ST-{uuid.uuid4().hex[:8]}",
            "session_id": state.get("session_id", ""),
            "step": capability, "node_type": "AGENT", "capability": capability,
            "status": "OK", "result": result,
            "occurred_at": datetime.now(),
        })

    # ---- gates ----
    async def record_gate(self, session_id, step, decision) -> None:
        self._gates.append({
            "session_id": session_id, "step": step, "decision": decision,
            "occurred_at": datetime.now(),
        })

    # ---- failure tracker ----
    async def increment_failure_count(self, session_id: str, capability: str) -> int:
        key = (session_id, capability)
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        return self._failure_counts[key]

    async def reset_failure_count(self, session_id: str, capability: str) -> None:
        self._failure_counts[(session_id, capability)] = 0

    async def log_suspend_reason(self, session_id, capability, reason) -> None:
        await self.update_status(session_id, "SUSPENDED", suspend_reason=reason)


# ---- 单例 ----
_orchestration_repo: OrchestrationRepo | None = None
_trace_repo: ToolCallTraceRepo | None = None


def get_orchestration_repo() -> OrchestrationRepo:
    global _orchestration_repo
    if _orchestration_repo is None:
        _orchestration_repo = OrchestrationRepo()
    return _orchestration_repo


def get_tool_call_trace_repo() -> ToolCallTraceRepo:
    global _trace_repo
    if _trace_repo is None:
        _trace_repo = ToolCallTraceRepo()
    return _trace_repo
