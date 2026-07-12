"""structlog 配置：JSONRenderer + 自动注入 trace_id/span_id。"""
from __future__ import annotations

import structlog


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _inject_trace_ids,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO+
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _inject_trace_ids(_, __, event_dict: dict) -> dict:
    """注入当前 OTel trace_id/span_id（无活跃 span 时留空）。"""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            event_dict["trace_id"] = f"{ctx.trace_id:032x}"
            event_dict["span_id"] = f"{ctx.span_id:016x}"
    except Exception:
        pass
    return event_dict


def bind_session(session_id: str, tenant_id: str, level: str) -> None:
    """会话级上下文绑定，后续日志自动带上。"""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        session_id=session_id, tenant_id=tenant_id, level=level
    )


def get_logger(name: str = "agent"):
    return structlog.get_logger(name)
