"""Agent 能力注册表：capability -> agent 实例。supervisor 通过 _run_agent 调用。"""
from __future__ import annotations

from typing import Optional, Protocol


class AgentCapability(Protocol):
    CAPABILITY: str

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict: ...


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentCapability] = {}

    def register(self, agent: AgentCapability) -> None:
        self._agents[agent.CAPABILITY] = agent

    def get(self, capability: str) -> Optional[AgentCapability]:
        return self._agents.get(capability)

    def capabilities(self) -> list[str]:
        return list(self._agents.keys())


def build_agent_registry(
    llm, orchestration_registry, diagnosis_registry, trace_repo, obs,
) -> AgentRegistry:
    """构建 A/B/C/D agent 注册表。"""
    from app.orchestration.agents.draft_agents import DraftAgent
    from app.orchestration.agents.fault_impact_agent import FaultImpactAgent
    from app.orchestration.agents.root_cause_agent import RootCauseAgent
    from app.orchestration.agents.traceability_agent import TraceabilityAgent

    reg = AgentRegistry()
    reg.register(RootCauseAgent(llm, orchestration_registry, trace_repo, obs))         # A
    reg.register(FaultImpactAgent(llm, orchestration_registry, trace_repo, obs))       # B
    reg.register(TraceabilityAgent(llm, diagnosis_registry, trace_repo, obs))      # C
    reg.register(DraftAgent("draft_sop", llm, orchestration_registry, trace_repo, obs))
    reg.register(DraftAgent("draft_8d", llm, orchestration_registry, trace_repo, obs))
    reg.register(DraftAgent("draft_rework_craft", llm, orchestration_registry, trace_repo, obs))
    return reg
