"""HybridRetriever：RRF 融合 + 过取 + 截断 + 单路降级。

用 fake 检索器（可控命中序列）验证融合数学与降级语义，不依赖 ChromaDB/Embedding。
"""
from __future__ import annotations

import pytest

from app.routes.document.application.hybrid_retriever import HybridRetriever
from app.routes.document.domain.answer import ChunkHit
from app.shared.tenant.context import TenantContext


def _hit(cid: str) -> ChunkHit:
    return ChunkHit(
        chunk_id=cid, doc_id="d", version_id="v", text=f"text-{cid}",
        locator={}, section_type="NOTE",
    )


class _FakeRetriever:
    """可控召回：记录调用 top_k，按序返回命中；可注入异常模拟路失败。"""

    def __init__(self, hits: list[ChunkHit], *, raise_exc: Exception | None = None) -> None:
        self._hits = hits
        self._raise = raise_exc
        self.last_top_k: int | None = None

    async def retrieve(
        self, *, query, tenant, version_anchor=None, doc_types=None, top_k=20,
    ) -> list[ChunkHit]:
        self.last_top_k = top_k
        if self._raise is not None:
            raise self._raise
        return list(self._hits)[:top_k]


async def test_rrf_fuses_both_channels_and_orders():
    """两路均命中的 chunk（d1/d2）RRF 分高于单路（d3/d4）；d2 双路 rank1/2 居首。"""
    dense = _FakeRetriever([_hit("d1"), _hit("d2"), _hit("d3")])
    sparse = _FakeRetriever([_hit("d2"), _hit("d4"), _hit("d1")])
    h = HybridRetriever(dense=dense, sparse=sparse, rrf_k=60, candidate_k=50)

    out = await h.retrieve(query="q", tenant=TenantContext(tenant_id="t1"), top_k=2)

    assert [o.chunk_id for o in out] == ["d2", "d1"]
    # d2 = 1/62 (dense rank2) + 1/61 (sparse rank1)
    assert out[0].score == pytest.approx(1 / 62 + 1 / 61)
    # d1 = 1/61 (dense rank1) + 1/63 (sparse rank3)
    assert out[1].score == pytest.approx(1 / 61 + 1 / 63)
    # 原始字段保留
    assert out[0].text == "text-d2"


async def test_candidate_k_overfetch_not_top_k():
    """两路应按 candidate_k 过取，而非最终 top_k。"""
    dense = _FakeRetriever([_hit("d1")])
    sparse = _FakeRetriever([_hit("d1")])
    h = HybridRetriever(dense=dense, sparse=sparse, rrf_k=60, candidate_k=50)

    await h.retrieve(query="q", tenant=TenantContext(tenant_id="t1"), top_k=2)

    assert dense.last_top_k == 50
    assert sparse.last_top_k == 50


async def test_truncates_to_top_k():
    dense = _FakeRetriever([_hit(f"d{i}") for i in range(5)])
    sparse = _FakeRetriever([])
    h = HybridRetriever(dense=dense, sparse=sparse, rrf_k=60, candidate_k=50)

    out = await h.retrieve(query="q", tenant=TenantContext(tenant_id="t1"), top_k=3)
    assert len(out) == 3


async def test_dense_failure_degrades_to_sparse():
    """稠密路异常 -> 降级稀疏单路 RRF（按 sparse rank 排序）。"""
    dense = _FakeRetriever([], raise_exc=RuntimeError("dense down"))
    sparse = _FakeRetriever([_hit("d2"), _hit("d4"), _hit("d1")])
    h = HybridRetriever(dense=dense, sparse=sparse, rrf_k=60, candidate_k=50)

    out = await h.retrieve(query="q", tenant=TenantContext(tenant_id="t1"), top_k=2)
    # 单路：d2(rank1) > d4(rank2) > d1(rank3)
    assert [o.chunk_id for o in out] == ["d2", "d4"]


async def test_sparse_failure_degrades_to_dense():
    sparse = _FakeRetriever([], raise_exc=RuntimeError("sparse down"))
    dense = _FakeRetriever([_hit("d1"), _hit("d2"), _hit("d3")])
    h = HybridRetriever(dense=dense, sparse=sparse, rrf_k=60, candidate_k=50)

    out = await h.retrieve(query="q", tenant=TenantContext(tenant_id="t1"), top_k=2)
    assert [o.chunk_id for o in out] == ["d1", "d2"]


async def test_both_empty_returns_empty():
    dense = _FakeRetriever([])
    sparse = _FakeRetriever([])
    h = HybridRetriever(dense=dense, sparse=sparse, rrf_k=60, candidate_k=50)
    out = await h.retrieve(query="q", tenant=TenantContext(tenant_id="t1"), top_k=5)
    assert out == []


async def test_weights_affect_order():
    """bm25_weight 远大于 dense_weight -> 稀疏路 rank1 的 d2 超过双路 d1。"""
    dense = _FakeRetriever([_hit("d1"), _hit("d2")])   # d1 dense rank1
    sparse = _FakeRetriever([_hit("d2"), _hit("d1")])  # d2 sparse rank1
    h = HybridRetriever(
        dense=dense, sparse=sparse, rrf_k=60,
        dense_weight=0.01, bm25_weight=10.0, candidate_k=50,
    )
    out = await h.retrieve(query="q", tenant=TenantContext(tenant_id="t1"), top_k=2)
    # 稀疏权重压倒 -> d2（sparse rank1）居首
    assert out[0].chunk_id == "d2"
