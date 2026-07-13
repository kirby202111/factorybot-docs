"""rag-service FastAPI 入口。

单服务 + 共享内核承载 A/B/E 三路线。lifespan 编排：启动断言 -> 存储就绪探测 ->
路线装配 -> schema 初始化 -> consumer 启停。只读红线靠 ``ReadOnly*Gate`` 启动断言兜底。
口径见《rag-service-技术选型和实现方案》§4.1。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import register_exception_handlers, register_routers
from app.api.middleware import RequestLogMiddleware, TenantMiddleware
from app.config import load_settings
from app.shared.config.rag_settings import RagSettings
from app.shared.web.container import Container
from app.shared.web.lifespan import lifespan


def create_app(settings: RagSettings | None = None) -> FastAPI:
    settings = settings or load_settings()
    container = Container(settings)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        async with lifespan(app, settings, container):
            yield

    app = FastAPI(
        title="rag-service",
        description="单服务 + 共享内核承载 A(追溯型)/B(文档型)/E(Agentic) 三路线 RAG",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.container = container
    app.state.settings = settings

    app.add_middleware(TenantMiddleware, propagator=container.tenant_propagator)
    app.add_middleware(RequestLogMiddleware, obs=container.obs)

    register_routers(app, settings, container)
    register_exception_handlers(app)
    return app


app = create_app()
