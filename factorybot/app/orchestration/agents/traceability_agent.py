"""能力 C：客诉根因追溯，复用 诊断图（5M1E 假设排序 + 证据链）。

版本钉死由 ACL 代码做（route_version 强制过滤），agent 只消费钉死后的版本。
"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.ai.graph_builder import build_diagnosis_graph


class TraceabilityAgent:
    CAPABILITY = "traceability"

    def __init__(self, llm, diagnosis_registry, trace_repo, obs) -> None:
        self._llm = llm
        self._diagnosis_registry = diagnosis_registry
        self._trace_repo = trace_repo
        self._obs = obs
        self._graph = build_diagnosis_graph(
            llm, diagnosis_registry, trace_repo, obs, capability="diagnosis",
        )

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        tenant = state.get("tenant")
        if isinstance(tenant, dict):
            tenant = TenantContext.model_validate(tenant)
        serial_no = state.get("complaint_batch_id") or state.get("batch_id") or state.get("serial_no") or ""
        initial = {
            "tenant": tenant, "obs_ctx": state.get("obs_ctx"),
            "question": f"客诉批次 {serial_no} 根因追溯（5M1E）",
            "serial_no": serial_no, "step_no": 0,
            "messages": [], "pending_tool_calls": [],
        }
        final = await self._graph.ainvoke(initial, config=config or {})
        report = final.get("report") or {}
        return {
            "hypothesis": report,
            "confidence": "high" if report.get("confidence", 0) >= 0.7 else "medium",
        }
