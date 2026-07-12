"""FastAPI 入口：lifespan 装配 + 三层写防线启动断言（红线校验）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import diagnosis_router, draft_router, l3_router
from app.container import get_container
from app.infrastructure.obs.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log = get_logger("startup")
    c = get_container()
    # 三层写防线启动断言：L1 ReadOnlyToolGate / L3 WriteToolGate / ModelRouter EvalGate
    c.validate_on_startup()
    log.info(
        "startup.assertions.ok",
        l1_tools=len(c.l1_registry.all()),
        l3_tools=len(c.l3_registry.all()),
        mock=c.settings.is_mock,
    )
    yield
    get_logger("shutdown").info("shutdown")


app = FastAPI(
    title="MES Agent Service",
    version="0.1.0",
    description="L1 诊断 / L2 草稿 / L3 编排（LangGraph + FastAPI）",
    lifespan=lifespan,
)

app.include_router(diagnosis_router)
app.include_router(draft_router)
app.include_router(l3_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "mock": get_container().settings.is_mock}
