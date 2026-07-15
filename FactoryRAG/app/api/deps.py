"""依赖注入 provider：从容器取实例，FastAPI 路由用 ``Depends`` 注入。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.shared.tenant.context import TenantContext
from app.shared.tenant.dependency import tenant_from_header


def get_container(request: Request):
    return request.app.state.container


def _require_svc(container, attr: str, detail: str):
    """从容器取路线 service；未装配（路线未启用 / 存储不可用）则 503。

    五个 ``get_*_svc`` 共用同一结构（取容器属性 -> None 校验 -> 抛 503），
    收敛到此工厂避免重复；``attr`` 为容器上的私有 service 槽位名。
    """
    svc = getattr(container, attr)
    if svc is None:
        raise HTTPException(status_code=503, detail=detail)
    return svc


def get_trace_svc(container=Depends(get_container)):
    """A TraceRetrievalService（仅 traceability.enabled 时可用）。"""
    return _require_svc(container, "_trace_svc", "traceability 路线未启用或 Neo4j 不可用")


def get_doc_retrieval_svc(container=Depends(get_container)):
    """B DocumentRetrievalService（仅 document.enabled 且 ChromaDB 可用时可用）。"""
    return _require_svc(container, "_doc_retrieval_svc", "document 路线未启用或 ChromaDB 不可用")


def get_doc_ingestion_svc(container=Depends(get_container)):
    """B DocumentIngestionService（仅 document.enabled 时可用）。"""
    return _require_svc(container, "_doc_ingestion_svc", "document 路线未启用")


def get_gateway_svc(container=Depends(get_container)):
    """E GatewayService（仅 agentic.enabled 时可用）。"""
    return _require_svc(container, "_gateway_svc", "agentic 路线未启用")


def get_tenant(tenant: TenantContext = Depends(tenant_from_header)) -> TenantContext:
    return tenant
