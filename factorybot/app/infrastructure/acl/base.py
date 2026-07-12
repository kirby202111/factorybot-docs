"""ACL 基类：httpx 出站 + mock 模式回退 + traceparent 注入。"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from app.domain.tenant import TenantContext
from app.infrastructure.mock.fixture_loader import FixtureLoader


class BaseAclClient:
    """所有 ACL client 的基类。

    - mock=True：从 fixtures 读，不发真实 HTTP。
    - mock=False：httpx 调真实 REST，注入 X-Tenant-* header。
    """

    def __init__(
        self,
        http: Optional[httpx.AsyncClient] = None,
        base_url: str = "",
        fixtures: Optional[FixtureLoader] = None,
        mock: bool = True,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._fixtures = fixtures
        self._mock = mock

    # ---- 只读 GET ----
    async def _get(
        self, path: str, *, tenant: Optional[TenantContext] = None,
        params: Optional[dict] = None,
        fixture_rel: Optional[str] = None, fixture_key: Optional[str] = None,
    ) -> Any:
        if self._mock:
            assert self._fixtures is not None, "mock 模式需要 fixtures"
            return self._fixtures.lookup(fixture_rel, fixture_key)
        assert self._http is not None, "real 模式需要 httpx.AsyncClient"
        headers = tenant.headers() if tenant else {}
        resp = await self._http.get(
            f"{self._base_url}{path}", params=params, headers=headers, timeout=2.0,
        )
        resp.raise_for_status()
        return resp.json()

    # ---- 受限写 POST（带 confirmation token）----
    async def _post(
        self, path: str, body: dict, *, tenant: Optional[TenantContext] = None,
        confirmation: Optional[Any] = None,
        fixture_rel: Optional[str] = None, fixture_key: Optional[str] = None,
    ) -> Any:
        if self._mock:
            assert self._fixtures is not None, "mock 模式需要 fixtures"
            # 写操作 mock：返回 fixture 中的成功响应
            return self._fixtures.lookup(fixture_rel, fixture_key, default="_default")
        assert self._http is not None, "real 模式需要 httpx.AsyncClient"
        headers = tenant.headers() if tenant else {}
        if confirmation is not None:
            headers["X-Confirmation-Token"] = confirmation.id
            headers["X-Confirmed-By"] = confirmation.user_id
        resp = await self._http.post(
            f"{self._base_url}{path}", json=body, headers=headers, timeout=3.0,
        )
        resp.raise_for_status()
        return resp.json()
