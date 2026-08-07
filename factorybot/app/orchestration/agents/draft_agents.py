"""能力 D：生成类（8D / 返工工艺草拟）。开放生成，代码完全做不了。"""
from __future__ import annotations

from app.infrastructure.ai.react_graph import build_react_graph, to_tenant


class DraftAgent:
    """单个 draft 子能力 agent。capability ∈ draft_8d | draft_rework_craft。"""

    def __init__(self, capability: str, llm, registry, trace_repo, obs, result_compactor=None) -> None:
        self.CAPABILITY = capability
        self._kind = capability
        self._graph = build_react_graph(
            llm, registry, trace_repo, obs,
            capability=capability, prompt_fn=self._prompt,
            result_compactor=result_compactor,
        )

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        inputs = {
            "route_id": state.get("target_route_id"),
            "route_version": state.get("target_route_version"),
            "work_order_id": state.get("work_order_id"),
            "batch_id": state.get("batch_id") or state.get("complaint_batch_id"),
        }
        sub_state = {
            "tenant": to_tenant(state), "obs_ctx": state.get("obs_ctx"),
            "inputs": inputs, "step_no": 0, "messages": [], "pending_tool_calls": [],
        }
        res = await self._graph.ainvoke(sub_state, config=config or {})
        return res.get("result") or {"intent": "草拟", "draft_payload": {}, "confidence": "medium"}

    def _prompt(self, state: dict) -> str:
        if self._kind == "draft_8d":
            return (
                "你是 MES 8D 报告草拟 agent。基于追溯链草拟 8D 报告（开放生成）。\n"
                "输出 JSON：{\"intent\":\"...\",\"draft_payload\":{\"问题描述\",\"根因\",\"containment\",\"纠正措施\"},"
                "\"confidence\":\"high|medium|low\"}"
            )
        return (
            "你是 MES 返工工艺草拟 agent。基于不良模式 + 历史返工记录草拟返工工艺建议（开放生成）。\n"
            "输出 JSON：{\"intent\":\"...\",\"draft_payload\":{\"rework_route\",\"reentry_point\"},"
            "\"confidence\":\"high|medium|low\"}"
        )
