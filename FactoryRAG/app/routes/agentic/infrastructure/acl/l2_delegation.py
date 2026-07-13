"""E 委托 agent-service L2（``POST /agent/draft``，30s）。透传 traceparent（决策 #1）。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class L2DelegationClient:
    """L2 草拟委托客户端。"""

    def __init__(self, *, http: Any, timeout: float = 30.0) -> None:
        self._http = http
        self._timeout = timeout

    async def delegate(
        self, *, draft_kind: str, context: dict, tenant: Any, traceparent: str
    ) -> dict[str, Any]:
        headers = {**tenant.headers(), "traceparent": traceparent}
        resp = await self._http.post(
            "/agent/draft",
            json={"kind": draft_kind, "context": context},
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    async def ping(self) -> bool:
        try:
            resp = await self._http.get("/health", timeout=1.0)
            return resp.status_code == 200
        except Exception:
            return False
