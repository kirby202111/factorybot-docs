"""B chunk 不可变不变式。

核心设计（绕开 ChromaDB 多记录翻转无事务弱点）：写入后所有 metadata 字段永不修改；
版本升版 = 追加新版本 chunk（带新 ``version``），不翻转老 chunk 的 state。
版本隔离靠查询 ``where={"state":"PUBLISHED","version_kind":..,"version":..}`` 过滤。
"""
from __future__ import annotations

from app.routes.document.domain.chunk import ChunkLocator, DocumentChunk
from app.routes.document.infrastructure.chunk_filter import ChunkFilter
from app.shared.events.version_contract import VersionAnchor, VersionKind
from app.shared.tenant.context import TenantContext


def _make_chunk(version: str, chunk_seq: int) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"doc-1:v{version}:{chunk_seq}",
        version_id=f"v{version}",
        doc_id="doc-1",
        chunk_seq=chunk_seq,
        text="SOP 步骤文本",
        locator=ChunkLocator(page=1, heading_path=["焊接", "步骤1"]),
        section_type="STEP",
        version_kind="route",
        version_ref_id="R-1",
        version=version,
        state="PUBLISHED",
        tenant_scope="workshop:PCBA",
        doc_type="SOP",
        file_content_hash=f"hash-{version}-{chunk_seq}",
    )


def test_old_chunk_metadata_unchanged_after_version_upgrade():
    """升版追加新 chunk，老 chunk metadata 完全不变（仍锁旧版本 + PUBLISHED）。"""
    v1 = _make_chunk("v3", 0)
    meta_v1_before = v1.to_metadata_dict()

    _v4 = _make_chunk("v4", 0)  # 升版：追加 v4，不翻转 v1

    meta_v1_after = v1.to_metadata_dict()
    assert meta_v1_before == meta_v1_after
    assert meta_v1_after["version"] == "v3"   # 老 chunk 仍锁 v3
    assert meta_v1_after["state"] == "PUBLISHED"    # 状态不翻转


def test_to_metadata_dict_fields_complete():
    """to_metadata_dict 字段完备（ChromaDB metadata 不可变字段全集）。"""
    chunk = _make_chunk("v3", 2)
    meta = chunk.to_metadata_dict()
    expected_keys = {
        "doc_id", "version_id", "doc_type", "state", "version_kind", "version_ref_id",
        "version", "tenant_scope", "chunk_seq", "section_type",
        "locator", "file_content_hash",
    }
    assert set(meta.keys()) == expected_keys
    assert meta["chunk_seq"] == 2
    assert meta["version"] == "v3"
    assert meta["version_kind"] == "route"
    assert meta["state"] == "PUBLISHED"
    assert meta["locator"]  # JSON 字符串非空


def test_build_where_enforces_published_and_version_equality(tenant: TenantContext):
    """ChunkFilter.to_where 强制 state=PUBLISHED + 版本锚点等值（非 $in）。"""
    where = ChunkFilter(
        tenant=tenant,
        version_anchor=VersionAnchor(kind=VersionKind.ROUTE, ref_id="R-1", version="v3"),
        doc_types=("SOP",),
    ).to_where()
    assert where["state"] == "PUBLISHED"
    assert where["version_kind"] == "route"
    assert where["version"] == "v3"                        # 版本等值，非 {"$in": [...]}
    assert where["doc_type"] == {"$in": ["SOP"]}
    assert where["tenant_scope"] == {"$in": tenant.chroma_scopes()}


def test_build_where_without_version_still_enforces_published(tenant: TenantContext):
    """不带版本锚点时仍强制 state=PUBLISHED，且不把版本加入过滤。"""
    where = ChunkFilter(tenant=tenant, version_anchor=None, doc_types=()).to_where()
    assert where["state"] == "PUBLISHED"
    assert "version_kind" not in where                     # 不带锚点则不过滤版本维度
    assert "version" not in where


def test_build_where_asset_anchor_filters_ref_id(tenant: TenantContext):
    """ASSET 锚点按 version_ref_id 过滤（version 可空）。"""
    where = ChunkFilter(
        tenant=tenant,
        version_anchor=VersionAnchor(kind=VersionKind.ASSET, ref_id="EQ-1", version=""),
        doc_types=(),
    ).to_where()
    assert where["state"] == "PUBLISHED"
    assert where["version_kind"] == "asset"
    assert where["version_ref_id"] == "EQ-1"
    assert "version" not in where                          # version 空 -> 不过滤 version
