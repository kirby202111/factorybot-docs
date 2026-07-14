"""版本契约：VersionAnchor / ReindexRequest + parse_anchor 缺失版本抛错。

版本一致性三段链第一段（rag-service 侧）：图 SNAPSHOT_OF_{kind}{version} 快照边
物理锁定版本 + 发布 rag.reindex.request 通知 B 重索引。
"""
from __future__ import annotations

import pytest

from app.shared.events.version_contract import (
    ReindexRequest,
    VersionAnchor,
    VersionKind,
    parse_anchor,
)


def test_version_anchor_as_edge_attr():
    """VersionAnchor.as_edge_attr 产出图快照边属性字典（{kind}_version）。"""
    anchor = VersionAnchor(kind=VersionKind.ROUTE, ref_id="R-1", version="v3")
    assert anchor.as_edge_attr() == {"route_version": "v3"}
    bom = VersionAnchor(kind=VersionKind.BOM, ref_id="BOM-1", version="v2")
    assert bom.as_edge_attr() == {"bom_version": "v2"}


def test_version_anchor_to_from_metadata():
    """to_metadata / from_metadata 互逆；无 kind/version 返回 None。"""
    anchor = VersionAnchor(kind=VersionKind.ASSET, ref_id="EQ-1", version="v2")
    meta = anchor.to_metadata()
    assert meta == {"version_kind": "asset", "version_ref_id": "EQ-1", "version": "v2"}
    restored = VersionAnchor.from_metadata(meta)
    assert restored == anchor
    assert VersionAnchor.from_metadata({"version_kind": "", "version": ""}) is None
    assert VersionAnchor.from_metadata({}) is None


def test_parse_anchor_missing_version_raises():
    """parse_anchor 缺失版本 -> ValueError（安全契约：版本锚点不可为空）。"""
    with pytest.raises(ValueError, match="route 版本锚点缺失"):
        parse_anchor(VersionKind.ROUTE, ref_id="R-1", version="")


def test_parse_anchor_ok():
    anchor = parse_anchor(VersionKind.RULE, ref_id="Q-1", version="v2")
    assert anchor.kind == VersionKind.RULE
    assert anchor.ref_id == "Q-1"
    assert anchor.version == "v2"


def test_version_kind_values():
    """VersionKind 覆盖五类版本锚点。"""
    assert VersionKind.ROUTE.value == "route"
    assert VersionKind.BOM.value == "bom"
    assert VersionKind.RULE.value == "rule"
    assert VersionKind.ASSET.value == "asset"
    assert VersionKind.STANDARD.value == "standard"


def test_reindex_request_kafka_payload():
    """ReindexRequest.as_kafka_payload 结构（A 升版 -> B 重索引内部事件）。"""
    req = ReindexRequest(
        anchor=VersionAnchor(kind=VersionKind.ROUTE, ref_id="R-1", version="v3"),
        trace_id="trace-1",
    )
    payload = req.as_kafka_payload()
    assert payload["event_type"] == "rag.reindex.request"
    assert payload["source_service"] == "rag-service.traceability"
    assert payload["payload"] == {
        "anchor": {"kind": "route", "ref_id": "R-1", "version": "v3"}
    }
    assert payload["trace_id"] == "trace-1"
    assert payload["event_id"]  # 自动生成非空
