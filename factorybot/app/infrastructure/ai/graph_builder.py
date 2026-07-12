"""L1 诊断 ReAct 图构建：agent 节点 -> 工具节点 -> 回模型，条件边 + recursion_limit。

StateGraph 把"模型思考 -> 工具执行 -> 回模型"做成显式图，可对每条边加条件路由/超时/
递归上限。recursion_limit=20 是硬上限靠框架兜底。不引入 Celery/AgentExecutor。
"""
from __future__ import annotations

import json
import operator
import re
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.report import DiagnosisReport
from app.domain.tool import ToolRegistry
from app.infrastructure.ai.base import assistant_msg, sys_msg, user_msg
from app.infrastructure.ai.tool_node import ToolNode, tool_to_schema
from app.infrastructure.obs.logging import get_logger
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
    "5. 证据充分后输出严格遵循下述 JSON 结构，不要再调工具。字段约束：\n"
    "   - summary: 字符串，根因总结\n"
    "   - confidence: 0.0~1.0 浮点（如 0.72），禁止用 高/中/低 等文字\n"
    "   - hypotheses: 数组，每项必须含字段 category(枚举仅 Man|Machine|Material|Method|"
    "Measurement|Environment)、rank(整数,1=最可能)、statement(字符串)、evidence(字符串数组,"
    "每条引用 trace_id 如 trace_id=T-101,至少一条)、suggested_action(字符串)\n"
    "   - subgraph_ref/route_version/evidence_refs: 从工具结果透传\n"
    "   - needs_human_review: 布尔\n"
    "   示例: {\"summary\":\"...\",\"confidence\":0.72,\"hypotheses\":[{\"category\":\"Material\","
    "\"rank\":1,\"statement\":\"锡膏批次异常\",\"evidence\":[\"trace_id=T-101\"],"
    "\"suggested_action\":\"抽测锡膏粘度\"}],\"subgraph_ref\":\"SUB-A1\",\"route_version\":\"v4\","
    "\"evidence_refs\":[\"trace_id=T-101\"],\"needs_human_review\":false}\n"
    "6. 证据优先，禁止从问题文本反推不良：若工具返回的追溯图无 BLOCK/不良节点、过点记录均为 PASS、"
    "无测试 FAIL 等异常证据，则 summary 必须写\"证据不足，未发现异常\"，hypotheses 置空数组 []，"
    "confidence=0.0，needs_human_review=true。严禁依据 question 中出现的\"焊接不良\"等词假设不良存在，"
    "严禁编造 serial_no/批次号/设备号等工具未返回的具体值。"
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
        # 幻觉护栏：工具未返回任何不良证据时，强制"证据不足"，不信 LLM 编造
        report = _guard_no_evidence(report, state)
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
    data = _extract_json(content)
    if data is None:
        get_logger("diagnosis").warning(
            "llm.output.non_json", content_preview=content[:500]
        )
        return DiagnosisReport.partial("LLM 输出非合法 JSON")
    # 兜底：缺 subgraph_ref/route_version 时从 state 透传
    data.setdefault("subgraph_ref", state.get("subgraph_ref") or "")
    if not data.get("route_version"):
        data["route_version"] = state.get("route_version")
    try:
        return DiagnosisReport.model_validate(data)
    except Exception as e:
        return DiagnosisReport.partial(f"DiagnosisReport 校验失败: {e}")


def _extract_json(content: str) -> Optional[dict]:
    """从 LLM 输出提取 JSON 对象：兼容 markdown fence / 前后解释文本。"""
    if not content:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    # ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            pass
    # 首个平衡 {...}（模型常在 JSON 前后加解释文本）
    start = content.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(content[start : i + 1])
                    except (json.JSONDecodeError, TypeError):
                        return None
    return None


def _guard_no_evidence(report: DiagnosisReport, state: AgentState) -> DiagnosisReport:
    """确定性幻觉护栏：若工具未返回任何不良证据（追溯图为空 / 无 BLOCK / 无 FAIL），
    无论 LLM 输出什么，强制改为"证据不足"，防止从 question 反推编造具体不良。

    prompt 规则 6 已要求 LLM 自行拒答，但思考模型仍可能编造，此为代码级兜底。
    判据：扫描所有工具返回，未出现 BLOCK/FAIL/DEFECTIVE/不良数>0 即视为无证据。
    """
    if _has_defect_evidence(state.get("messages", [])):
        return report
    get_logger("diagnosis").warning(
        "llm.hallucination.guarded", summary_preview=report.summary[:120]
    )
    return DiagnosisReport(
        summary="证据不足：工具未返回任何不良证据（追溯图为空或过点记录均为 PASS），无法定位根因",
        confidence=0.0,
        hypotheses=[],
        subgraph_ref=state.get("subgraph_ref") or "",
        needs_human_review=True,
    )


def _has_defect_evidence(messages: list) -> bool:
    """扫描工具返回消息，判断是否存在不良证据（BLOCK / FAIL / DEFECTIVE / 不良数>0）。"""
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        try:
            payload = json.loads(m.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if _payload_has_defect(payload.get("data")):
            return True
    return False


def _payload_has_defect(data) -> bool:
    """data 可能是追溯图 dict、过点/测试结果列表、或单视图 dict。"""
    if isinstance(data, list):
        return any(_dict_has_defect(d) for d in data if isinstance(d, dict))
    if isinstance(data, dict):
        # 追溯图节点（CheckpointRecord.decision / TestResult.raw_verdict / QualityVerdict.verdict）
        for node in data.get("nodes", []) or []:
            if isinstance(node, dict) and _dict_has_defect(node.get("properties", {}) or {}):
                return True
        # 不良率视图
        if data.get("defective_units") and data["defective_units"] > 0:
            return True
        # 自身或 records/results/repairs/rework_orders 条目
        if _dict_has_defect(data):
            return True
        for key in ("records", "results", "repairs", "rework_orders"):
            for item in data.get(key, []) or []:
                if isinstance(item, dict) and _dict_has_defect(item):
                    return True
    return False


def _dict_has_defect(d: dict) -> bool:
    decision = str(d.get("decision", "")).upper()
    verdict = str(d.get("raw_verdict", "")).upper()
    qverdict = str(d.get("verdict", "")).upper()
    return decision == "BLOCK" or verdict == "FAIL" or qverdict == "DEFECTIVE"
