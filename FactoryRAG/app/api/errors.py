"""全局异常处理。只读旁路：图库/向量库崩返回 503 不阻塞生产。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError):
        # 强制带版本红线等入口校验失败 -> 400
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("未处理异常")
        return JSONResponse(status_code=500, content={"detail": "内部错误"})
