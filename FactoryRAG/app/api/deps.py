"""依赖注入 provider：从容器取实例，FastAPI 路由用 ``Depends`` 注入。"""
from __future__ import annotations

from fastapi import Depends, Request

from app.shared.tenant.context import TenantContext
from app.shared.tenant.dependency import tenant_from_header


def get_container(request: Request):
    return request.app.state.container


def get_trace_svc(container=Depends(get_container)):
    """A TraceRetrievalService（仅 traceability.enabled 时可用）。"""
    if container._trace_svc is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="traceability 路线未启用或 Neo4j 不可用")
    return container._trace_svc


def get_doc_retrieval_svc(container=Depends(get_container)):
    if container._doc_retrieval_svc is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="document 路线未启用或 ChromaDB 不可用")
    return container._doc_retrieval_svc


def get_doc_ingestion_svc(container=Depends(get_container)):
    if container._doc_ingestion_svc is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="document 路线未启用")
    return container._doc_ingestion_svc


def get_gateway_svc(container=Depends(get_container)):
    if container._gateway_svc is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="agentic 路线未启用")
    return container._gateway_svc


def get_tenant(tenant: TenantContext = Depends(tenant_from_header)) -> TenantContext:
    return tenant
