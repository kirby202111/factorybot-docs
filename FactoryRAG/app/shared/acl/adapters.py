"""路线间调用的 Adapter：InProcess（单服务内直调）+ Http（拆服务后）。

**Port 只收原语**（见 ports.py），故 DTO 构造职责落在本层：
- InProcess Adapter 把原语组装成各路线 DTO 后直调 application service（决策 #4）。
  路线 DTO 的 import **延迟到方法体内**，保持 ``import app.shared.acl`` 不触发
  路线模块加载（与"重依赖懒导入"口径一致）；单服务模式下路线必然就绪。
- Http Adapter 把原语组装成端点 JSON（匹配各路线 router 的请求 schema），拆服务时
  仅改容器绑定，业务代码零改动。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.shared.tenant.context import TenantContext
from app.shared.tenant.propagation import TenantPropagator


def _iso(value: datetime | None) -> str | None:
    """datetime -> ISO 字符串（httpx ``json=`` 不识别 datetime）。"""
    return value.isoformat() if isinstance(value, datetime) else value


class InProcessTraceRagAdapter:
    """单服务内：直调 A 的 TraceRetrievalService（决策 #4）。

    ``svc`` 是 Container 注入的 A 路线 application service 实例；本类在方法体内
    懒 import A 的 DTO 并构造，调用方只传原语，零跨路线 import。
    """

    def __init__(self, svc: Any) -> None:
        self._svc = svc

    async def query(
        self,
        question: str,
        tenant: TenantContext,
        *,
        seed_kind: str | None = None,
        seed_value: str | None = None,
        as_of: datetime | None = None,
        route_version: str | None = None,
    ) -> Any:
        from app.routes.traceability.domain.seed import Seed, SeedKind, TraceQuery

        seed = None
        if seed_kind and seed_value:
            seed = Seed(kind=SeedKind(seed_kind), value=seed_value)
        req = TraceQuery(
            question=question, seed=seed, as_of=as_of, route_version=route_version or None
        )
        return await self._svc.retrieve_and_synthesize(req, tenant)

    async def expand(
        self,
        kind: str,
        value: str,
        tenant: TenantContext,
        *,
        as_of: datetime | None = None,
        route_version: str | None = None,
    ) -> Any:
        from app.routes.traceability.domain.seed import ExpandRequest, SeedKind

        req = ExpandRequest(
            kind=SeedKind(kind), value=value, as_of=as_of, route_version=route_version or None
        )
        return await self._svc.expand_subgraph(req, tenant)


class HttpTraceRagAdapter:
    """拆服务后：httpx -> rag-service A，业务代码零改动。

    原语组装成 A 端点 JSON（``TraceQuery`` / ``ExpandRequest`` schema）。
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._propagator = TenantPropagator()

    async def query(
        self,
        question: str,
        tenant: TenantContext,
        *,
        seed_kind: str | None = None,
        seed_value: str | None = None,
        as_of: datetime | None = None,
        route_version: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question": question,
            "as_of": _iso(as_of),
            "route_version": route_version,
        }
        if seed_kind and seed_value:
            payload["seed"] = {"kind": seed_kind, "value": seed_value}
        resp = await self._client.post(
            f"{self._base_url}/rag/trace/query",
            json=payload,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def expand(
        self,
        kind: str,
        value: str,
        tenant: TenantContext,
        *,
        as_of: datetime | None = None,
        route_version: str | None = None,
    ) -> dict[str, Any]:
        payload = {"kind": kind, "value": value, "as_of": _iso(as_of), "route_version": route_version}
        resp = await self._client.post(
            f"{self._base_url}/rag/trace/expand",
            json=payload,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


class InProcessDocRagAdapter:
    """单服务内：直调 B 的 DocumentRetrievalService（决策 #4）。

    原语 -> B 的 ``DocQuery`` / ``DocSearch``（方法体内懒 import）。
    ``doc_category`` 缺省归 ``GENERAL``；``route_version`` 空串归一化为 None。
    """

    def __init__(self, svc: Any) -> None:
        self._svc = svc

    async def query(
        self,
        question: str,
        tenant: TenantContext,
        *,
        route_version: str | None = None,
        doc_category: str | None = None,
        asset_id: str | None = None,
        doc_types: list[str] | None = None,
    ) -> Any:
        from app.routes.document.domain.answer import DocQuery
        from app.routes.document.domain.document import DocumentCategory, DocType

        req = DocQuery(
            question=question,
            doc_category=DocumentCategory(doc_category) if doc_category else DocumentCategory.GENERAL,
            route_version=route_version or None,
            asset_id=asset_id or None,
            doc_types=[DocType(dt) for dt in doc_types] if doc_types else None,
        )
        return await self._svc.retrieve_and_synthesize(req, tenant)

    async def search(
        self,
        query: str,
        tenant: TenantContext,
        *,
        route_version: str | None = None,
        asset_id: str | None = None,
        doc_types: list[str] | None = None,
        top_k: int = 20,
    ) -> list[Any]:
        from app.routes.document.domain.answer import DocSearch
        from app.routes.document.domain.document import DocType

        req = DocSearch(
            question=query,
            route_version=route_version or None,
            asset_id=asset_id or None,
            doc_types=[DocType(dt) for dt in doc_types] if doc_types else None,
            top_k=top_k,
        )
        return await self._svc.search_chunks(req, tenant)


class HttpDocRagAdapter:
    """拆服务后：httpx -> rag-doc-service，业务代码零改动。

    原语组装成 B 端点 JSON（``DocQuery`` / ``DocSearch`` schema）。
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._propagator = TenantPropagator()

    async def query(
        self,
        question: str,
        tenant: TenantContext,
        *,
        route_version: str | None = None,
        doc_category: str | None = None,
        asset_id: str | None = None,
        doc_types: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question": question,
            "route_version": route_version,
            "asset_id": asset_id,
            "doc_types": doc_types,
        }
        if doc_category:
            payload["doc_category"] = doc_category
        resp = await self._client.post(
            f"{self._base_url}/rag/docs/query",
            json=payload,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def search(
        self,
        query: str,
        tenant: TenantContext,
        *,
        route_version: str | None = None,
        asset_id: str | None = None,
        doc_types: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        payload = {
            "question": query,
            "route_version": route_version,
            "asset_id": asset_id,
            "doc_types": doc_types,
            "top_k": top_k,
        }
        resp = await self._client.post(
            f"{self._base_url}/rag/docs/search",
            json=payload,
            headers=self._propagator.outbound_headers(tenant),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
