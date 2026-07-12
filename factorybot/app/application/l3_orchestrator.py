"""L3Orchestrator：会话生命周期 + interrupt/resume 驱动。

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
from app.domain.l3_state import ActionCard, L3Session, ScenarioType, SessionStatus
from app.domain.tenant import TenantContext
from app.infrastructure.longtask.session_manager import SessionManager
from app.infrastructure.persistence.repos import L3Repo
from app.infrastructure.redis_.confirmation_store import ConfirmationStore

SCENARIO_MAP: dict[str, ScenarioType] = {
    "changeover": ScenarioType.CHANGEOVER,
    "fault_response": ScenarioType.FAULT_RESPONSE,
    "complaint_8d": ScenarioType.COMPLAINT_8D,
    "process_change": ScenarioType.PROCESS_CHANGE,
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


class L3Orchestrator:
    def __init__(
        self,
        graphs: dict[str, object],
        session_manager: SessionManager,
        repo: L3Repo,
        confirmation_store: ConfirmationStore,
        dispatcher: ActionCardDispatcher,
    ) -> None:
        self._graphs = graphs
        self._sessions = session_manager
        self._repo = repo
        self._store = confirmation_store
        self._dispatcher = dispatcher
        s = get_settings()
        self._recursion_limit = s.l3_recursion_limit
        self._timeout = s.l3_session_timeout
        self._active_tasks: dict[str, asyncio.Task] = {}

    # ---- 启动 ----
    async def start(
        self, scenario: str, tenant: TenantContext,
        work_order_id: str | None = None, batch_id: str | None = None,
        asset_id: str | None = None, target_route_id: str | None = None,
        target_route_version: str | None = None, fault_time: str | None = None,
        complaint_batch_id: str | None = None,
    ) -> L3Session:
        sc = SCENARIO_MAP.get(scenario)
        if sc is None:
            raise ValueError(f"未知场景: {scenario}")
        session = L3Session(
            session_id=f"S-L3-{uuid.uuid4().hex[:8]}",
            scenario=sc, work_order_id=work_order_id, batch_id=batch_id,
            asset_id=asset_id, target_route_id=target_route_id,
            target_route_version=target_route_version,
            tenant_context=tenant.model_dump(),
            status=SessionStatus.PLANNING,
        )
        # 故障/客诉场景的额外字段存入 tenant_context 旁路（L3State 在 _drive 注入）
        if fault_time:
            session.tenant_context["fault_time"] = fault_time
        if complaint_batch_id:
            session.tenant_context["complaint_batch_id"] = complaint_batch_id
        await self._sessions.create(session)
        await self._repo.update_status(session.session_id, "RUNNING", "PLAN")
        task = asyncio.create_task(self._drive(session, tenant))
        self._active_tasks[session.session_id] = task
        task.add_done_callback(lambda t: self._active_tasks.pop(session.session_id, None))
        return session

    async def _drive(self, session: L3Session, tenant: TenantContext) -> None:
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

    # ---- 确认续跑 ----
    async def resume(self, session_id: str, step: str, approved: bool,
                     user_id: str) -> str:
        session = await self._repo.get_session(session_id)
        if session is None:
            raise ValueError(f"会话不存在: {session_id}")
        scenario = session.scenario.value
        graph = self._graphs[scenario]
        config = {"configurable": {"thread_id": session_id},
                  "recursion_limit": self._recursion_limit}
        state = await graph.aget_state(config)
        card = _extract_pending_card(state)
        action = card.writes_via_action() if card else f"{session_id}:{step}"
        token = await self._store.issue(session_id, step, approved, user_id, action=action)
        decision = "PASS" if approved else "REJECT"
        try:
            await graph.ainvoke(Command(resume=token), config=config)
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
    async def get_session(self, session_id: str) -> Optional[L3Session]:
        return await self._repo.get_session(session_id)

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
