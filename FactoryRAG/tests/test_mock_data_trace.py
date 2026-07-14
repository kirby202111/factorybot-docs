"""A 追溯型 RAG：基于 data/trace/ 模拟数据的检索测试（零外部依赖）。

真实组件：``TraceRetrievalService`` + 真实 ``SeedResolver``（正则路径）+ ``FakeGraphRetriever``
（按 seed 返回 mock ``TraceSubgraph``）+ 桩 LLM（从子图抽真实 node_id 作证据）。覆盖：
版本快照锁定（非当前 ACTIVE）、禁实体幻觉、低置信转人工、图内版本隔离、
A->B 跨路线富化（版本一致性三段链第一段：图锁 v3 -> B 仅召回 v3 SOP）。
"""
from __future__ import annotations

import pytest

from app.routes.traceability.domain.seed import ExpandRequest, Seed, SeedKind, TraceQuery
from app.shared.tenant.context import TenantContext
from _mock_rag_infra import (
    FakeGraphRetriever,
    StubTraceLLM,
    build_doc_rag_port,
    build_trace_svc,
    load_trace_scenarios,
)

TENANT = TenantContext(tenant_id="t-mock", tenant_scopes=["workshop:PCBA", "line:SMT-1"])


def _all_node_ids(subgraph) -> set[str]:
    ids = {subgraph.seed.node_id}
    for cluster in (
        subgraph.clusters.man, subgraph.clusters.machine, subgraph.clusters.material,
        subgraph.clusters.method, subgraph.clusters.measurement, subgraph.clusters.environment,
    ):
        ids.update(n.node_id for n in cluster)
    return ids


# ── Seed 解析（真实 SeedResolver 正则路径，不触 Neo4j/Embedding/LLM）──
async def test_seed_resolver_regex():
    from app.routes.traceability.application.seed_resolver import SeedResolver
    from unittest.mock import MagicMock

    resolver = SeedResolver(llm=MagicMock(), embedder=MagicMock(), driver=MagicMock())
    assert await resolver.resolve("SN-2024-001 出现锡桥", TENANT) == Seed(kind=SeedKind.WIP_UNIT, value="SN-2024-001")
    assert await resolver.resolve("WO-2024-001 有缺陷", TENANT) == Seed(kind=SeedKind.WORK_ORDER, value="WO-2024-001")
    assert await resolver.resolve("批次 B7777 异常", TENANT) == Seed(kind=SeedKind.INVENTORY_BATCH, value="B7777")


# ── 版本快照锁定：取生产时快照版本，非当前 ACTIVE ──
async def test_trace_query_locks_snapshot_version():
    """SN-2024-001 的图快照锁定 v3（snapshot 节点 status=DEPRECATED，但仍是生产时锁定版本）。"""
    svc = build_trace_svc(retriever=FakeGraphRetriever())
    answer = await svc.query(
        TraceQuery(question="SN-2024-001 锡桥根因", seed=Seed(kind=SeedKind.WIP_UNIT, value="SN-2024-001")),
        TENANT,
    )
    assert answer.version == "v3"
    assert answer.version_kind == "route"
    assert answer.subgraph_ref.startswith("WipUnit:SN-2024-001@")
    # 显式断言：锁定的 v3 来自 status=DEPRECATED 的历史快照节点（非当前 ACTIVE）
    sub = await svc.expand_subgraph(
        ExpandRequest(kind=SeedKind.WIP_UNIT, value="SN-2024-001"), TENANT,
    )
    rv_nodes = [n for n in sub.clusters.method if n.label == "RouteVersion"]
    assert rv_nodes, "Method 维度应有 RouteVersion 快照节点"
    assert any(n.props.get("status") == "DEPRECATED" and n.props.get("route_version") == "v3"
               for n in rv_nodes)


# ── 禁实体幻觉：假设证据必须引用子图真实 node_id ──
async def test_hypotheses_carry_real_evidence():
    svc = build_trace_svc(retriever=FakeGraphRetriever())
    answer = await svc.query(
        TraceQuery(question="SN-2024-001 锡桥根因", seed=Seed(kind=SeedKind.WIP_UNIT, value="SN-2024-001")),
        TENANT,
    )
    assert answer.hypotheses, "应有根因假设"
    sub = await svc.expand_subgraph(
        ExpandRequest(kind=SeedKind.WIP_UNIT, value="SN-2024-001"), TENANT,
    )
    real_ids = _all_node_ids(sub)
    for h in answer.hypotheses:
        for ev in h.evidence:
            if ev.startswith("node_id="):
                nid = ev.split("=", 1)[1]
                assert nid in real_ids, f"假设证据引用了不存在的节点 {nid}（实体幻觉）"


# ── 低置信转人工 ──
async def test_low_confidence_human_review():
    svc = build_trace_svc(retriever=FakeGraphRetriever(), llm=StubTraceLLM(confidence=0.3))
    answer = await svc.query(
        TraceQuery(question="SN-2024-001 锡桥根因", seed=Seed(kind=SeedKind.WIP_UNIT, value="SN-2024-001")),
        TENANT,
    )
    assert answer.confidence < 0.6
    assert answer.needs_human_review is True


# ── 图内版本隔离：不同 SN 锁定不同工艺版本 ──
async def test_graph_version_isolation():
    svc = build_trace_svc(retriever=FakeGraphRetriever())
    sub_v3 = await svc.expand_subgraph(
        ExpandRequest(kind=SeedKind.WIP_UNIT, value="SN-2024-001"), TENANT,
    )
    sub_v4 = await svc.expand_subgraph(
        ExpandRequest(kind=SeedKind.WIP_UNIT, value="SN-2024-009"), TENANT,
    )
    assert sub_v3.version_locked().version == "v3"
    assert sub_v4.version_locked().version == "v4"


# ── A->B 跨路线：图锁 v3 -> B 仅召回 v3 SOP（版本一致性三段链第一段）──
async def test_cross_route_b_filters_by_locked_version():
    """直接打 B Port：ROUTE 锚点 v3 -> 引用 v3 文档（sop-smt-reflow），非 v4。"""
    doc_rag = await build_doc_rag_port()
    answer = await doc_rag.query(
        "回流焊锡桥处置 SOP", TENANT,
        version="v3", version_kind="route", version_ref_id="route-smt-reflow",
        doc_category="PROCESS_BOUND",
    )
    assert answer.citations, "B 应召回 v3 SOP"
    assert answer.citations[0].document_id == "sop-smt-reflow"
    assert all(c.document_id != "sop-smt-reflow-v4" for c in answer.citations)


async def test_a_enriches_suggested_action_from_b():
    """A 查询 SN-2024-001（锁 v3）-> Material 假设的 suggested_action 被 B 的 v3 SOP 富化。"""
    doc_rag = await build_doc_rag_port()
    svc = build_trace_svc(retriever=FakeGraphRetriever(), doc_rag=doc_rag)
    answer = await svc.query(
        TraceQuery(question="SN-2024-001 锡桥根因", seed=Seed(kind=SeedKind.WIP_UNIT, value="SN-2024-001")),
        TENANT,
    )
    assert answer.hypotheses
    action = answer.hypotheses[0].suggested_action
    assert "参考 SOP" in action, "suggested_action 应被 B 的 SOP 片段富化"
    # v3 标记出现（chunk 头含 "route v3" 或步骤含 "v3 温度"），v4 标记绝不出现（ROUTE 锚点 v3 过滤）
    assert "v3" in action
    assert "route v4" not in action
    assert "250" not in action  # v4 峰值温度，被 ROUTE 锚点 v3 过滤排除
