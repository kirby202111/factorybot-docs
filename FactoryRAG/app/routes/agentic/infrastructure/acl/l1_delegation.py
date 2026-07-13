"""E 委托 agent-service L1（``POST /agent/diagnose``，60s）。

决策 #1 traceparent 全链路：E 委托 L1 时手动注入 ``traceparent``；
L1 ``main.py`` 挂 ``opentelemetry-instrumentation-fastapi`` 为硬要求，
接收 incoming traceparent 并续接 trace，出站 httpx instrumentation 自动透传到 A/B/MES。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class L1DelegationClient:
    """L1 诊断委托客户端。"""

    def __init__(self, *, http: Any, timeout: float = 60.0) -> None:
        self._http = http
        self._timeout = timeout

    async def delegate(self, *, question: str, tenant: Any, traceparent: str) -> dict[str, Any]:
        headers = {**tenant.headers(), "traceparent": traceparent}
        resp = await self._http.post(
            "/agent/diagnose",
            json={"question": question},
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    async def ping(self) -> bool:
        """L1 健康探测（启动期/降级判断）。"""
        try:
            resp = await self._http.get("/health", timeout=1.0)
            return resp.status_code == 200
        except Exception:
            return False
