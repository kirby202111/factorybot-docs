"""structlog + JSONRenderer，自动注入 trace_id/span_id。"""
from __future__ import annotations

import logging
from typing import Any

try:
    import structlog

    _HAS_STRUCTLOG = True
except Exception:  # pragma: no cover
    _HAS_STRUCTLOG = False


class LoggingContext:
    """structlog 配置 + 上下文注入。

    自动注入 ``trace_id``/``span_id``/``tenant``/``route`` 等字段。
    structlog 不可用时退回标准 logging。
    """

    _configured = False

    def __init__(self, service_name: str = "rag-service") -> None:
        self._service_name = service_name
        self._configure()

    def _configure(self) -> None:
        if not _HAS_STRUCTLOG or LoggingContext._configured:
            return
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            cache_logger_on_first_use=True,
        )
        LoggingContext._configured = True

    def get_logger(self, **initial: Any) -> Any:
        if not _HAS_STRUCTLOG:
            return logging.getLogger(self._service_name)
        logger = structlog.get_logger(self._service_name)
        if initial:
            logger = logger.bind(**initial)
        return logger

    @staticmethod
    def bind(**kwargs: Any) -> None:
        """绑定上下文字段（随 async contextvar 流动）。"""
        if _HAS_STRUCTLOG:
            structlog.contextvars.bind_contextvars(**kwargs)

    @staticmethod
    def clear() -> None:
        if _HAS_STRUCTLOG:
            structlog.contextvars.clear_contextvars()
