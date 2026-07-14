"""ChunkFilter 稠密/稀疏过滤等价性（混合召回硬约束）。

``to_where()`` 给 ChromaDB pre-filter、``matches()`` 给 BM25 内存谓词，二者必须对任意
chunk + 过滤判据产出一致命中。用 ChromaDB where 语义模拟器逐条比对，防 DEPRECATED/
版本/租户/doc_type 泄漏。
"""
from __future__ import annotations

from app.routes.document.domain.chunk import DocumentChunk
from app.routes.document.infrastructure.chunk_filter import ChunkFilter
from app.shared.events.version_contract import VersionAnchor, VersionKind
from app.shared.tenant.context import TenantContext


def _chunk(
    *,
    chunk_id: str,
    state: str = "PUBLISHED",
    version_kind: str = "",
    version_ref_id: str = "",
    version: str = "",
    tenant_scope: str = "",
    doc_type: str = "",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        version_id="v1",
        doc_id="d1",
        chunk_seq=0,
        text="x",
        state=state,
        version_kind=version_kind,
        version_ref_id=version_ref_id,
        version=version,
        tenant_scope=tenant_scope,
        doc_type=doc_type,
    )


def _route_anchor(version: str, ref_id: str = "") -> VersionAnchor:
    return VersionAnchor(kind=VersionKind.ROUTE, ref_id=ref_id, version=version)


def _asset_anchor(ref_id: str, version: str = "") -> VersionAnchor:
    return VersionAnchor(kind=VersionKind.ASSET, ref_id=ref_id, version=version)


def _where_matches(where: dict, chunk: DocumentChunk) -> bool:
    """模拟 ChromaDB ``where`` 过滤语义（与 to_where 对偶）。"""
    meta = chunk.to_metadata_dict()
    for k, v in where.items():
        val = meta.get(k)
        if isinstance(v, dict) and "$in" in v:
            if val not in v["$in"]:
                return False
        elif val != v:
            return False
    return True


def _assert_parity(chunks, f: ChunkFilter) -> None:
    where = f.to_where()
    for c in chunks:
        assert f.matches(c) == _where_matches(where, c), (
            f"过滤不一致 chunk={c.chunk_id}: matches={f.matches(c)} where={_where_matches(where, c)}"
        )


def test_state_filter_excludes_deprecated():
    tenant = TenantContext(tenant_id="t1")
    f = ChunkFilter(tenant=tenant)
    chunks = [_chunk(chunk_id="a", state="PUBLISHED"), _chunk(chunk_id="b", state="DEPRECATED")]
    _assert_parity(chunks, f)
    assert f.matches(chunks[0]) is True
    assert f.matches(chunks[1]) is False


def test_version_equality():
    tenant = TenantContext(tenant_id="t1")
    f = ChunkFilter(tenant=tenant, version_anchor=_route_anchor("v3"))
    chunks = [
        _chunk(chunk_id="a", version_kind="route", version="v3"),
        _chunk(chunk_id="b", version_kind="route", version="v4"),
        _chunk(chunk_id="c", version_kind="", version=""),  # 未绑定版本
    ]
    _assert_parity(chunks, f)
    assert [c.chunk_id for c in chunks if f.matches(c)] == ["a"]


def test_tenant_scope_in():
    tenant = TenantContext(tenant_id="t1", tenant_scopes=["workshop:PCBA", "line:SMT-1"])
    f = ChunkFilter(tenant=tenant)
    chunks = [
        _chunk(chunk_id="a", tenant_scope="workshop:PCBA"),
        _chunk(chunk_id="b", tenant_scope="line:SMT-1"),
        _chunk(chunk_id="c", tenant_scope="workshop:BOX"),  # 不在 scopes
        _chunk(chunk_id="d", tenant_scope=""),              # 通用知识型空 scope
    ]
    _assert_parity(chunks, f)
    assert {c.chunk_id for c in chunks if f.matches(c)} == {"a", "b"}


def test_empty_scopes_pass_all():
    """tenant_scopes 为空 -> 不过滤 tenant_scope（与 ChromaDB 不写该键等价）。"""
    tenant = TenantContext(tenant_id="t1", tenant_scopes=[])
    f = ChunkFilter(tenant=tenant)
    chunks = [_chunk(chunk_id="a", tenant_scope="anything"), _chunk(chunk_id="b", tenant_scope="")]
    _assert_parity(chunks, f)
    assert all(f.matches(c) for c in chunks)


def test_doc_type_in_and_asset():
    tenant = TenantContext(tenant_id="t1")
    f = ChunkFilter(tenant=tenant, version_anchor=_asset_anchor("EQ-007"), doc_types=("SOP", "PARAM"))
    chunks = [
        _chunk(chunk_id="a", version_kind="asset", version_ref_id="EQ-007", doc_type="SOP"),
        _chunk(chunk_id="b", version_kind="asset", version_ref_id="EQ-007", doc_type="FAULT"),  # doc_type 不命中
        _chunk(chunk_id="c", version_kind="asset", version_ref_id="EQ-008", doc_type="SOP"),    # ref 不命中
        _chunk(chunk_id="d", version_kind="", version_ref_id="", doc_type="SOP"),               # 未绑定
    ]
    _assert_parity(chunks, f)
    assert [c.chunk_id for c in chunks if f.matches(c)] == ["a"]


def test_combined_filters_parity():
    """组合所有维度，逐 chunk 比对两路语义。"""
    tenant = TenantContext(tenant_id="t1", tenant_scopes=["workshop:PCBA"])
    f = ChunkFilter(tenant=tenant, version_anchor=_route_anchor("v3"), doc_types=("SOP",))
    chunks = [
        _chunk(chunk_id="ok", version_kind="route", version="v3", tenant_scope="workshop:PCBA", doc_type="SOP"),
        _chunk(chunk_id="bad_state", state="DEPRECATED", version_kind="route", version="v3"),
        _chunk(chunk_id="bad_rv", version_kind="route", version="v9"),
        _chunk(chunk_id="bad_scope", version_kind="route", version="v3", tenant_scope="workshop:BOX"),
        _chunk(chunk_id="bad_dtype", version_kind="route", version="v3", tenant_scope="workshop:PCBA", doc_type="FAULT"),
    ]
    _assert_parity(chunks, f)
    assert [c.chunk_id for c in chunks if f.matches(c)] == ["ok"]
