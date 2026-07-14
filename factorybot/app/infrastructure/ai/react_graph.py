"""ReAct 图基座：通用 model -> tools -> model 子图，行为由钩子注入。

L1 诊断图与 L3 agent 子图（A/B/D）共用此基座，仅 prompt / 收尾不同：
- prompt_fn(state): 系统提示（每步重建）
- user_prompt_fn(state): 首轮 user 消息（默认 inputs_to_text）
- finalize_fn(content, state): 终止步收尾，返回 state 增量（默认 json.loads -> {"result":...}）

L1 的 _guard_no_evidence 护栏作为 L1 专属 finalize_fn 注入，不下沉基座
（draft 类开放生成 agent 不应被误伤）。详见 graph_builder.l1_finalize。
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


class ReactState(TypedDict, total=False):
    messages: Annotated[list, operator.add]   # 节点返回新增消息，reducer 拼接
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
    """兼容 ObservableChatModel（接受 obs_ctx）与裸 ChatModel。"""
    try:
        return await llm.ainvoke(messages, tools, obs_ctx=obs_ctx)
    except TypeError:
        return await llm.ainvoke(messages, tools)


def _default_finalize(content: str) -> dict:
    """默认终止步：json.loads -> {"result":...}；非 JSON 兜底 err dict。"""
    try:
        return {"result": json.loads(content)}
    except (json.JSONDecodeError, TypeError):
        return {"result": {"error": "non-json output", "content": content[:500]}}


def build_react_graph(
    llm,
    registry: ToolRegistry,
    trace_repo: ToolCallTraceRepo,
    obs,
    *,
    capability: str,
    prompt_fn,
    user_prompt_fn=None,
    finalize_fn=None,
    state_schema=ReactState,
    recursion_limit: int = 10,
):
    """构建通用 ReAct 子图。model 节点按 prompt_fn 生成系统提示，终止步按 finalize_fn 收尾。

    - prompt_fn(state)->str：系统提示。
    - user_prompt_fn(state)->str：首轮 user 消息；默认 inputs_to_text(inputs)。
    - finalize_fn(content, state)->dict：终止步返回的 state 增量；默认 _default_finalize。
    - state_schema：LangGraph state TypedDict，默认 ReactState；L1 传 AgentState 以声明 report 通道。
    """
    tool_node = ToolNode(registry, trace_repo, obs, capability)
    _user_prompt_fn = user_prompt_fn or (lambda state: inputs_to_text(state.get("inputs", {})))

    async def model_node(state) -> dict:
        tenant = to_tenant(state)
        tools = [tool_to_schema(d) for d in registry.tools_for(capability, tenant)]
        history = state.get("messages") or []
        messages = [sys_msg(prompt_fn(state))]
        new_msgs: list[dict] = []
        if history:
            messages.extend(history)
        else:
            um = user_msg(_user_prompt_fn(state))
            messages.append(um)
            new_msgs.append(um)  # 首轮持久化 user 消息，供后续步引用
        resp = await _ainvoke(llm, messages, tools, state.get("obs_ctx"))
        step_no = state.get("step_no", 0) + 1
        if resp.tool_calls:
            new_msgs.append(assistant_msg(resp.content, [tc.model_dump() for tc in resp.tool_calls]))
            return {
                "pending_tool_calls": [tc.model_dump() for tc in resp.tool_calls],
                "messages": new_msgs,
                "step_no": step_no,
            }
        # 终止：按 finalize_fn 收尾（L1 解析报告 + 护栏；其余默认 json.loads）
        new_msgs.append(assistant_msg(resp.content))
        updates = finalize_fn(resp.content, state) if finalize_fn else _default_finalize(resp.content)
        return {"pending_tool_calls": [], "messages": new_msgs,
                "step_no": step_no, **updates}

    def route(state) -> str:
        return "tools" if state.get("pending_tool_calls") else END

    g = StateGraph(state_schema)
    g.add_node("model", model_node)
    g.add_node("tools", tool_node)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", route, ["tools", END])
    g.add_edge("tools", "model")
    compiled = g.compile()
    compiled.recursion_limit = recursion_limit
    return compiled
