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
        confirmation_store=None,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._fixtures = fixtures
        self._mock = mock
        self._store = confirmation_store  # 受限写 client 注入；None 则只做 valid_for

    # ---- 只读 GET ----
    async def _get(
        self, path: str, *, tenant: Optional[TenantContext] = None,
        params: Optional[dict] = None,
        fixture_rel: Optional[str] = None, fixture_key: Optional[str] = None,
        allow_default: bool = False,
    ) -> Any:
        """mock 下从 fixtures 取。allow_default=False（默认）：key 未命中返回 None，
        镜像真实 REST 404，避免用 _default 占位数据冒充查询实体。"""
        if self._mock:
            assert self._fixtures is not None, "mock 模式需要 fixtures"
            return self._fixtures.lookup(fixture_rel, fixture_key, allow_default=allow_default)
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

    # ---- 只读 POST（复杂查询用 JSON body，非受限写）----
    async def _post_read(
        self, path: str, body: dict, *, tenant: Optional[TenantContext] = None,
        fixture_rel: Optional[str] = None, fixture_key: Optional[str] = None,
        allow_default: bool = False,
    ) -> Any:
        """只读 POST：复杂查询参数走 JSON body（FactoryRAG 检索端点均为 POST+body）。

        与 _get 同语义但不带 confirmation token（非受限写）。mock 下从 fixtures 取
        （allow_default=False 默认不冒充，镜像真实 REST 404）；real 下 POST+JSON body。
        """
        if self._mock:
            assert self._fixtures is not None, "mock 模式需要 fixtures"
            return self._fixtures.lookup(fixture_rel, fixture_key, allow_default=allow_default)
        assert self._http is not None, "real 模式需要 httpx.AsyncClient"
        headers = tenant.headers() if tenant else {}
        resp = await self._http.post(
            f"{self._base_url}{path}", json=body, headers=headers, timeout=2.0,
        )
        resp.raise_for_status()
        return resp.json()

    # ---- 只读 GET + to_view 一体 ----
    async def _get_view(self, view_cls, path, *, tenant: Optional[TenantContext] = None,
                        params: Optional[dict] = None,
                        fixture_rel: Optional[str] = None, fixture_key: Optional[str] = None,
                        allow_default: bool = False):
        """只读 GET + to_view 的常用组合（只读 ACL 方法标配）。

        mock 下 fixtures 透传成 View 形状；real 下 DTO 经 to_view 映射成 View。
        返回 list/dict 或需后处理的方法不适用，仍用 _get。
        """
        from app.infrastructure.acl.views import to_view
        dto = await self._get(path, tenant=tenant, params=params,
                              fixture_rel=fixture_rel, fixture_key=fixture_key,
                              allow_default=allow_default)
        return to_view(view_cls, dto)

    # ---- 受限写统一 token 校验 ----
    async def _validate_confirmation(self, confirmation, action: str) -> None:
        """受限写统一 token 校验：action 匹配 + (若注入 store) 存在性/一致性校验。

        保留现有安全语义：rework_write 注入 store 做 valid_for + store.validate 双校验；
        process_write/pass_write 未注入 store，只做 valid_for。统一与否是独立安全决策，
        此处不改变现有行为。
        """
        expected = f"{action}:{confirmation.session_id}"
        if not confirmation.valid_for(expected):
            raise PermissionError(f"token action 不匹配: expected={expected}")
        if self._store is not None and not await self._store.validate(
            confirmation.id, expected
        ):
            raise PermissionError("token 无效或已过期")
