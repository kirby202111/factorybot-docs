"""SupervisorGraph：纯代码编排器容器，暴露节点可调用对象供场景装配。

不持工具、不调 LLM（supervisor capability 下无任何工具，WriteToolGate 断言）。
场景图（changeover/fault_response/...）各自装配 StateGraph(OrchestrationState)，复用这里的节点。
"""
from __future__ import annotations

from typing import Any, Optional

from app.application.action_card_dispatcher import ActionCardDispatcher
from app.domain.orchestration_state import ActionCard
from app.infrastructure.persistence.repos import OrchestrationRepo
from app.orchestration.action_card_builder import build_action_card
from app.orchestration.agents import AgentRegistry
from app.orchestration.code_nodes.barrier import FailureTracker, barrier_node
from app.orchestration.code_nodes.gate import GateManager
from app.orchestration.code_nodes.plan import PlanNode
from app.orchestration.code_nodes.query_compare import QueryCompareNodes
from app.orchestration.code_nodes.write_via_appservice import WriteViaAppService


def _token_to_dict(token) -> dict:
    if token is None:
        return None
    return {
        "id": token.id, "session_id": token.session_id, "step": token.step,
        "action": token.action, "approved": token.approved,
        "user_id": token.user_id, "issued_at": token.issued_at,
    }


def _token_from_dict(d: Optional[dict]):
    if d is None:
        return None
    from app.infrastructure.redis_.confirmation_store import ConfirmationToken
    return ConfirmationToken(
        id=d["id"], session_id=d["session_id"], step=d["step"],
        action=d["action"], approved=d["approved"],
        user_id=d["user_id"], issued_at=d["issued_at"],
    )


class SupervisorGraph:
    def __init__(
        self,
        query_compare: QueryCompareNodes,
        agents: AgentRegistry,
        gates: GateManager,
        repo: OrchestrationRepo,
        failure_tracker: FailureTracker,
        dispatcher: ActionCardDispatcher,
        write_service: WriteViaAppService,
    ) -> None:
        self.plan_node = PlanNode()
        self.qc = query_compare
        self.agents = agents
        self.gates = gates
        self.repo = repo
        self.failure_tracker = failure_tracker
        self.dispatcher = dispatcher
        self.write = write_service

    # ---- 代码节点直接引用 ----
    async def plan(self, state: dict) -> dict:
        return await self.plan_node.plan(state)

    async def done(self, state: dict) -> dict:
        return await self.plan_node.done(state)

    # ---- gate 工厂 ----
    def gate(self, step: str, capability: Optional[str] = None):
        async def fn(state: dict) -> dict:
            card = build_action_card(state, step, capability)
            decision = await self.gates.await_confirmation(
                state.get("session_id", ""), step, card,
            )
            # interrupt 返回的 confirmation 已在 GateManager 内消费；
            # 此处把 token 字典透传给后续 write 节点（若有）
            return {f"gate_{step.lower()}": decision}
        return fn

    # ---- agent 工厂 ----
    def run_agent(self, capability: str):
        async def fn(state: dict) -> dict:
            agent = self.agents.get(capability)
            if agent is None:
                return {"agent_hypothesis": {"error": f"unknown {capability}"},
                        "agent_confidence": "low"}
            try:
                result = await agent.ainvoke(
                    state,
                    config={"configurable": {"thread_id": f"{state.get('session_id','')}_{capability}"}},
                )
                await self.failure_tracker.record_agent_result(
                    state.get("session_id", ""), capability, {"status": "SUCCESS"},
                )
                mapped = self._map_result(capability, result)
                await self.repo.save_agent_step(state, capability, result)
                return mapped
            except Exception as e:
                ok = await self.failure_tracker.record_agent_result(
                    state.get("session_id", ""), capability, {"status": "FAILED", "error": str(e)},
                )
                if not ok:
                    return {"agent_hypothesis": {"error": str(e)}, "agent_confidence": "low",
                            "status": "SUSPENDED"}
                return {"agent_hypothesis": {"error": str(e)}, "agent_confidence": "low"}
        return fn

    @staticmethod
    def _map_result(capability: str, result: dict) -> dict:
        confidence = result.get("confidence")
        if "disposition_card" in result:
            return {"agent_hypothesis": result.get("hypothesis"),
                    "agent_confidence": confidence,
                    "action_card": result.get("disposition_card")}
        if "draft_payload" in result:
            return {"agent_hypothesis": result, "agent_confidence": confidence,
                    "action_card": {"intent": result.get("intent"),
                                    "draft_payload": result.get("draft_payload")}}
        return {"agent_hypothesis": result.get("hypothesis", result),
                "agent_confidence": confidence}

    # ---- barrier 工厂 ----
    async def barrier(self, state: dict) -> dict:
        return await barrier_node(state, self.repo, self.dispatcher)
