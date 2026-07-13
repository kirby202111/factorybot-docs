"""路线间调用的 Adapter：InProcess（单服务内直调）+ Http（拆服务后）。

InProcess Adapter 只持有被调服务的实例（DI 注入），**不 import 路线模块**
（保持 shared 不依赖 routes）。具体服务实例由 Container（组合根）注入。
拆服务时：把 DI 容器里 Port -> InProcess Adapter 的绑定换成 Http Adapter，业务代码零改动。
"""
from __future__ import annotations

from typing import Any

import httpx

from app.shared.tenant.context import TenantContext
from app.shared.tenant.propagation import TenantPropagator


class InProcessTraceRagAdapter:
    """单服务内：直调 A 的 TraceRetrievalService（决策 #4）。

    ``svc`` 是 Container 注入的 A 路线 application service 实例；本类不 import
    ``routes.traceability``，仅 duck-type 调用其方法。
    """

    def __init__(self, svc: Any) -> None:
        self._svc = svc

    async def query(self, req: Any, tenant: TenantContext) -> Any:
        return await self._svc.retrieve_and_synthesize(req, tenant)

    async def expand(self, req: Any, tenant: TenantContext) -> Any:
        return await self._svc.expand_subgraph(req, tenant)


class HttpTraceRagAdapter:
    """拆服务后：httpx -> rag-service A，业务代码零改动。"""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._propagator = TenantPropagator()

    async def query(self, req: Any, tenant: TenantContext) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self._base_url}/rag/trace/query",
            json=req.model_dump(mode="json") if hasattr(req, "model_dump") else req,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def expand(self, req: Any, tenant: TenantContext) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self._base_url}/rag/trace/expand",
            json=req.model_dump(mode="json") if hasattr(req, "model_dump") else req,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


class InProcessDocRagAdapter:
    """单服务内：直调 B 的 DocumentRetrievalService（决策 #4）。"""

    def __init__(self, svc: Any) -> None:
        self._svc = svc

    async def query(self, req: Any, tenant: TenantContext) -> Any:
        return await self._svc.retrieve_and_synthesize(req, tenant)

    async def search(self, req: Any, tenant: TenantContext) -> list[Any]:
        return await self._svc.search_chunks(req, tenant)


class HttpDocRagAdapter:
    """拆服务后：httpx -> rag-doc-service，业务代码零改动。"""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._propagator = TenantPropagator()

    async def query(self, req: Any, tenant: TenantContext) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self._base_url}/rag/docs/query",
            json=req.model_dump(mode="json") if hasattr(req, "model_dump") else req,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def search(self, req: Any, tenant: TenantContext) -> list[dict[str, Any]]:
        resp = await self._client.post(
            f"{self._base_url}/rag/docs/search",
            json=req.model_dump(mode="json") if hasattr(req, "model_dump") else req,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
