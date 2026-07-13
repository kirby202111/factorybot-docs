"""A: ``POST /rag/trace/query`` / ``POST /rag/trace/expand``。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_tenant, get_trace_svc
from app.routes.traceability.domain.answer import TraceAnswer
from app.routes.traceability.domain.seed import ExpandRequest, TraceQuery
from app.routes.traceability.domain.subgraph import TraceSubgraph
from app.shared.tenant.context import TenantContext


def trace_router() -> APIRouter:
    router = APIRouter(prefix="/rag/trace", tags=["traceability-A"])

    @router.post("/query", response_model=TraceAnswer)
    async def query(
        req: TraceQuery,
        tenant: TenantContext = Depends(get_tenant),
        svc=Depends(get_trace_svc),
    ) -> TraceAnswer:
        """子图检索 + LLM 综合，返回 TraceAnswer（含 subgraph_ref + route_version）。"""
        return await svc.retrieve_and_synthesize(req, tenant)

    @router.post("/expand", response_model=TraceSubgraph)
    async def expand(
        req: ExpandRequest,
        tenant: TenantContext = Depends(get_tenant),
        svc=Depends(get_trace_svc),
    ) -> TraceSubgraph:
        """只取子图不综合（L2 回查用，不重跑 Cypher）。"""
        return await svc.expand_subgraph(req, tenant)

    return router
