"""OrchestrationService：会话生命周期 + interrupt/resume 驱动。

start() 创建 session + asyncio.create_task(_drive) 点火；HTTP 立即返回 session_id。
_drive() ainvoke 到首个 gate interrupt 返回；_after_invoke() 检测 interrupt -> 推动作卡。
resume() 人确认 -> issue token -> ainvoke(Command(resume=token)) 续跑。
Pod 重启后同一 thread_id 续跑（mock 用 MemorySaver，real 用 SqlSaver 落 MySQL）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Optional

from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from app.application.action_card_dispatcher import ActionCardDispatcher
from app.config import get_settings
from app.domain.errors import ResourceAccessError
from app.domain.orchestration_state import ActionCard, OrchestrationSession, ScenarioType, SessionStatus
from app.domain.tenant import TenantContext
from app.infrastructure.longtask.session_manager import SessionManager
from app.infrastructure.obs.logging import get_logger
from app.infrastructure.persistence.repos import OrchestrationRepo
from app.infrastructure.redis_.confirmation_store import ConfirmationStore

_log = get_logger("orchestration")

SCENARIO_MAP: dict[str, ScenarioType] = {
    "changeover": ScenarioType.CHANGEOVER,
    "fault_response": ScenarioType.FAULT_RESPONSE,
    "complaint_8d": ScenarioType.COMPLAINT_8D,
}


def _extract_pending_card(state) -> Optional[ActionCard]:
    """从 LangGraph StateSnapshot 提取 pending interrupt 的动作卡。"""
    tasks = getattr(state, "tasks", None) or ()
    for task in tasks:
        for intr in (getattr(task, "interrupts", None) or ()):
            val = intr.value
            if isinstance(val, ActionCard):
                return val
            if isinstance(val, dict):
                try:
                    return ActionCard.model_validate(val)
                except Exception:
                    continue
    return None


def _extract_pending_interrupts(state) -> list:
    """提取所有 pending interrupt 的 (id, card)，用于多并行 gate 的 resume。

    fault_response 等场景 gate_repair ‖ gate_isolation 并行，两个 gate 都 interrupt(value=card)，
    产生多个 pending interrupt；resume 须对每个按 id 提供值，且各 gate 的 card action 不同，
    需分别签发匹配 token。
    """
    result = []
    for task in (getattr(state, "tasks", None) or ()):
        for intr in (getattr(task, "interrupts", None) or ()):
            val = getattr(intr, "value", None)
            card = None
            if isinstance(val, ActionCard):
                card = val
            elif isinstance(val, dict):
                try:
                    card = ActionCard.model_validate(val)
                except Exception:
                    continue
            if card is not None:
                result.append((getattr(intr, "id", None), card))
    return result


class OrchestrationService:
    def __init__(
        self,
        graphs: dict[str, object],
        session_manager: SessionManager,
        repo: OrchestrationRepo,
        confirmation_store: ConfirmationStore,
        dispatcher: ActionCardDispatcher,
    ) -> None:
        self._graphs = graphs
        self._sessions = session_manager
        self._repo = repo
        self._store = confirmation_store
        self._dispatcher = dispatcher
        s = get_settings()
        self._recursion_limit = s.orchestration_recursion_limit
        self._timeout = s.orchestration_session_timeout
        self._active_tasks: dict[str, asyncio.Task] = {}
        # 崩溃兜底调度的 mark_failed 任务引用集合（防止被 GC 提前回收）
        self._finalize_tasks: set[asyncio.Task] = set()

    # ---- 启动 ----
    async def start(
        self, scenario: str, tenant: TenantContext,
        work_order_id: str | None = None, batch_id: str | None = None,
        asset_id: str | None = None, target_route_id: str | None = None,
        target_route_version: str | None = None, fault_time: str | None = None,
        complaint_batch_id: str | None = None,
    ) -> OrchestrationSession:
        sc = SCENARIO_MAP.get(scenario)
        if sc is None:
            raise ValueError(f"未知场景: {scenario}")
        session = OrchestrationSession(
            session_id=f"S-ORCH-{uuid.uuid4().hex[:8]}",
            scenario=sc, work_order_id=work_order_id, batch_id=batch_id,
            asset_id=asset_id, target_route_id=target_route_id,
            target_route_version=target_route_version,
            tenant_context=tenant.model_dump(),
            status=SessionStatus.PLANNING,
        )
        # 故障/客诉场景的额外字段存入 tenant_context 旁路（OrchestrationState 在 _drive 注入）
        if fault_time:
            session.tenant_context["fault_time"] = fault_time
        if complaint_batch_id:
            session.tenant_context["complaint_batch_id"] = complaint_batch_id
        await self._sessions.create(session)
        await self._repo.update_status(session.session_id, "RUNNING", "PLAN")
        task = asyncio.create_task(self._drive(session, tenant))
        self._active_tasks[session.session_id] = task
        task.add_done_callback(self._on_drive_done(session.session_id))
        return session

    async def _drive(self, session: OrchestrationSession, tenant: TenantContext) -> None:
        graph = self._graphs[session.scenario.value]
        config = {"configurable": {"thread_id": session.session_id},
                  "recursion_limit": self._recursion_limit}
        initial = {
            "session_id": session.session_id, "scenario": session.scenario.value,
            "tenant": tenant.model_dump(), "work_order_id": session.work_order_id,
            "batch_id": session.batch_id, "asset_id": session.asset_id,
            "target_route_id": session.target_route_id,
            "target_route_version": session.target_route_version,
            "fault_time": session.tenant_context.get("fault_time"),
            "complaint_batch_id": session.tenant_context.get("complaint_batch_id"),
            "status": "RUNNING", "created_at": datetime.now().isoformat(),
        }
        try:
            await asyncio.wait_for(graph.ainvoke(initial, config=config), timeout=self._timeout)
        except GraphRecursionError:
            await self._repo.mark_failed(session.session_id, "步数超限")
            return
        except asyncio.TimeoutError:
            await self._repo.mark_failed(session.session_id, "整体超时")
            return
        except Exception as e:
            await self._repo.mark_failed(session.session_id, f"驱动异常: {e}")
            return
        await self._after_invoke(session.session_id, session.scenario.value)

    def _on_drive_done(self, session_id: str):
        """驱动任务结束兜底：清理 _active_tasks；未捕获异常记 CRITICAL 并尽力把会话转
        FAILED，避免崩溃后卡 RUNNING 成为孤儿。

        _drive 自身 try/except 覆盖 graph.ainvoke，但 graph 未注册(KeyError，在 try 之前)、
        mark_failed 自身失败、_after_invoke 推卡/落库失败等仍会逃逸到 task -> 此处兜底。
        本回调由 asyncio 同步调用，不可 await；mark_failed 经 create_task 调度，
        其内部异常由 _finalize_crashed_session 自身兜底（再失败仅记日志，不再传播）。
        """
        def _cb(task: asyncio.Task) -> None:
            self._active_tasks.pop(session_id, None)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            _log.critical(
                "orchestration.drive_task_crashed",
                session_id=session_id,
                error=repr(exc),
                error_type=type(exc).__name__,
            )
            fin = asyncio.create_task(self._finalize_crashed_session(session_id, exc))
            self._finalize_tasks.add(fin)
            fin.add_done_callback(self._finalize_tasks.discard)

        return _cb

    async def _finalize_crashed_session(self, session_id: str, exc: BaseException) -> None:
        """崩溃兜底：尽力 mark_failed；自身失败仅记日志，不再传播（避免再次静默丢失）。"""
        try:
            await self._repo.mark_failed(session_id, f"驱动任务崩溃: {exc}")
        except Exception as finalize_exc:
            _log.error(
                "orchestration.finalize_crashed_session_failed",
                session_id=session_id,
                error=repr(finalize_exc),
                error_type=type(finalize_exc).__name__,
            )

    # ---- 确认续跑 ----
    async def resume(self, session_id: str, step: str, approved: bool,
                     tenant: TenantContext) -> str:
        session = await self.get_session(session_id, tenant)
        if session is None:
            raise ResourceAccessError(f"会话不存在: {session_id}")
        user_id = tenant.user_id
        scenario = session.scenario.value
        graph = self._graphs[scenario]
        config = {"configurable": {"thread_id": session_id},
                  "recursion_limit": self._recursion_limit}
        state = await graph.aget_state(config)
        pending = _extract_pending_interrupts(state)
        decision = "PASS" if approved else "REJECT"
        # 多并行 gate（如 gate_repair ‖ gate_isolation）产生多个 pending interrupt，
        # 须按 id 映射 resume；各 gate card action 不同，分别签发匹配 token（用 dict 形态
        # 便于 checkpointer 序列化，GateManager 兼容 dict 解析）
        if len(pending) <= 1:
            card = pending[0][1] if pending else None
            action = card.writes_via_action() if card else f"{session_id}:{step}"
            token = await self._store.issue(session_id, step, approved, user_id, action=action)
            resume_value = token
        else:
            from dataclasses import asdict
            resume_value = {}
            for iid, card in pending:
                tok = await self._store.issue(
                    session_id, card.step, approved, user_id, action=card.writes_via_action())
                resume_value[iid] = asdict(tok)
        try:
            await graph.ainvoke(Command(resume=resume_value), config=config)
        except GraphRecursionError:
            await self._repo.mark_failed(session_id, "步数超限")
            return decision
        except asyncio.TimeoutError:
            await self._repo.mark_failed(session_id, "整体超时")
            return decision
        except Exception as e:
            await self._repo.mark_failed(session_id, f"续跑异常: {e}")
            return decision
        await self._after_invoke(session_id, scenario)
        return decision

    # ---- 状态检查 + 推卡 ----
    async def _after_invoke(self, session_id: str, scenario: str) -> Optional[str]:
        graph = self._graphs[scenario]
        config = {"configurable": {"thread_id": session_id}}
        try:
            state = await graph.aget_state(config)
        except Exception:
            return None
        card = _extract_pending_card(state)
        if card is not None:
            await self._dispatcher.push(card)
            await self._repo.update_status(session_id, "RUNNING", card.step)
            return card.step
        if not getattr(state, "next", None):
            await self._repo.mark_done(session_id)
        return None

    # ---- 查询 ----
    async def get_session(self, session_id: str,
                          tenant: TenantContext) -> Optional[OrchestrationSession]:
        session = await self._repo.get_session(session_id)
        if session is None:
            return None
        if session.tenant_context.get("tenant_id") != tenant.tenant_id:
            # 跨租户访问企图：记 warning 供安全审计（对外仍统一 404 隐藏存在性）
            _log.warning(
                "orchestration.tenant_access_denied",
                session_id=session_id,
                owner_tenant=session.tenant_context.get("tenant_id"),
                caller_tenant=tenant.tenant_id,
            )
            raise ResourceAccessError(f"会话不属于当前租户: {session_id}")
        return session

    async def pending_step(self, session_id: str) -> Optional[str]:
        session = await self._repo.get_session(session_id)
        if session is None or session.status != SessionStatus.RUNNING:
            return None
        graph = self._graphs[session.scenario.value]
        config = {"configurable": {"thread_id": session_id}}
        try:
            state = await graph.aget_state(config)
        except Exception:
            return None
        card = _extract_pending_card(state)
        return card.step if card else None
