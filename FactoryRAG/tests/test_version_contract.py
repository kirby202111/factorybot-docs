"""版本契约：VersionAnchor / ReindexRequest + parse_anchor 缺失版本抛错。

版本一致性三段链第一段（rag-service 侧）：图 SNAPSHOT_OF_ROUTE{route_version} 快照边
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
    """VersionAnchor.as_edge_attr 产出图快照边属性字典。"""
    anchor = VersionAnchor(kind=VersionKind.ROUTE, ref_id="R-1", version="v3")
    assert anchor.as_edge_attr() == {"route_version": "v3"}


def test_parse_anchor_missing_version_raises():
    """parse_anchor 缺失版本 -> ValueError（安全契约：版本锚点不可为空）。"""
    with pytest.raises(ValueError, match="route_version 缺失"):
        parse_anchor(VersionKind.ROUTE, ref_id="R-1", version="")


def test_parse_anchor_ok():
    anchor = parse_anchor(VersionKind.RULE, ref_id="Q-1", version="v2")
    assert anchor.kind == VersionKind.RULE
    assert anchor.ref_id == "Q-1"
    assert anchor.version == "v2"


def test_reindex_request_kafka_payload():
    """ReindexRequest.as_kafka_payload 结构（A 升版 -> B 重索引内部事件）。"""
    req = ReindexRequest(route_id="R-1", route_version="v3", trace_id="trace-1")
    payload = req.as_kafka_payload()
    assert payload["event_type"] == "rag.reindex.request"
    assert payload["source_service"] == "rag-service.traceability"
    assert payload["payload"] == {"route_id": "R-1", "route_version": "v3"}
    assert payload["trace_id"] == "trace-1"
    assert payload["event_id"]  # 自动生成非空
