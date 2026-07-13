"""对 MES 只读 REST 的 ACL 基类（出站）。

httpx 异步基类：自动注入 ``traceparent`` / 超时重试 / 租户 header / 只读断言。
方法名禁止写动词--由 ``ReadOnlyAclGate`` 在启动期扫描（§3.2）。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BaseReadonlyAclClient:
    """对 MES 只读 REST 的 httpx 异步基类。

    SRP：只管"出站只读 HTTP + traceparent/租户透传 + 超时重试"。
    子类方法名**禁止**写动词（create/update/delete/...），由 ``ReadOnlyAclGate``
    启动期扫描 ``dir(type(c))`` 兜底。MES 从不回写：rag-service 是只读旁路。
    """

    _WRITE_VERBS = {"create", "update", "delete", "post", "put", "patch", "remove", "save", "insert"}

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        tenant_propagator: Any | None = None,
        timeout: float = 2.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._tenant_propagator = tenant_propagator
        self._timeout = timeout

    async def _get(self, path: str, *, params: dict[str, Any] | None = None, tenant: Any | None = None) -> dict[str, Any]:
        """只读 GET。自动注入 traceparent + 租户 header。"""
        headers = self._build_headers(tenant)
        resp = await self._client.get(
            f"{self._base_url}{path}", params=params, headers=headers, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _build_headers(self, tenant: Any | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        # traceparent：由 OTel httpx instrumentation 自动注入；此处兜底手动注入。
        try:
            from opentelemetry import trace  # type: ignore

            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.is_valid:
                headers["traceparent"] = (
                    f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"
                )
        except Exception:
            pass
        # 租户透传
        if tenant is not None and self._tenant_propagator is not None:
            headers.update(self._tenant_propagator.outbound_headers(tenant))
        elif tenant is not None and hasattr(tenant, "headers"):
            headers.update(tenant.headers())
        return headers
