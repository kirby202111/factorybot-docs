"""ObservabilityPort 的默认实现：组合 Tracing + MetricsCollector + Redactor。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from app.shared.obs.logging_ctx import LoggingContext
from app.shared.obs.metrics import MetricsCollector
from app.shared.obs.port import ObservabilityPort
from app.shared.obs.redactor import Redactor
from app.shared.obs.tracing import Tracing


class Observability(ObservabilityPort):
    """可观测底座默认实现。

    业务节点依赖 ``ObservabilityPort``（DIP）；本类是具体实现，组合
    ``Tracing``(OTel) + ``MetricsCollector``(prometheus) + ``Redactor``(脱敏)。
    任一子系统失败均不反噬业务（只读旁路）。
    """

    def __init__(
        self,
        service_name: str = "rag-service",
        tracing: Tracing | None = None,
        metrics: MetricsCollector | None = None,
        redactor: Redactor | None = None,
        logging_ctx: LoggingContext | None = None,
    ) -> None:
        self.tracing = tracing or Tracing()
        self.metrics = metrics or MetricsCollector()
        self.redactor = redactor or Redactor()
        self.logging = logging_ctx or LoggingContext(service_name)

    # ── span ──
    def llm_span(self, *, model: str, prompt_version: str):
        return self.tracing.llm_span(model=model, prompt_version=prompt_version)

    def session_span(self, *, session_id: str, route: str):
        return self.tracing.session_span(session_id=session_id, route=route)

    def retrieval_span(self, *, route: str, kind: str):
        return self.tracing.retrieval_span(route=route, kind=kind)

    def projection_span(self, *, context: str, event_type: str):
        return self.tracing.projection_span(context=context, event_type=event_type)

    # ── metrics ──
    def record_llm(self, *, model: str, tokens: int, latency_ms: int, prompt_version: str) -> None:
        try:
            self.metrics.llm_call_duration.labels(model=model, prompt_version=prompt_version).observe(latency_ms / 1000)
            self.metrics.llm_tokens.labels(model=model).inc(tokens)
        except Exception:  # 观测失败不反噬业务
            pass

    def record_retrieval(self, *, route: str, hits: int, latency_ms: int) -> None:
        try:
            if route == "A":
                self.metrics.trace_query_duration.observe(latency_ms / 1000)
            elif route == "B":
                self.metrics.doc_chroma_query_duration.observe(latency_ms / 1000)
                self.metrics.doc_search_hits.observe(hits)
        except Exception:
            pass

    def record_projection(self, *, context: str, action: str, latency_ms: int) -> None:
        try:
            self.metrics.projection_events.labels(context=context, action=action).inc()
            self.metrics.projection_lag.observe(latency_ms)
        except Exception:
            pass

    # ── redact ──
    def redact(self, text: str) -> str:
        return self.redactor.redact(text)

    @contextmanager
    def bind_context(self, **fields) -> Iterator[None]:
        """绑定日志上下文字段（structlog contextvar）。"""
        self.logging.bind(**fields)
        try:
            yield
        finally:
            self.logging.clear()
