"""API 路由层（统一注册 + 横切）。

``register_routers`` 按路线级开关注册（灰度引入：先 B 再 A，E 收口）；
``/health``/``/ready``/``/metrics`` 始终注册。所有业务端点经 ``TenantMiddleware``
注入 ``TenantContext``，出站自动透传 ``X-Tenant-Scope`` + ``traceparent``。
"""
from app.api.deps import (
    get_container,
    get_doc_ingestion_svc,
    get_doc_retrieval_svc,
    get_gateway_svc,
    get_trace_svc,
)
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestLogMiddleware, TenantMiddleware
from app.api.register import register_routers

__all__ = [
    "register_routers",
    "register_exception_handlers",
    "TenantMiddleware",
    "RequestLogMiddleware",
    "get_container",
    "get_trace_svc",
    "get_doc_retrieval_svc",
    "get_doc_ingestion_svc",
    "get_gateway_svc",
]
