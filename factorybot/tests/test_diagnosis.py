"""诊断端到端：mock LLM 驱动 ReAct -> 5M1E 报告。

期望：query_traceability_graph -> query_pass_records -> DiagnosisReport，
subgraph_ref=SUB-A1, version=v4 (route), 假设 evidence 非空。
"""
import pytest

from app.container import get_container


@pytest.mark.asyncio
async def test_diagnosis_produces_5m1e_report():
    c = get_container()
    tenant = c.default_tenant()
    report = await c.diagnosis_service.diagnose(
        "单件 SN-2026-001234 焊接不良根因", tenant, serial_no="SN-2026-001234",
    )
    assert report.subgraph_ref == "SUB-A1"
    assert report.version == "v4"
    assert report.version_kind == "route"
    assert report.version_ref_id == "RR-B"
    assert len(report.hypotheses) >= 1
    # 每条假设必须引用证据（红线：不得编造）
    assert all(h.evidence for h in report.hypotheses)
    # 工具调用痕迹落 tool_call_trace
    traces = await c.tool_trace_repo.list_for_session(report.subgraph_ref) \
        if hasattr(c.tool_trace_repo, "list_for_session") else []
    # session_id 不等于 subgraph_ref；这里只校验报告结构


@pytest.mark.asyncio
async def test_diagnosis_tool_call_trace_recorded():
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


@pytest.mark.asyncio
async def test_diagnosis_unknown_serial_returns_empty_not_default_impersonation():
    """未知 serial_no 不应回退到 _default 冒充 SN-DEFAULT，应返回空结果（数据诚实）。

    这是 诊断 幻觉根因的回归保护：mock 读在 key 未命中时返回空，而非另一实体的占位数据，
    让 LLM 看到"证据为空"而非"良性数据"，配合 prompt 规则 6 拒答而非编造。
    """
    c = get_container()
    tenant = c.default_tenant()
    unknown = "Q123-UNKNOWN"

    # 追溯图：空，不冒充 _default 的 SN-DEFAULT / 3 个节点
    g = await c.acl.rag.query_traceability_graph(unknown, tenant)
    assert g.nodes == []
    assert g.serial_no == ""
    assert g.subgraph_ref == ""

    # 过点记录：空列表，不冒充 _default 的 PASS 记录
    pr = await c.acl.pass_execution.query_pass_records(unknown, tenant)
    pr_list = pr.records if hasattr(pr, "records") else pr
    assert pr_list == []

    # 其他按 serial_no 查的只读工具同样不应冒充（返回空视图 / 空列表）
    repair = await c.acl.rework.query_repair_history(unknown, tenant)
    assert repair.repairs == []
