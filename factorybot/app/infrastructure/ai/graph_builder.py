"""诊断图配置：系统提示 / 首轮 user / 终止收尾（parse + 幻觉护栏）。

图骨架委托 app.infrastructure.ai.react_graph.build_react_graph；本模块只持有 诊断 专属的
提示词与收尾逻辑。_guard_no_evidence 作为 diagnosis_finalize 的一部分，是 诊断/C 专属护栏，
不下沉通用基座（draft 类开放生成 agent 不应被误伤）。

recursion_limit=20 硬上限靠框架兜底；DiagnosisService 外层再加 asyncio.wait_for 整体超时。
"""
from __future__ import annotations

import json
import operator
import re
from typing import Annotated, Any, Optional, TypedDict

from app.domain.report import DiagnosisReport
from app.domain.tool import ToolRegistry
from app.infrastructure.ai.react_graph import build_react_graph
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
    # 版本一致性三段链第二段：物理锁定的版本锚点（扁平三字段）
    version: Optional[str]
    version_kind: Optional[str]
    version_ref_id: Optional[str]
    subgraph_ref: Optional[str]
    step_no: int
    report: Optional[dict]


DIAGNOSIS_SYSTEM_PROMPT = (
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
    "   - subgraph_ref/version/version_kind/version_ref_id/evidence_refs: 从工具结果透传\n"
    "   - needs_human_review: 布尔\n"
    "   示例: {\"summary\":\"...\",\"confidence\":0.72,\"hypotheses\":[{\"category\":\"Material\","
    "\"rank\":1,\"statement\":\"锡膏批次异常\",\"evidence\":[\"trace_id=T-101\"],"
    "\"suggested_action\":\"抽测锡膏粘度\"}],\"subgraph_ref\":\"SUB-A1\","
    "\"version\":\"v4\",\"version_kind\":\"route\",\"version_ref_id\":\"RR-B\","
    "\"evidence_refs\":[\"trace_id=T-101\"],\"needs_human_review\":false}\n"
    "6. 证据优先，禁止从问题文本反推不良：若工具返回的追溯图无 BLOCK/不良节点、过点记录均为 PASS、"
    "无测试 FAIL 等异常证据，则 summary 必须写\"证据不足，未发现异常\"，hypotheses 置空数组 []，"
    "confidence=0.0，needs_human_review=true。严禁依据 question 中出现的\"焊接不良\"等词假设不良存在，"
    "严禁编造 serial_no/批次号/设备号等工具未返回的具体值。"
)


def build_diagnosis_graph(
    llm, registry: ToolRegistry, trace_repo: ToolCallTraceRepo, obs=None,
    capability: str = "diagnosis", recursion_limit: int = 20,
    result_compactor=None,
):
    """构建 诊断图：build_react_graph + 诊断 专属钩子（prompt/user/finalize）。

    诊断（DiagnosisService）与 C（TraceabilityAgent）共用此入口，共享 diagnosis_finalize
    （含 _guard_no_evidence 护栏）。无 checkpointer，同步跑完。
    """
    return build_react_graph(
        llm, registry, trace_repo, obs,
        capability=capability,
        prompt_fn=diagnosis_prompt,
        user_prompt_fn=diagnosis_user_prompt,
        finalize_fn=diagnosis_finalize,
        state_schema=AgentState,
        recursion_limit=recursion_limit,
        result_compactor=result_compactor,
    )


# ---- 诊断 钩子（注入 build_react_graph）----

def diagnosis_prompt(state) -> str:
    return DIAGNOSIS_SYSTEM_PROMPT


def diagnosis_user_prompt(state) -> str:
    return _build_user_prompt(state)


def diagnosis_finalize(content: str, state) -> dict:
    """终止步：解析 DiagnosisReport + 幻觉护栏，返回 {"report": ...}。"""
    report = _parse_report(content, state)
    report = _guard_no_evidence(report, state)
    return {"report": report.model_dump()}


# ---- 诊断 专属实现：提示构造 / 报告解析 / 护栏 ----

def _build_user_prompt(state: AgentState) -> str:
    q = state.get("question", "")
    parts = [f"问题：{q}"]
    if state.get("serial_no"):
        parts.append(f"单件 serial_no={state['serial_no']}")
    if state.get("work_order_id"):
        parts.append(f"工单 work_order_id={state['work_order_id']}")
    if state.get("version"):
        kind = state.get("version_kind") or "route"
        parts.append(f"版本锚点 version={state['version']} (kind={kind})")
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
    # 兜底：缺 subgraph_ref/版本锚点 时从 state 透传
    data.setdefault("subgraph_ref", state.get("subgraph_ref") or "")
    if not data.get("version"):
        data["version"] = state.get("version")
        data["version_kind"] = state.get("version_kind")
        data["version_ref_id"] = state.get("version_ref_id")
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
