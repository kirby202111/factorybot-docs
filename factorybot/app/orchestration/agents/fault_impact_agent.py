"""能力 B：设备故障隔离范围动态判定。

触发：设备故障事件（equipment.fault）。输出 fault_mode/drift_window/isolation_set/release。
"""
from __future__ import annotations

from app.infrastructure.ai.react_graph import build_react_graph, to_tenant


class FaultImpactAgent:
    CAPABILITY = "fault_impact"

    def __init__(self, llm, registry, trace_repo, obs) -> None:
        self._graph = build_react_graph(
            llm, registry, trace_repo, obs,
            capability=self.CAPABILITY, prompt_fn=self._prompt,
        )

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        inputs = {"asset_id": state.get("asset_id"), "fault_time": state.get("fault_time")}
        sub_state = {
            "tenant": to_tenant(state), "obs_ctx": state.get("obs_ctx"),
            "inputs": inputs, "step_no": 0, "messages": [], "pending_tool_calls": [],
        }
        res = await self._graph.ainvoke(sub_state, config=config or {})
        return res.get("result") or {"hypothesis": {}, "confidence": "low"}

    def _prompt(self, state: dict) -> str:
        i = state.get("inputs", {})
        return (
            "你是 MES 设备故障隔离范围判定 agent（FaultImpact）。\n"
            f"输入：asset_id={i.get('asset_id')}, fault_time={i.get('fault_time')}。\n"
            "调用 query_equipment_telemetry / query_process_fmea / query_product_sensitivity 取证，"
            "推理故障模式（硬停/软漂移/间歇）、漂移窗口、隔离集与放行集。\n"
            "输出 JSON：{\"hypothesis\":{\"fault_mode\",\"drift_window\",\"isolation_set\","
            "\"release\",\"sensitivity_reason\"},\"confidence\":\"high|medium|low\","
            "\"disposition_card\":{\"intent\",\"route_to\",\"suggested_actions\"}}"
        )
