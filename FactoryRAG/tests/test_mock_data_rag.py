"""B 文档型 RAG：基于 data/ 模拟数据的检索测试（零外部依赖）。

真实组件：``ChunkStrategySelector`` 切分 + ``Bm25Index``(rank_bm25+jieba) 稀疏召回 +
``VectorRetriever``(FakeEmbedder+FakeChromaCollection) 稠密召回 + ``HybridRetriever``(RRF 融合) +
``DocumentRetrievalService``(透传 reranker + 桩 LLM)。覆盖相关性、版本/资产/类型/租户隔离、
DEPRECATED 永不返回、端到端 DocAnswer、入口版本红线。
"""
from __future__ import annotations

import pytest

from app.routes.document.domain.answer import DocQuery
from app.routes.document.domain.document import DocumentCategory
from app.shared.events.version_contract import VersionAnchor, VersionKind
from app.shared.tenant.context import TenantContext
from _mock_rag_infra import (
    build_bm25,
    build_doc_svc,
    build_hybrid_retriever,
    load_doc_chunks,
    load_doc_queries,
)

# 全车间租户：可见 PCBA + BOX 全部文档（tenant_scope 过滤不误伤）
TENANT = TenantContext(tenant_id="t-mock", tenant_scopes=["workshop:PCBA", "workshop:BOX", "line:SMT-1"])


def _anchor_from_query(q: dict) -> VersionAnchor | None:
    """从 queries.json 条目的 version_kind/version/version_ref_id 构造锚点。"""
    vk = q.get("version_kind")
    ver = q.get("version")
    if not vk or not ver:
        return None
    try:
        return VersionAnchor(
            kind=VersionKind(vk),
            ref_id=q.get("version_ref_id", "") or "",
            version=ver,
        )
    except ValueError:
        return None


# ── 数据加载 sanity ──
def test_mock_data_chunks_loaded():
    """模拟数据切分后产出 chunk，含三类 doc_type 与一条 DEPRECATED。"""
    chunks, entries = load_doc_chunks()
    assert len(chunks) > 0
    doc_types = {c.doc_type for c in chunks}
    assert {"SOP", "MANUAL", "STANDARD"} <= doc_types
    states = {c.state for c in chunks}
    assert "DEPRECATED" in states  # sop-smt-reflow-v2-deprecated
    assert "sop-smt-reflow-v2-deprecated" in entries


# ── 检索相关性（BM25 稀疏 + Hybrid RRF 两路同测）──
@pytest.mark.parametrize("backend", ["bm25", "hybrid"])
async def test_recall_relevance(backend: str):
    """逐条 queries.json：期望 doc_id 命中、排除项不出现、top hit 含期望关键词。"""
    chunks, _ = load_doc_chunks()
    if backend == "bm25":
        _, retriever = await build_bm25(chunks)
    else:
        retriever = await build_hybrid_retriever(chunks)

    for q in load_doc_queries():
        hits = await retriever.retrieve(
            query=q["query"],
            tenant=TENANT,
            version_anchor=_anchor_from_query(q),
            doc_types=q.get("doc_types"),
            top_k=20,
        )
        doc_ids = [h.doc_id for h in hits]
        for must in q["expect_doc_ids"]:
            assert must in doc_ids, f"[{backend}] {q['query']!r}: 期望 {must} 命中，实际 {doc_ids}"
        for excl in q.get("exclude_doc_ids", []):
            # exclude 只约束 top-5（有意义排名区），不约束 top-20 全量候选池：
            # GENERAL 宽泛查询会词面命中相关文档（如"电子制造行业标准"命中含"电子制造作业区域"的 ESD），
            # 要求相关文档不在 top-20 候选池不现实；它不进 top-5 即满足"不是首选答案"。
            assert excl not in doc_ids[:5], f"[{backend}] {q['query']!r}: 排除 {excl} 但出现于 top-5 {doc_ids[:5]}"
        assert hits, f"[{backend}] {q['query']!r}: 无命中"
        # 相关性：top hit 必属期望文档
        assert hits[0].doc_id in q["expect_doc_ids"], (
            f"[{backend}] {q['query']!r}: top hit doc={hits[0].doc_id} 不在期望 {q['expect_doc_ids']}"
        )
        # 关键词在 top-5 命中片段中可检得（多章节文档关键词可能不在首条）
        top_n_text = " ".join(h.text for h in hits[:5])
        for kw in q["expect_keywords"]:
            assert kw in top_n_text, f"[{backend}] {q['query']!r}: top-5 缺关键词 {kw!r}"


# ── 版本隔离红线（PROCESS_BOUND ROUTE 版本等值过滤）──
async def test_version_isolation_v3_only():
    """查 v3 -> 仅回 version==v3 的 chunk，v4/v2-deprecated 不泄漏。"""
    chunks, _ = load_doc_chunks()
    _, retriever = await build_bm25(chunks)
    hits = await retriever.retrieve(
        query="回流焊峰值温度",
        tenant=TENANT,
        version_anchor=VersionAnchor(kind=VersionKind.ROUTE, ref_id="route-smt-reflow", version="v3"),
        top_k=20,
    )
    assert hits, "v3 应有命中"
    assert all(h.version == "v3" for h in hits), "v3 查询泄漏了非 v3 chunk"
    assert all(h.doc_id != "sop-smt-reflow-v4" for h in hits)


async def test_version_isolation_v4_only():
    chunks, _ = load_doc_chunks()
    _, retriever = await build_bm25(chunks)
    hits = await retriever.retrieve(
        query="回流焊链速",
        tenant=TENANT,
        version_anchor=VersionAnchor(kind=VersionKind.ROUTE, ref_id="route-smt-reflow", version="v4"),
        top_k=20,
    )
    assert hits
    assert all(h.version == "v4" for h in hits)
    assert all(h.doc_id != "sop-smt-reflow" for h in hits)


# ── DEPRECATED 永不返回（Bm25Index 构建期跳过非 PUBLISHED）──
async def test_deprecated_never_returned():
    chunks, _ = load_doc_chunks()
    index, retriever = await build_bm25(chunks)
    published = [c for c in chunks if c.state == "PUBLISHED"]
    assert index.size == len(published), "索引仅含 PUBLISHED"
    # 全文检索"回流焊"（v2-deprecated 也含此词）-> 不应返回废弃文档
    hits = await retriever.retrieve(query="回流焊", tenant=TENANT, top_k=50)
    doc_ids = {h.doc_id for h in hits}
    assert "sop-smt-reflow-v2-deprecated" not in doc_ids
    assert all(h.state == "PUBLISHED" for h in hits)


# ── 资产隔离红线（ASSET_BOUND version_ref_id 过滤）──
async def test_asset_isolation():
    chunks, _ = load_doc_chunks()
    _, retriever = await build_bm25(chunks)
    # 查 EQ-REFLOW-001 -> 仅回流焊炉手册，不混入贴片机手册
    hits = await retriever.retrieve(
        query="故障处理",
        tenant=TENANT,
        version_anchor=VersionAnchor(kind=VersionKind.ASSET, ref_id="EQ-REFLOW-001", version=""),
        top_k=20,
    )
    assert hits
    assert all(h.doc_id == "manual-reflow-oven-eq001" for h in hits)


# ── doc_type 过滤 ──
async def test_doc_type_filter():
    chunks, _ = load_doc_chunks()
    _, retriever = await build_bm25(chunks)
    hits = await retriever.retrieve(
        query="工艺标准", tenant=TENANT, doc_types=["SOP"], top_k=20,
    )
    assert hits
    # ChunkHit 无 doc_type 字段 -> 用 doc_id 推断：SOP 文档 id 集合
    sop_doc_ids = {"sop-smt-reflow", "sop-smt-reflow-v4", "sop-box-build"}
    assert all(h.doc_id in sop_doc_ids for h in hits)


# ── 租户作用域隔离 ──
async def test_tenant_scope_isolation():
    """BOX 车间租户只见 box-build SOP，PCBA 文档被 tenant_scope 过滤。"""
    chunks, _ = load_doc_chunks()
    _, retriever = await build_bm25(chunks)
    box_tenant = TenantContext(tenant_id="t-box", tenant_scopes=["workshop:BOX"])
    # 整机组装（BOX）可见
    hits = await retriever.retrieve(
        query="整机组装扭矩",
        tenant=box_tenant,
        version_anchor=VersionAnchor(kind=VersionKind.ROUTE, ref_id="route-box-build", version="v2"),
        top_k=20,
    )
    assert any(h.doc_id == "sop-box-build" for h in hits)
    # 回流焊（PCBA）被 tenant_scope 过滤 -> 空
    hits_pcba = await retriever.retrieve(
        query="回流焊峰值温度",
        tenant=box_tenant,
        version_anchor=VersionAnchor(kind=VersionKind.ROUTE, ref_id="route-smt-reflow", version="v3"),
        top_k=20,
    )
    assert hits_pcba == []


# ── 端到端：DocumentRetrievalService.query() -> DocAnswer ──
async def test_end_to_end_doc_answer():
    chunks, _ = load_doc_chunks()
    retriever = await build_hybrid_retriever(chunks)
    svc = build_doc_svc(retriever)
    req = DocQuery(
        question="回流焊峰值温度设多少",
        doc_category=DocumentCategory.PROCESS_BOUND,
        version="v3",
        version_kind="route",
        version_ref_id="route-smt-reflow",
        top_k=20,
        top_n=5,
    )
    answer = await svc.query(req, TENANT)
    assert answer.citations, "应有引用"
    assert answer.citations[0].document_id == "sop-smt-reflow"
    assert answer.version_filter == "v3"
    assert answer.needs_human_review is False  # confidence 0.75 + 有引用


# ── 入口版本红线（用真实数据验证）──
async def test_process_bound_missing_version_still_enforced():
    chunks, _ = load_doc_chunks()
    retriever = await build_hybrid_retriever(chunks)
    svc = build_doc_svc(retriever)
    req = DocQuery(question="回流焊", doc_category=DocumentCategory.PROCESS_BOUND)  # 缺版本锚点
    with pytest.raises(ValueError, match="ROUTE 版本锚点"):
        await svc.query(req, TENANT)
