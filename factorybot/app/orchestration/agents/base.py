"""agent 子图基类：通用 ReAct subgraph（model -> tools -> model），产出 result dict。

每个 agent 能力（A/B/D）复用此结构，仅 system prompt 不同。C（客诉追溯）复用 L1 诊断图。
"""
from __future__ import annotations

import json
import operator
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.tenant import TenantContext
from app.domain.tool import ToolRegistry
from app.infrastructure.ai.base import assistant_msg, sys_msg, user_msg
from app.infrastructure.ai.tool_node import ToolNode, tool_to_schema
from app.infrastructure.persistence.repos import ToolCallTraceRepo


class AgentSubgraphState(TypedDict, total=False):
    messages: Annotated[list, operator.add]
    pending_tool_calls: list[dict]
    tenant: Any
    obs_ctx: Any
    step_no: int
    inputs: dict
    result: Optional[dict]


def to_tenant(state: dict) -> TenantContext:
    t = state.get("tenant")
    if isinstance(t, TenantContext):
        return t
    if isinstance(t, dict):
        return TenantContext.model_validate(t)
    return TenantContext.default()


def inputs_to_text(inputs: dict) -> str:
    return "\n".join(f"{k}={v}" for k, v in inputs.items() if v is not None)


async def _ainvoke(llm, messages, tools, obs_ctx):
    try:
        return await llm.ainvoke(messages, tools, obs_ctx=obs_ctx)
    except TypeError:
        return await llm.ainvoke(messages, tools)


def build_agent_subgraph(
    llm, registry: ToolRegistry, trace_repo: ToolCallTraceRepo, obs,
    capability: str, prompt_fn, recursion_limit: int = 10,
):
    """构建 agent ReAct 子图。model 节点按 prompt_fn 生成系统提示。"""
    tool_node = ToolNode(registry, trace_repo, obs, capability)

    async def model_node(state: AgentSubgraphState) -> dict:
        tenant = to_tenant(state)
        tools = [tool_to_schema(d) for d in registry.tools_for(capability, tenant)]
        history = state.get("messages") or []
        messages = [sys_msg(prompt_fn(state))]
        new_msgs: list[dict] = []
        if history:
            messages.extend(history)
        else:
            um = user_msg(inputs_to_text(state.get("inputs", {})))
            messages.append(um)
            new_msgs.append(um)
        resp = await _ainvoke(llm, messages, tools, state.get("obs_ctx"))
        step_no = state.get("step_no", 0) + 1
        if resp.tool_calls:
            new_msgs.append(assistant_msg(resp.content, [tc.model_dump() for tc in resp.tool_calls]))
            return {
                "pending_tool_calls": [tc.model_dump() for tc in resp.tool_calls],
                "messages": new_msgs,
                "step_no": step_no,
            }
        try:
            result = json.loads(resp.content)
        except (json.JSONDecodeError, TypeError):
            result = {"error": "non-json output", "content": resp.content[:500]}
        new_msgs.append(assistant_msg(resp.content))
        return {"pending_tool_calls": [], "messages": new_msgs,
                "step_no": step_no, "result": result}

    def route(state: AgentSubgraphState) -> str:
        return "tools" if state.get("pending_tool_calls") else END

    g = StateGraph(AgentSubgraphState)
    g.add_node("model", model_node)
    g.add_node("tools", tool_node)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", route, ["tools", END])
    g.add_edge("tools", "model")
    compiled = g.compile()
    compiled.recursion_limit = recursion_limit
    return compiled
