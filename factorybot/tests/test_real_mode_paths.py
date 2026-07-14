"""real 模式路径回归：验证 mock 遮蔽的真实集成路径（HTTP 方法/body/mapper/启动校验）。

用 httpx.MockTransport 模拟 FactoryRAG 响应，不依赖真实基础设施，但能暴露
"mock 下跑通、real 下 405/422"类问题。覆盖第一优先级（真实模式接线）的关键路径。
"""
import json

import httpx
import pytest

from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor
from app.infrastructure.acl.doc_rag import DocRagAclClient
from app.infrastructure.acl.rag import RagAclClient
from app.infrastructure.cost.eval_gate import EvalGate
from app.infrastructure.cost.model_router import ModelRouter


@pytest.mark.asyncio
async def test_query_traceability_graph_real_post_expand_and_mapper():
    """real：POST /rag/trace/expand（非 GET /query），body=Seed，mapper 映射 TraceSubgraph。"""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content) if req.content else None
        return httpx.Response(200, json={
            "seed": {"label": "WipUnit", "node_id": "w:SN-001",
                     "props": {"seed_kind": "WipUnit", "seed_value": "SN-001"}},
            "clusters": {
                "method": [{"label": "RouteVersion", "node_id": "rv:1",
                            "props": {"route_id": "RR-B", "route_version": "v4"},
                            "source_event_id": "e1"}],
                "man": [{"label": "CheckpointRecord", "node_id": "cp:1",
                         "props": {"decision": "BLOCK"}, "source_event_id": "e2"}],
            },
            "edges": [{"rel": "SNAPSHOT_OF_ROUTE", "from_id": "w:SN-001",
                       "to_id": "rv:1", "version": "v4"}],
            "as_of": "2026-07-12T13:00:00+00:00",
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    client = RagAclClient(http=http, base_url="", fixtures=None, mock=False)
    try:
        view = await client.query_traceability_graph("SN-001", TenantContext.default())
    finally:
        await http.aclose()

    # 请求：POST /expand（非 GET /query 的 query string），body 是 Seed
    assert captured["method"] == "POST"
    assert captured["path"] == "/rag/trace/expand"
    assert captured["body"] == {"kind": "WipUnit", "value": "SN-001"}
    # mapper：clusters 展平成 nodes，version 从 method 提取，subgraph_ref 计算
    assert view.serial_no == "SN-001"
    assert len(view.nodes) == 2
    assert view.version == "v4"
    assert view.version_kind == "route"
    assert view.version_ref_id == "RR-B"
    assert view.subgraph_ref == "WipUnit:SN-001@2026-07-12T13:00:00+00:00"
    assert view.edges[0]["type"] == "SNAPSHOT_OF_ROUTE"
    assert view.edges[0]["properties"]["version"] == "v4"


@pytest.mark.asyncio
async def test_search_docs_real_post_and_no_client_filter():
    """real：POST /rag/docs/search，body 对齐 DocSearch，不做客户端版本过滤。"""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content) if req.content else None
        # 服务端返回含 version 不匹配的 chunk（验证客户端不再过滤）
        return httpx.Response(200, json=[
            {"chunk_id": "c1", "doc_id": "d1", "version": "v3", "text": "旧版SOP"},
            {"chunk_id": "c2", "doc_id": "d2", "version": "v4", "text": "现行SOP"},
        ])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    client = DocRagAclClient(http=http, base_url="", fixtures=None, mock=False)
    anchor = VersionAnchor.from_flat("v4", "route", "RR-B")
    try:
        docs = await client.search_docs("焊接SOP", TenantContext.default(), version_anchor=anchor)
    finally:
        await http.aclose()

    assert captured["method"] == "POST"
    assert captured["path"] == "/rag/docs/search"
    assert captured["body"] == {
        "question": "焊接SOP", "version": "v4",
        "version_kind": "route", "version_ref_id": "RR-B",
    }
    # 客户端不再过滤：两 chunk 都返回（版本过滤交服务端）
    assert len(docs) == 2


def test_model_router_real_does_not_raise_on_unevaluated():
    """real（allow_mock=False）：未评测模型打 warn 但不 raise（诚实化不阻断启动）。"""
    router = ModelRouter(EvalGate(), allow_mock=False, active_model="claude-sonnet-5")
    router.validate_on_startup()  # EvalGate 无 register -> passed 恒 False；不抛即通过


def test_model_router_mock_skips_validation():
    """mock（allow_mock=True）：豁免全部路由校验，不查 passed。"""
    router = ModelRouter(EvalGate(), allow_mock=True, active_model="mock")
    router.validate_on_startup()  # 不抛即通过
