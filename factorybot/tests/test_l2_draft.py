"""L2 草稿端到端：L1 报告 -> 返工单草稿。requires_confirmation 恒 True，route_version 透传。"""
import pytest

from app.container import get_container
from app.domain.draft import DraftKind
from app.domain.report import DiagnosisReport, FiveM1ECategory, Hypothesis


def _fake_report() -> DiagnosisReport:
    return DiagnosisReport(
        summary="SN-2026-001234 焊接不良",
        confidence=0.72,
        hypotheses=[
            Hypothesis(category=FiveM1ECategory.MATERIAL, rank=1,
                       statement="锡膏批次异常", evidence=["trace_id=T-101"]),
        ],
        subgraph_ref="SUB-A1",
        route_version="v4",
        evidence_refs=["trace_id=T-101"],
    )


@pytest.mark.asyncio
async def test_l2_rework_order_draft():
    c = get_container()
    tenant = c.default_tenant()
    draft = await c.draft_service.draft(_fake_report(), DraftKind.REWORK_ORDER, tenant)
    assert draft.draft_kind == DraftKind.REWORK_ORDER
    assert draft.requires_confirmation is True          # L2 不变式
    assert draft.route_version == "v4"                  # 版本一致性三段链第三段
    assert draft.draft_id.startswith("D-")
    assert "source_work_order_id" in draft.payload


@pytest.mark.asyncio
async def test_l2_no_write_client_gate():
    """启动断言：L2 持有的 ACL client 无写动词方法。"""
    from app.domain.gate import assert_no_write_clients
    c = get_container()
    # L2 用到的只读 ACL client
    l2_clients = [c.acl.rag, c.acl.doc_rag, c.acl.process]
    assert_no_write_clients(l2_clients)  # 不抛异常即通过


@pytest.mark.asyncio
async def test_l2_evidence_retrieval():
    c = get_container()
    tenant = c.default_tenant()
    draft = await c.draft_service.draft(_fake_report(), DraftKind.EIGHT_D, tenant)
    evidence = await c.draft_service.get_evidence(draft.draft_id)
    assert any("subgraph_ref=SUB-A1" in e for e in evidence)
