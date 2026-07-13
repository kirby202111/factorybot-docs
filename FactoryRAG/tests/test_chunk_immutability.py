"""B chunk 不可变不变式。

核心设计（绕开 ChromaDB 多记录翻转无事务弱点）：写入后所有 metadata 字段永不修改；
工艺升版 = 追加新版本 chunk（带新 route_version），不翻转老 chunk 的 state。
版本隔离靠查询 ``where={"state":"PUBLISHED","route_version":rv}`` 过滤。
"""
from __future__ import annotations

from app.routes.document.domain.chunk import ChunkLocator, DocumentChunk
from app.routes.document.infrastructure.chromadb.retriever import VectorRetriever
from app.shared.tenant.context import TenantContext


def _make_chunk(route_version: str, chunk_seq: int) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"doc-1:v{route_version}:{chunk_seq}",
        version_id=f"v{route_version}",
        doc_id="doc-1",
        chunk_seq=chunk_seq,
        text="SOP 步骤文本",
        locator=ChunkLocator(page=1, heading_path=["焊接", "步骤1"]),
        section_type="STEP",
        route_version=route_version,
        route_id="R-1",
        state="PUBLISHED",
        tenant_scope="workshop:PCBA",
        doc_type="SOP",
        file_content_hash=f"hash-{route_version}-{chunk_seq}",
    )


def test_old_chunk_metadata_unchanged_after_version_upgrade():
    """升版追加新 chunk，老 chunk metadata 完全不变（仍锁旧版本 + PUBLISHED）。"""
    v1 = _make_chunk("v3", 0)
    meta_v1_before = v1.to_metadata_dict()

    _v4 = _make_chunk("v4", 0)  # 升版：追加 v4，不翻转 v1

    meta_v1_after = v1.to_metadata_dict()
    assert meta_v1_before == meta_v1_after
    assert meta_v1_after["route_version"] == "v3"   # 老 chunk 仍锁 v3
    assert meta_v1_after["state"] == "PUBLISHED"    # 状态不翻转


def test_to_metadata_dict_fields_complete():
    """to_metadata_dict 字段完备（ChromaDB metadata 不可变字段全集）。"""
    chunk = _make_chunk("v3", 2)
    meta = chunk.to_metadata_dict()
    expected_keys = {
        "doc_id", "version_id", "doc_type", "state", "route_version", "route_id",
        "tenant_scope", "binding_asset_id", "chunk_seq", "section_type",
        "locator", "file_content_hash",
    }
    assert set(meta.keys()) == expected_keys
    assert meta["chunk_seq"] == 2
    assert meta["route_version"] == "v3"
    assert meta["state"] == "PUBLISHED"
    assert meta["locator"]  # JSON 字符串非空


def test_build_where_enforces_published_and_route_version_equality(tenant: TenantContext):
    """VectorRetriever._build_where 强制 state=PUBLISHED + route_version 等值（非 $in）。"""
    retriever = VectorRetriever(collection=None, embedder=None)
    where = retriever._build_where(
        tenant, route_version="v3", asset_id=None, doc_types=["SOP"]
    )
    assert where["state"] == "PUBLISHED"
    assert where["route_version"] == "v3"                       # 版本等值，非 {"$in": [...]}
    assert where["doc_type"] == {"$in": ["SOP"]}
    assert where["tenant_scope"] == {"$in": tenant.chroma_scopes()}


def test_build_where_without_route_version_still_enforces_published(tenant: TenantContext):
    """不带 route_version 时仍强制 state=PUBLISHED，且不把版本加入过滤。"""
    retriever = VectorRetriever(collection=None, embedder=None)
    where = retriever._build_where(tenant, route_version=None, asset_id="A-1", doc_types=None)
    assert where["state"] == "PUBLISHED"
    assert "route_version" not in where                         # 不带版本则不过滤
    assert where["binding_asset_id"] == "A-1"
