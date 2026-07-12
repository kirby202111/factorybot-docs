"""Tracing：封装 OTel span 创建。无 OTel SDK 时退化为 no-op，不反噬业务。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from opentelemetry import trace

from app.infrastructure.obs.context import ObservabilityContext

_NOOP = object()


class Tracing:
    """OTel span 封装。未配置 exporter 时 tracer 为 no-op，安全。"""

    def __init__(self, service_name: str = "agent-service") -> None:
        self._tracer = trace.get_tracer(service_name)

    @contextmanager
    def session_span(self, obs_ctx: ObservabilityContext) -> Iterator[Any]:
        with self._tracer.start_as_current_span(
            "agent.session", attributes=obs_ctx.base_attributes()
        ) as span:
            try:
                yield span
            except Exception as e:
                span.record_exception(e)
                span.set_attribute("agent.status", "ERROR")
                raise

    @contextmanager
    def tool_span(self, obs_ctx: ObservabilityContext, tool_name: str,
                  bounded_context: str) -> Iterator[Any]:
        with self._tracer.start_as_current_span(
            "tool.invoke",
            attributes={
                "agent.tool.name": tool_name,
                "agent.tool.bounded_context": bounded_context,
                "agent.step_no": obs_ctx.step_no,
            },
        ) as span:
            try:
                yield span
            except Exception as e:
                span.record_exception(e)
                raise

    @contextmanager
    def llm_span(self, obs_ctx: ObservabilityContext, model: str,
                 prompt_version: str) -> Iterator[Any]:
        with self._tracer.start_as_current_span(
            "llm.invoke",
            attributes={
                "agent.llm.model": model,
                "agent.llm.prompt_version": prompt_version,
                "agent.step_no": obs_ctx.step_no,
            },
        ) as span:
            try:
                yield span
            except Exception as e:
                span.record_exception(e)
                raise
