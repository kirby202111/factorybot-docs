"""FastAPI 入口：lifespan 装配 + 三层写防线启动断言（红线校验）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import diagnosis_router, draft_router, orchestration_router
from app.container import get_container
from app.domain.errors import ResourceAccessError
from app.infrastructure.obs.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log = get_logger("startup")
    c = get_container()
    # 三层写防线启动断言：诊断 ReadOnlyToolGate / 编排 WriteToolGate / ModelRouter EvalGate
    c.validate_on_startup()
    log.info(
        "startup.assertions.ok",
        diagnosis_tools=len(c.diagnosis_registry.all()),
        orchestration_tools=len(c.orchestration_registry.all()),
        mock=c.settings.is_mock,
    )
    yield
    await c.shutdown()
    get_logger("shutdown").info("shutdown")


app = FastAPI(
    title="MES Agent Service",
    version="0.1.0",
    description="诊断 / 草稿 / 编排（LangGraph + FastAPI）",
    lifespan=lifespan,
)

app.include_router(diagnosis_router)
app.include_router(draft_router)
app.include_router(orchestration_router)


@app.exception_handler(ResourceAccessError)
async def _resource_access_handler(request: Request, exc: ResourceAccessError) -> JSONResponse:
    """多租户隔离：资源不存在 / 跨租户访问统一返回 404（隐藏存在性）。"""
    return JSONResponse(status_code=404, content={"detail": "resource not found"})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "mock": get_container().settings.is_mock}
