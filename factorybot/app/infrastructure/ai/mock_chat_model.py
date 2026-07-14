"""MockChatModel：确定性 LLM 替身，离线驱动 ReAct + 结构化输出。

无 API Key 时由 llm_factory 选用。按 system prompt 识别"能力"，按对话步数推进：
- L1 诊断：query_traceability_graph -> query_pass_records -> 输出 DiagnosisReport
- root_cause (A)：query_stencil_lending -> 输出根因 + 处置卡
- fault_impact (B)：输出隔离范围
- draft (D)：输出草稿/动作卡
真实模型经 LangChainChatModel 适配，接口一致。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.infrastructure.ai.base import ModelResponse, ToolCall


class MockChatModel:
    name = "mock-llm"

    async def ainvoke(
        self, messages: list[dict], tools: Optional[list[dict]] = None,
    ) -> ModelResponse:
        sys_text = _find(messages, "system") or ""
        user_text = _find(messages, "user") or ""
        tool_results = [m for m in messages if m.get("role") == "tool"]
        step = len(tool_results)

        if "钢网/程序核对异常" in sys_text:
            return self._root_cause(user_text, tool_results, step)
        if "故障隔离" in sys_text or "FaultImpact" in sys_text:
            return self._fault_impact(user_text, step)
        if "客诉追溯" in sys_text or "TraceabilityAgent" in sys_text:
            return self._traceability(user_text, step)
        if "草拟" in sys_text or "draft" in sys_text.lower():
            return self._draft(sys_text, user_text, step)
        if "5M1E" in sys_text or "根因诊断助手" in sys_text:
            return self._l1_diagnosis(user_text, tool_results, step)
        # 兜底
        return ModelResponse(content=json.dumps({"status": "ok"}, ensure_ascii=False))

    async def ainvoke_structured(self, messages: list[dict], schema: type) -> Any:
        sys_text = _find(messages, "system") or ""
        user_text = _find(messages, "user") or ""
        # L2 Draft 草稿
        from app.domain.draft import Draft, DraftKind
        if schema is Draft or getattr(schema, "__name__", "") == "Draft":
            return self._build_draft(sys_text, user_text)
        # 兜底：尽力构造 schema
        return self._construct_default(schema)

    # ---- L1 诊断 ----
    def _l1_diagnosis(self, user_text: str, tool_results: list[dict], step: int) -> ModelResponse:
        sn = _extract(r"SN-[A-Za-z0-9-]+", user_text) or "SN-2026-001234"
        if step == 0:
            return ModelResponse(
                tool_calls=[ToolCall(id=f"call_mock_{step}", name="query_traceability_graph", args={"serial_no": sn})],
                finish_reason="tool_calls",
            )
        if step == 1:
            return ModelResponse(
                tool_calls=[ToolCall(id=f"call_mock_{step}", name="query_pass_records", args={"serial_no": sn})],
                finish_reason="tool_calls",
            )
        # 收集 trace_id 作为证据
        tids = []
        for m in tool_results:
            try:
                payload = json.loads(m.get("content", ""))
                if "trace_id" in payload:
                    tids.append(payload["trace_id"])
            except (json.JSONDecodeError, TypeError):
                pass
        if not tids:
            tids = ["T-101", "T-102"]
        report = {
            "summary": f"单件 {sn} 焊接不良（锡少/虚焊），5M1E 分析：料(锡膏批次)与机(贴装设备)双嫌疑。",
            "confidence": 0.72,
            "hypotheses": [
                {
                    "category": "Material", "rank": 1,
                    "statement": "锡膏批次 B-2026-0701 可能回温/粘度异常致锡少",
                    "evidence": [f"trace_id={tids[0]}"],
                    "suggested_action": "抽测锡膏粘度，核对回温记录与钢网开口",
                },
                {
                    "category": "Machine", "rank": 2,
                    "statement": "贴片机 ASSET-01 贴装压力可能漂移",
                    "evidence": [f"trace_id={tids[-1]}"],
                    "suggested_action": "核对设备遥测 placement_pressure 时序，必要时复校",
                },
            ],
            "subgraph_ref": "SUB-A1",
            "version": "v4",
            "version_kind": "route",
            "version_ref_id": "RR-B",
            "evidence_refs": [f"trace_id={t}" for t in tids],
            "needs_human_review": False,
        }
        return ModelResponse(content=json.dumps(report, ensure_ascii=False))

    # ---- A 根因 ----
    def _root_cause(self, user_text: str, tool_results: list[dict], step: int) -> ModelResponse:
        actual = _extract_kv(user_text, "actual") or "ST-A"
        if step == 0:
            return ModelResponse(
                tool_calls=[ToolCall(id=f"call_mock_{step}", name="query_stencil_lending", args={"stencil_id": actual})],
                finish_reason="tool_calls",
            )
        result = {
            "hypothesis": {
                "root_cause": f"上工单收线未归还钢网 {actual}，本工单误用旧钢网",
                "evidence": ["trace_id=T-rc1"],
            },
            "confidence": "high",
            "disposition_card": {
                "intent": f"归还钢网 {actual} 并换上正确钢网 ST-B 后重检",
                "route_to": "u_zhang(线长)",
                "suggested_actions": ["归还 ST-A", "借出 ST-B", "重跑钢网核对"],
            },
        }
        return ModelResponse(content=json.dumps(result, ensure_ascii=False))

    # ---- B 故障隔离 ----
    def _fault_impact(self, user_text: str, step: int) -> ModelResponse:
        result = {
            "hypothesis": {
                "fault_mode": "软漂移",
                "drift_window": ["2026-07-12T06:00:00Z", "2026-07-12T14:30:00Z"],
                "isolation_set": ["B-501", "B-502"],
                "release": ["B-503"],
                "sensitivity_reason": "0201 细间距对贴装压力敏感",
            },
            "confidence": "high",
            "disposition_card": {
                "intent": "隔离 B-501/B-502，放行 B-503",
                "route_to": "u_wang(质量)",
                "suggested_actions": ["隔离 B-501/B-502", "B-503 复检放行"],
            },
        }
        return ModelResponse(content=json.dumps(result, ensure_ascii=False))

    # ---- C 客诉追溯 ----
    def _traceability(self, user_text: str, step: int) -> ModelResponse:
        result = {
            "hypothesis": {
                "hypotheses": [
                    {"cause": "料-锡膏 B-2026-0701", "confidence": "high"},
                    {"cause": "机-设备漂移", "confidence": "medium"},
                ],
                "evidence_chain": ["trace_id=T-c1", "trace_id=T-c2"],
            },
            "confidence": "high",
        }
        return ModelResponse(content=json.dumps(result, ensure_ascii=False))

    # ---- D 草拟（L3 agent 路径，返回动作卡 JSON）----
    def _draft(self, sys_text: str, user_text: str, step: int) -> ModelResponse:
        if "8D" in sys_text or "8d" in sys_text:
            payload = {
                "intent": "草拟客诉 8D 报告",
                "draft_payload": {
                    "problem": "0201 细间距贴装偏移致焊接不良",
                    "root_cause": "贴装压力软漂移 + 锡膏粘度异常",
                    "containment": "隔离受影响批次 B-501/B-502",
                    "corrective": "复校压力传感器 + 锡膏粘度抽检",
                },
                "confidence": "high",
            }
        elif "SOP" in sys_text or "sop" in sys_text:
            payload = {
                "intent": "基于工艺升版草拟新 SOP",
                "draft_payload": {
                    "steps": ["OP-SPI", "OP-SMT", "OP-REFLOW", "OP-AOI"],
                    "params": {"zone3_temp": 248},
                    "version": "v5",
                },
                "confidence": "medium",
            }
        else:
            payload = {
                "intent": "草拟返工工艺建议",
                "draft_payload": {"rework_route": "RR-RW-1", "reentry_point": "OP-REFLOW"},
                "confidence": "medium",
            }
        return ModelResponse(content=json.dumps(payload, ensure_ascii=False))

    # ---- L2 Draft 结构化输出 ----
    def _build_draft(self, sys_text: str, user_text: str):
        from app.domain.draft import Draft, DraftKind
        wo = _extract_kv(user_text, "source_work_order_id") or "WO-2026-0701"
        sn = _extract(r"SN-[A-Za-z0-9-]+", user_text) or "SN-2026-001234"
        if "返工" in sys_text or "REWORK" in sys_text:
            return Draft(
                draft_kind=DraftKind.REWORK_ORDER,
                intent=f"对焊接不良单件 {sn} 返工，再入点 OP-REFLOW",
                payload={
                    "source_work_order_id": wo,
                    "affected_sn_list": [sn],
                    "reentry_point": "OP-REFLOW",
                    "rework_route_ref": "RR-RW-1",
                },
                confidence=0.7,
            )
        if "8D" in sys_text or "8d" in sys_text:
            return Draft(
                draft_kind=DraftKind.EIGHT_D,
                intent="草拟客诉 8D 报告",
                payload={
                    "问题描述": "0201 细间距贴装偏移致焊接不良",
                    "根因": "贴装压力软漂移 + 锡膏粘度异常",
                    "containment": "隔离受影响批次",
                    "纠正措施": "复校压力传感器 + 锡膏粘度抽检",
                },
                confidence=0.7,
            )
        return Draft(
            draft_kind=DraftKind.SOP,
            intent="基于工艺升版草拟新 SOP",
            payload={"steps": ["OP-SPI", "OP-SMT", "OP-REFLOW", "OP-AOI"],
                     "params": {"zone3_temp": 248}, "version": "v5"},
            confidence=0.7,
        )

    def _construct_default(self, schema: type) -> Any:
        try:
            return schema()  # 依赖 schema 有默认值
        except Exception:
            return None


# ---- 工具函数 ----
def _find(messages: list[dict], role: str) -> Optional[str]:
    for m in messages:
        if m.get("role") == role:
            return m.get("content", "")
    return None


def _extract(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(0) if m else None


def _extract_kv(text: str, key: str) -> Optional[str]:
    m = re.search(rf"{key}\s*[=:]\s*([A-Za-z0-9_\-]+)", text)
    return m.group(1) if m else None
