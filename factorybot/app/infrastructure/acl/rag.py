"""RAG 服务 ACL：追溯图查询（L1 快路径）+ 子图回查（L2，不重查图）。

- query_traceability_graph：L1 ToolRegistry 注册首位，system prompt 引导"先调图"。
  图用 SNAPSHOT_OF_ROUTE{route_version} 快照边把版本一致性变成结构属性。
- fetch_subgraph_nodes：L2 按 L1 透传的 subgraph_ref 回查图节点，不重查图。
"""
from __future__ import annotations

from typing import Optional

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import SubgraphView, TraceGraphView, to_view


class RagAclClient(BaseAclClient):
    """RAG 服务·追溯图（只读，图是事件流的只读投影）。"""

    async def query_traceability_graph(
        self, serial_no: str, tenant: TenantContext,
        subgraph_ref: Optional[str] = None,
        route_version: Optional[str] = None,
    ) -> TraceGraphView:
        """POST /rag/trace/query -- 返回节点/边/subgraph_ref/route_version。"""
        dto = await self._get(
            "/rag/trace/query",
            tenant=tenant,
            params={
                "serial_no": serial_no,
                "subgraph_ref": subgraph_ref,
                "route_version": route_version,
            },
            fixture_rel="rag/trace_graph", fixture_key=serial_no,
        )
        return to_view(TraceGraphView, dto)

    async def fetch_subgraph_nodes(
        self, subgraph_ref: str, tenant: TenantContext,
    ) -> list[dict]:
        """GET /rag/trace/subgraph/{ref} -- L2 按 subgraph_ref 回查，不重查图。

        返回 TraceNodeView 形状的节点列表（label/bounded_context/node_id/props/source_event_id）。
        """
        dto = await self._get(
            f"/rag/trace/subgraph/{subgraph_ref}", tenant=tenant,
            fixture_rel="rag/subgraphs", fixture_key=subgraph_ref,
        )
        view = to_view(SubgraphView, dto) if isinstance(dto, dict) else SubgraphView(subgraph_ref=subgraph_ref, nodes=(dto or []))
        return view.nodes
