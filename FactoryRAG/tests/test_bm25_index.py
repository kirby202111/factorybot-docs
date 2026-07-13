"""Bm25Index：构建/检索/增量维护/过滤/边界。

用英文术语文本保证排名在 jieba 与降级正则两路下均确定（英文按词切分一致）。
"""
from __future__ import annotations

import pytest

from app.routes.document.domain.chunk import DocumentChunk
from app.routes.document.infrastructure.bm25.bm25_index import Bm25Index
from app.routes.document.infrastructure.bm25.tokenizer import Tokenizer
from app.routes.document.infrastructure.chunk_filter import ChunkFilter
from app.shared.tenant.context import TenantContext


def _chunk(
    *,
    chunk_id: str,
    text: str,
    state: str = "PUBLISHED",
    route_version: str | None = None,
    version_id: str = "v1",
    tenant_scope: str = "",
    doc_type: str = "",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        version_id=version_id,
        doc_id="d1",
        chunk_seq=0,
        text=text,
        state=state,
        route_version=route_version,
        tenant_scope=tenant_scope,
        doc_type=doc_type,
    )


@pytest.fixture
def index() -> Bm25Index:
    return Bm25Index(Tokenizer())


async def test_build_and_search_ranks_by_term_overlap(index: Bm25Index):
    await index.build_from_chunks(
        [
            _chunk(chunk_id="c1", text="reflow oven temperature profile SMT"),
            _chunk(chunk_id="c2", text="wave soldering temperature profile"),
            _chunk(chunk_id="c3", text="quality inspection defect catalog"),
        ]
    )
    assert index.size == 3

    results = await index.search("reflow temperature", predicate=lambda c: True, top_k=3)
    ids = [c.chunk_id for c, _ in results]
    # c1 命中 reflow+temperature，c2 仅 temperature，c3 无命中
    assert ids[0] == "c1"
    assert ids.index("c1") < ids.index("c2")
    assert results[0][1] > results[1][1]  # 分数单调


async def test_empty_query_returns_empty(index: Bm25Index):
    await index.build_from_chunks([_chunk(chunk_id="c1", text="reflow oven")])
    assert await index.search("", predicate=lambda c: True, top_k=5) == []
    assert await index.search("   ", predicate=lambda c: True, top_k=5) == []


async def test_empty_token_chunk_skipped(index: Bm25Index):
    """纯停用词/空文本 chunk 不入索引（无可索引词项）。"""
    await index.build_from_chunks(
        [
            _chunk(chunk_id="c1", text="reflow oven"),
            _chunk(chunk_id="c2", text="的 了 和"),  # 全停用词
            _chunk(chunk_id="c3", text=""),
        ]
    )
    assert index.size == 1  # 仅 c1 入索引


async def test_add_new_chunk_retrievable(index: Bm25Index):
    await index.build_from_chunks([_chunk(chunk_id="c1", text="reflow oven")])
    await index.add([_chunk(chunk_id="c2", text="wave soldering temperature")])
    assert index.size == 2
    results = await index.search("soldering", predicate=lambda c: True, top_k=5)
    assert any(c.chunk_id == "c2" for c, _ in results)


async def test_add_idempotent_for_same_chunk_id(index: Bm25Index):
    await index.build_from_chunks([_chunk(chunk_id="c1", text="reflow oven")])
    await index.add([_chunk(chunk_id="c1", text="reflow oven")])  # 同 id 幂等
    assert index.size == 1


async def test_remove_excludes_chunk(index: Bm25Index):
    await index.build_from_chunks(
        [
            _chunk(chunk_id="c1", text="reflow oven temperature"),
            _chunk(chunk_id="c2", text="reflow oven profile"),
        ]
    )
    await index.remove(["c1"])
    assert index.size == 1
    results = await index.search("reflow", predicate=lambda c: True, top_k=5)
    assert all(c.chunk_id != "c1" for c, _ in results)
    assert any(c.chunk_id == "c2" for c, _ in results)


async def test_remove_nonexistent_is_noop(index: Bm25Index):
    await index.build_from_chunks([_chunk(chunk_id="c1", text="reflow oven")])
    await index.remove(["nope"])
    assert index.size == 1


async def test_remove_by_version(index: Bm25Index):
    await index.build_from_chunks(
        [
            _chunk(chunk_id="c1", text="reflow oven", version_id="v1"),
            _chunk(chunk_id="c2", text="wave solder", version_id="v2"),
        ]
    )
    await index.remove_by_version("v1")
    assert index.size == 1
    results = await index.search("reflow", predicate=lambda c: True, top_k=5)
    assert all(c.chunk_id != "c1" for c, _ in results)


async def test_predicate_filters_by_route_version(index: Bm25Index):
    """ChunkFilter.matches 过滤：仅返回 route_version=v3 的命中（与稠密路等价）。"""
    await index.build_from_chunks(
        [
            _chunk(chunk_id="c1", text="reflow oven", route_version="v3"),
            _chunk(chunk_id="c2", text="reflow oven", route_version="v4"),
        ]
    )
    tenant = TenantContext(tenant_id="t1")
    f = ChunkFilter(tenant=tenant, route_version="v3")
    results = await index.search("reflow", predicate=f.matches, top_k=5)
    ids = [c.chunk_id for c, _ in results]
    assert ids == ["c1"]


async def test_build_from_collection_pulls_published(index: Bm25Index):
    """build_from_collection 仅拉 state=PUBLISHED（DEPRECATED 不入索引）。"""
    collection = type(
        "FakeCollection",
        (),
        {
            "get": lambda self, where=None, include=None: {
                "ids": ["c1", "c2"],
                "documents": ["reflow oven", "wave solder"],
                "metadatas": [
                    {"doc_id": "d1", "version_id": "v1", "chunk_seq": 0, "state": "PUBLISHED"},
                    {"doc_id": "d1", "version_id": "v1", "chunk_seq": 1, "state": "PUBLISHED"},
                ],
            }
        },
    )()
    await index.build_from_collection(collection)
    assert index.size == 2
    results = await index.search("reflow", predicate=lambda c: True, top_k=5)
    assert any(c.chunk_id == "c1" for c, _ in results)
