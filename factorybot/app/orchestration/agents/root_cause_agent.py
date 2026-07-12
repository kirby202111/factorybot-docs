"""能力 A：换线钢网/程序核对异常根因推理 + 处置卡。

触发：barrier 检出 tooling_result.status == FAIL。换线全程 PASS 时不触发。
只在非确定分支触发；代码能做的不交给 LLM。
"""
from __future__ import annotations

from app.orchestration.agents.base import (
    build_agent_subgraph, to_tenant,
)


class RootCauseAgent:
    CAPABILITY = "root_cause"

    def __init__(self, llm, registry, trace_repo, obs) -> None:
        self._graph = build_agent_subgraph(
            llm, registry, trace_repo, obs, self.CAPABILITY, self._prompt,
        )

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        inputs = {
            "expected": state.get("expected"),
            "actual": state.get("actual"),
            "mismatch_code": state.get("mismatch_code"),
        }
        sub_state = {
            "tenant": to_tenant(state), "obs_ctx": state.get("obs_ctx"),
            "inputs": inputs, "step_no": 0, "messages": [], "pending_tool_calls": [],
        }
        res = await self._graph.ainvoke(sub_state, config=config or {})
        return res.get("result") or {"hypothesis": {"root_cause": "未知"}, "confidence": "low"}

    def _prompt(self, state: dict) -> str:
        i = state.get("inputs", {})
        return (
            "你是 MES 换线钢网/程序核对异常的根因诊断 agent。\n"
            f"输入：结构化 mismatch（expected={i.get('expected')}, "
            f"actual={i.get('actual')}, code={i.get('mismatch_code')}）。\n"
            "按需调用只读工具取证（query_stencil_lending 钢网借还记录 / "
            "query_last_changeover_close 上工单收线记录），不要套固定决策树。\n"
            "取证充分后输出 JSON："
            "{\"hypothesis\":{\"root_cause\":\"...\",\"evidence\":[\"trace_id=...\"]},"
            "\"confidence\":\"high|medium|low\","
            "\"disposition_card\":{\"intent\":\"...\",\"route_to\":\"...\",\"suggested_actions\":[...]}}\n"
            "不得直接下达处置，处置卡需人确认。"
        )
