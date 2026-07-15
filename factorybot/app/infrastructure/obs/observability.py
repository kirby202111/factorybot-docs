"""可观测组件装配：实现 ObservabilityPort，组合 Tracing + Metrics + LlmCallLogger。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.infrastructure.obs.context import ObservabilityContext
from app.infrastructure.obs.llm_call_logger import LlmCallLogger, LlmCallRecord
from app.infrastructure.obs.logging import get_logger
from app.infrastructure.obs.metrics import MetricsCollector
from app.infrastructure.obs.tracing import Tracing

_log = get_logger("obs")


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
        """观测旁路异步调用：不反噬业务，但任务异常必须可见（记 ERROR 日志）。

        不再用 get_event_loop()（3.12 已弃用）及其 run_until_complete 兜底
        （唯一调用方 llm_called 只在 async 上下文内被调，该分支为死代码）。
        """
        fn_name = getattr(fn, "__name__", repr(fn))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中的事件循环（当前无 sync 调用方，防御性兜底）：记 warning 后跳过
            _log.warning("obs.safe_async.no_running_loop", fn=fn_name)
            return
        task = loop.create_task(fn(*args, **kwargs))

        def _on_done(t: asyncio.Task) -> None:
            # done-callback 取 task 异常记日志，避免"Task exception was never retrieved"静默丢失
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                _log.error(
                    "obs.background_task_failed", fn=fn_name,
                    error=repr(exc), error_type=type(exc).__name__,
                )

        task.add_done_callback(_on_done)


def build_observability() -> "Observability":
    return Observability(
        tracing=Tracing(),
        metrics=MetricsCollector(),
        llm_logger=LlmCallLogger(),
    )
