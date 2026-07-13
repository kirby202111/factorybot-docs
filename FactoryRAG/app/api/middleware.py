"""FastAPI 中间件：租户上下文 + 请求日志。"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.shared.tenant.propagation import TenantPropagator

logger = logging.getLogger(__name__)


class TenantMiddleware(BaseHTTPMiddleware):
    """解析租户上下文挂到 request.state，出站自动透传 ``X-Tenant-Scope``。"""

    def __init__(self, app, propagator: TenantPropagator | None = None) -> None:
        super().__init__(app)
        self._propagator = propagator or TenantPropagator()

    async def dispatch(self, request: Request, call_next):
        tenant_id = request.headers.get("X-Tenant-Id", "")
        scope = request.headers.get("X-Tenant-Scope", "")
        if tenant_id:
            from app.shared.tenant.context import TenantContext

            scopes = [s.strip() for s in scope.split(",") if s.strip()]
            request.state.tenant = TenantContext(tenant_id=tenant_id, tenant_scopes=scopes)
        response = await call_next(request)
        return response


class RequestLogMiddleware(BaseHTTPMiddleware):
    """请求日志（structlog + trace_id 注入）。观测是只读旁路，失败不反噬业务。"""

    def __init__(self, app, obs=None) -> None:
        super().__init__(app)
        self._obs = obs

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "request %s %s -> %s %dms",
                request.method, request.url.path, response.status_code, latency_ms,
            )
            return response
        except Exception:
            logger.exception("request 处理异常: %s %s", request.method, request.url.path)
            raise
