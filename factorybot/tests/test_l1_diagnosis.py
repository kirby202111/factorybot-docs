"""L1 诊断端到端：mock LLM 驱动 ReAct -> 5M1E 报告。

期望：query_traceability_graph -> query_pass_records -> DiagnosisReport，
subgraph_ref=SUB-A1, route_version=v4, 假设 evidence 非空。
"""
import pytest

from app.container import get_container


@pytest.mark.asyncio
async def test_l1_diagnosis_produces_5m1e_report():
    c = get_container()
    tenant = c.default_tenant()
    report = await c.diagnosis_service.diagnose(
        "单件 SN-2026-001234 焊接不良根因", tenant, serial_no="SN-2026-001234",
    )
    assert report.subgraph_ref == "SUB-A1"
    assert report.route_version == "v4"
    assert len(report.hypotheses) >= 1
    # 每条假设必须引用证据（红线：不得编造）
    assert all(h.evidence for h in report.hypotheses)
    # 工具调用痕迹落 tool_call_trace
    traces = await c.tool_trace_repo.list_for_session(report.subgraph_ref) \
        if hasattr(c.tool_trace_repo, "list_for_session") else []
    # session_id 不等于 subgraph_ref；这里只校验报告结构


@pytest.mark.asyncio
async def test_l1_tool_call_trace_recorded():
    c = get_container()
    tenant = c.default_tenant()
    report = await c.diagnosis_service.diagnose(
        "SN-2026-001234 焊接不良", tenant, serial_no="SN-2026-001234",
    )
    # 至少调了图 + 过点记录两个工具
    all_traces = c.tool_trace_repo._rows
    tool_names = {t.tool_name for t in all_traces}
    assert "query_traceability_graph" in tool_names
    assert "query_pass_records" in tool_names
