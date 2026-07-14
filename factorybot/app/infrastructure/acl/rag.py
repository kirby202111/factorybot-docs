"""RAG 服务 ACL：追溯图查询（诊断 快路径）+ 子图回查（草稿，不重查图）。

- query_traceability_graph：诊断 ToolRegistry 注册首位，system prompt 引导"先调图"。
  real 模式调 ``POST /rag/trace/expand``（只取原始子图，不做 LLM 综合--诊断 Agent 自己
  ReAct 推理，不应让 RAG 替它出 5M1E 假设）；mock 模式从 fixtures 读已映射好的
  TraceGraphView 形状。图用 ``SNAPSHOT_OF_{kind}{version}`` 快照边把版本一致性变成结构属性。
- fetch_subgraph_nodes：草稿 按 诊断 透传的 subgraph_ref 回查图节点，不重查图。
"""
from __future__ import annotations

from typing import Any, Optional

from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import SubgraphView, TraceGraphView, to_view


class RagAclClient(BaseAclClient):
    """RAG 服务·追溯图（只读，图是事件流的只读投影）。"""

    async def query_traceability_graph(
        self, serial_no: str, tenant: TenantContext,
        subgraph_ref: Optional[str] = None,
        version_anchor: Optional[VersionAnchor] = None,
    ) -> TraceGraphView:
        """POST /rag/trace/expand -- 返回节点/边/subgraph_ref/版本锚点（原始子图）。

        诊断 Agent 要原始子图自己 ReAct 推理，故走 /expand（只取子图）而非 /query
        （/query 返回 TraceAnswer，是 LLM 综合的 5M1E 假设，会让 RAG 替诊断 Agent 推理）。
        入参 serial_no 作为 Seed(WipUnit)；subgraph_ref 入参保留兼容但忽略
        （/expand 按 seed 查，subgraph_ref 是其输出属性）。
        """
        body: dict = {"kind": "WipUnit", "value": serial_no}
        if version_anchor is not None:
            body["version"] = version_anchor.version
            body["version_kind"] = version_anchor.kind.value
        dto = await self._post_read(
            "/rag/trace/expand", body, tenant=tenant,
            fixture_rel="rag/trace_graph", fixture_key=serial_no,
        )
        if self._mock:
            # fixture 已是 TraceGraphView 形状（mock 下透传）
            return to_view(TraceGraphView, dto)
        # real：FactoryRAG 返回 TraceSubgraph(clusters/edges)，映射成 TraceGraphView
        return _map_subgraph_to_graph_view(dto, serial_no)

    async def fetch_subgraph_nodes(
        self, subgraph_ref: str, tenant: TenantContext,
    ) -> list[dict]:
        """草稿 按 subgraph_ref 回查图节点，不重查图。

        返回节点列表（label/node_id/props/source_event_id）。
        TODO(real): FactoryRAG 当前无"按 subgraph_ref 回查"的端点（/expand 收 kind/value，
        subgraph_ref 是其输出）。real 集成时需：解析 subgraph_ref(<kind>:<value>@<as_of>)
        提取 kind/value/as_of 调 /expand，或在 FactoryRAG 增设按 ref 查的端点。
        mock 模式下从 fixtures 读 rag/subgraphs.json 按 subgraph_ref 索引。
        """
        if not self._mock:
            raise NotImplementedError(
                "fetch_subgraph_nodes real 模式未实现：FactoryRAG 无按 subgraph_ref 查端点；"
                "需解析 subgraph_ref 调 /expand 或在 FactoryRAG 增设端点（见 TODO）"
            )
        dto = await self._post_read(
            "/rag/trace/expand", {}, tenant=tenant,
            fixture_rel="rag/subgraphs", fixture_key=subgraph_ref,
        )
        view = to_view(SubgraphView, dto) if isinstance(dto, dict) else SubgraphView(
            subgraph_ref=subgraph_ref, nodes=(dto or []))
        return view.nodes


def _map_subgraph_to_graph_view(dto: Any, serial_no: str) -> TraceGraphView:
    """FactoryRAG TraceSubgraph -> factorybot TraceGraphView（real 模式 mapper）。

    /expand 响应只含 seed/clusters/edges/as_of；subgraph_ref 与 version 三字段在
    TraceSubgraph 上是 @property/方法，不进 JSON。这里镜像其计算逻辑构造 View，
    使 real 产出与 mock fixture（rag/trace_graph.json）一致的形状，诊断 Agent 无感知。

    字段映射：
    - nodes: clusters 五维(5M1E)展平，TraceNode{node_id,props} -> {id,properties}
    - edges: TraceEdge{rel,from_id,to_id,version} -> {source,target,type,properties}
    - subgraph_ref: f"{seed_kind}:{seed_value}@{as_of_iso}"（镜像 TraceSubgraph.subgraph_ref）
    - version 三字段: 从 clusters.method 的 RouteVersion 快照节点提取（镜像 version_locked）
    """
    dto = dto if isinstance(dto, dict) else {}
    clusters = dto.get("clusters") or {}

    nodes: list[dict] = []
    for dim in ("man", "machine", "material", "method", "measurement", "environment"):
        for n in clusters.get(dim) or []:
            nodes.append({
                "id": n.get("node_id", ""),
                "label": n.get("label", ""),
                "properties": n.get("props") or {},
                "source_event_id": n.get("source_event_id", ""),
            })

    edges: list[dict] = []
    for e in dto.get("edges") or []:
        edge: dict = {
            "source": e.get("from_id", ""),
            "target": e.get("to_id", ""),
            "type": e.get("rel", ""),
        }
        if e.get("version") is not None:
            edge["properties"] = {"version": e["version"]}
        edges.append(edge)

    # subgraph_ref：镜像 TraceSubgraph.subgraph_ref property
    seed = dto.get("seed") or {}
    seed_props = seed.get("props") or {}
    seed_kind = seed_props.get("seed_kind", seed.get("label", ""))
    seed_value = seed_props.get("seed_value", seed.get("node_id", ""))
    as_of = dto.get("as_of") or ""
    subgraph_ref = f"{seed_kind}:{seed_value}@{as_of}" if as_of else ""

    # version 三字段：从 clusters.method 的 RouteVersion 快照节点提取（镜像 version_locked）
    version = version_kind = version_ref_id = None
    for n in clusters.get("method") or []:
        p = n.get("props") or {}
        rv = p.get("route_version")
        if rv:
            version = str(rv)
            version_kind = "route"
            version_ref_id = str(p.get("route_id", ""))
            break

    return TraceGraphView(
        serial_no=serial_no,
        nodes=nodes,
        edges=edges,
        subgraph_ref=subgraph_ref,
        version=version,
        version_kind=version_kind,
        version_ref_id=version_ref_id,
    )
