"""``register_routers``：按路线级开关注册（灰度引入：先 B 再 A，E 收口）。

``/health``/``/ready``/``/metrics`` 始终注册。口径见《技术选型和实现方案》§4.2。
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.v1.chat_router import chat_router
from app.api.v1.doc_router import doc_router
from app.api.v1.trace_router import trace_router
from app.shared.config.rag_settings import RagSettings
from app.shared.web.container import Container
from app.shared.web.health import HealthRouter


def register_routers(app: FastAPI, settings: RagSettings, container: Container) -> None:
    # 始终注册健康检查
    HealthRouter().register(app)

    # 路线级开关（灰度引入：先 B 再 A，E 收口）
    if settings.document.enabled:
        app.include_router(doc_router())
    if settings.traceability.enabled:
        app.include_router(trace_router())
    if settings.agentic.enabled:
        app.include_router(chat_router())
