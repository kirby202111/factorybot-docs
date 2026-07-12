"""可观测组件装配：实现 ObservabilityPort，组合 Tracing + Metrics + LlmCallLogger。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.infrastructure.obs.context import ObservabilityContext
from app.infrastructure.obs.llm_call_logger import LlmCallLogger, LlmCallRecord
from app.infrastructure.obs.metrics import MetricsCollector
from app.infrastructure.obs.tracing import Tracing


class Observability:
    """ObservabilityPort 的具体实现。观测是只读旁路，内部异常一律吞掉。"""

    def __init__(self, tracing: Tracing, metrics: MetricsCollector,
                 llm_logger: LlmCallLogger) -> None:
        self._tracing = tracing
        self._metrics = metrics
        self._llm_logger = llm_logger

    @contextmanager
    def session_span(self, obs_ctx: ObservabilityContext) -> Iterator[Any]:
        with self._tracing.session_span(obs_ctx):
            yield

    @contextmanager
    def tool_span(self, obs_ctx: ObservabilityContext, tool_name: str,
                  bounded_context: str) -> Iterator[Any]:
        with self._tracing.tool_span(obs_ctx, tool_name, bounded_context):
            yield

    @contextmanager
    def llm_span(self, obs_ctx: ObservabilityContext, model: str,
                 prompt_version: str) -> Iterator[Any]:
        with self._tracing.llm_span(obs_ctx, model, prompt_version):
            yield

    def tool_ok(self, tool: str, latency_s: float) -> None:
        self._safe(self._metrics.tool_ok, tool, latency_s)

    def tool_denied(self, tool: str) -> None:
        self._safe(self._metrics.tool_denied, tool)

    def tool_error(self, tool: str) -> None:
        self._safe(self._metrics.tool_error, tool)

    def llm_called(self, model: str, prompt_version: str, prompt_tokens: int,
                   completion_tokens: int, latency_ms: int, finish_reason: str,
                   obs_ctx: Optional[ObservabilityContext] = None) -> None:
        self._safe(
            self._metrics.llm_called, model, prompt_version,
            prompt_tokens, completion_tokens, latency_ms, finish_reason, obs_ctx,
        )
        if obs_ctx is not None:
            import uuid
            rec = LlmCallRecord(
                call_id=str(uuid.uuid4()),
                session_id=obs_ctx.session_id,
                step_no=obs_ctx.step_no,
                model=model,
                prompt_version=prompt_version,
                prompt_token_count=prompt_tokens,
                completion_token_count=completion_tokens,
                latency_ms=latency_ms,
                finish_reason=finish_reason,
                capability=obs_ctx.capability or "",
            )
            self._safe_async(self._llm_logger.log, rec)

    def low_confidence(self, level: str) -> None:
        self._safe(self._metrics.low_confidence, level)

    def session_started(self, level: str) -> None:
        self._safe(self._metrics.session_started, level)

    def session_ended(self, level: str) -> None:
        self._safe(self._metrics.session_ended, level)

    def session_finished(self, level: str, status: str) -> None:
        self._safe(self._metrics.session_finished, level, status)

    # ---- 只读旁路：任何观测异常都不反噬业务 ----
    def _safe(self, fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            pass

    def _safe_async(self, fn, *args, **kwargs) -> None:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(fn(*args, **kwargs))
            else:
                loop.run_until_complete(fn(*args, **kwargs))
        except Exception:
            pass


def build_observability() -> "Observability":
    return Observability(
        tracing=Tracing(),
        metrics=MetricsCollector(),
        llm_logger=LlmCallLogger(),
    )
