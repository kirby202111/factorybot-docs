"""L1 诊断 ReAct 图构建：agent 节点 -> 工具节点 -> 回模型，条件边 + recursion_limit。

StateGraph 把"模型思考 -> 工具执行 -> 回模型"做成显式图，可对每条边加条件路由/超时/
递归上限。recursion_limit=20 是硬上限靠框架兜底。不引入 Celery/AgentExecutor。
"""
from __future__ import annotations

import json
import operator
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.report import DiagnosisReport
from app.domain.tool import ToolRegistry
from app.infrastructure.ai.base import assistant_msg, sys_msg, user_msg
from app.infrastructure.ai.tool_node import ToolNode, tool_to_schema
from app.infrastructure.persistence.repos import ToolCallTraceRepo


class AgentState(TypedDict, total=False):
    messages: Annotated[list, operator.add]   # 节点返回新增消息，reducer 拼接
    pending_tool_calls: list[dict]
    tenant: Any
    obs_ctx: Any
    question: str
    serial_no: Optional[str]
    work_order_id: Optional[str]
    route_version: Optional[str]
    subgraph_ref: Optional[str]
    step_no: int
    report: Optional[dict]


L1_SYSTEM_PROMPT = (
    "你是 MES 车间根因诊断助手。基于只读工具按 5M1E 给出根因假设排序。\n"
    "约束：\n"
    "1. 只能调用提供的工具，不得编造数据。\n"
    "2. 先调追溯图（query_traceability_graph）建立全链路视图，再按需降级查只读 REST。\n"
    "3. 查工艺必须带 route_version（从过点记录提取）。\n"
    "4. 每个假设必须引用工具返回的证据（trace_id）。\n"
    "5. 证据充分后输出严格遵循 DiagnosisReport JSON 结构（summary/confidence/hypotheses/"
    "subgraph_ref/route_version/evidence_refs/needs_human_review），不要再调工具。"
)


def build_diagnosis_graph(
    llm, registry: ToolRegistry, trace_repo: ToolCallTraceRepo, obs=None,
    capability: str = "l1", recursion_limit: int = 20,
):
    """构建并编译 L1 诊断图（无 checkpointer，同步跑完）。"""
    tool_node = ToolNode(registry, trace_repo, obs, capability)

    async def agent_node(state: AgentState) -> dict:
        tools = [tool_to_schema(d) for d in registry.tools_for(capability, state["tenant"])]
        history = state.get("messages") or []
        messages = [sys_msg(L1_SYSTEM_PROMPT)]
        new_msgs: list[dict] = []
        if history:
            messages.extend(history)
        else:
            um = user_msg(_build_user_prompt(state))
            messages.append(um)
            new_msgs.append(um)  # 首轮持久化 user 消息，供后续步引用
        obs_ctx = state.get("obs_ctx")
        # ObservableChatModel.ainvoke 接受 obs_ctx；裸 ChatModel 不接受（_ainvoke 兼容两者）
        resp = await _ainvoke(llm, messages, tools, obs_ctx)
        step_no = state.get("step_no", 0) + 1
        if resp.tool_calls:
            new_msgs.append(assistant_msg(resp.content, [tc.model_dump() for tc in resp.tool_calls]))
            return {
                "pending_tool_calls": [tc.model_dump() for tc in resp.tool_calls],
                "messages": new_msgs,
                "step_no": step_no,
            }
        # 完成：解析 DiagnosisReport
        new_msgs.append(assistant_msg(resp.content))
        report = _parse_report(resp.content, state)
        return {"pending_tool_calls": [], "messages": new_msgs,
                "step_no": step_no, "report": report.model_dump()}

    def route(state: AgentState) -> str:
        return "tools" if state.get("pending_tool_calls") else END

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, ["tools", END])
    g.add_edge("tools", "agent")
    compiled = g.compile()
    compiled.recursion_limit = recursion_limit
    return compiled


async def _ainvoke(llm, messages, tools, obs_ctx):
    """兼容 ObservableChatModel（接受 obs_ctx）与裸 ChatModel。"""
    try:
        return await llm.ainvoke(messages, tools, obs_ctx=obs_ctx)
    except TypeError:
        return await llm.ainvoke(messages, tools)


def _build_user_prompt(state: AgentState) -> str:
    q = state.get("question", "")
    parts = [f"问题：{q}"]
    if state.get("serial_no"):
        parts.append(f"单件 serial_no={state['serial_no']}")
    if state.get("work_order_id"):
        parts.append(f"工单 work_order_id={state['work_order_id']}")
    if state.get("route_version"):
        parts.append(f"工艺版本 route_version={state['route_version']}")
    if state.get("subgraph_ref"):
        parts.append(f"子图引用 subgraph_ref={state['subgraph_ref']}")
    return "\n".join(parts)


def _parse_report(content: str, state: AgentState) -> DiagnosisReport:
    """解析 LLM 输出为 DiagnosisReport；失败则转人工。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return DiagnosisReport.partial("LLM 输出非合法 JSON")
    # 兜底：缺 subgraph_ref/route_version 时从 state 透传
    data.setdefault("subgraph_ref", state.get("subgraph_ref") or "")
    if not data.get("route_version"):
        data["route_version"] = state.get("route_version")
    try:
        return DiagnosisReport.model_validate(data)
    except Exception as e:
        return DiagnosisReport.partial(f"DiagnosisReport 校验失败: {e}")
