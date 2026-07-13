"""健康检查端点：/health /ready /metrics。

补齐三路线均缺失的运维底座。口径见《rag-service-技术选型和实现方案》§4.3。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import PlainTextResponse

try:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    _HAS_PROM = True
except Exception:  # pragma: no cover
    _HAS_PROM = False


class HealthRouter:
    """``/health`` / ``/ready`` / ``/metrics`` 路由注册器。始终注册（不受路线开关影响）。"""

    def __init__(self) -> None:
        self.router = APIRouter(tags=["health"])
        self.router.add_api_route("/health", self._health, methods=["GET"])
        self.router.add_api_route("/ready", self._ready, methods=["GET"])
        self.router.add_api_route("/metrics", self._metrics, methods=["GET"], response_class=PlainTextResponse)

    async def _health(self) -> dict[str, str]:
        """进程存活（K8s liveness）。"""
        return {"status": "ok"}

    async def _ready(self, request: Request) -> dict[str, Any]:
        """依赖连通性 + consumer 位点滞后（K8s readiness）。

        不可用的路线标记降级（返回 503 由调用方判定），不拖垮其他路线。
        """
        container: Any = request.app.state.container
        health: dict[str, bool] = getattr(request.app.state, "storage_health", None) or {}
        # 启动后重新探测一次（运行期健康）
        try:
            health = await container.engines.probe()
        except Exception:
            pass
        consumer_lag: dict[str, int] = {}
        for c in getattr(container, "consumers", []):
            # consumer 滞后度（占位：实际从 offset 表与 Kafka high watermark 算）
            consumer_lag[c.group_id] = 0
        all_ok = all(health.values()) if health else False
        return {
            "status": "ok" if all_ok else "degraded",
            "storages": health,
            "consumer_lag": consumer_lag,
            "routes": {
                "document": container.settings.document.enabled,
                "traceability": container.settings.traceability.enabled,
                "agentic": container.settings.agentic.enabled,
            },
        }

    async def _metrics(self) -> PlainTextResponse:
        """prometheus 指标（rag_ 前缀）。"""
        if not _HAS_PROM:
            return PlainTextResponse("# prometheus_client 不可用\n")
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    def register(self, app: FastAPI) -> None:
        app.include_router(self.router)
