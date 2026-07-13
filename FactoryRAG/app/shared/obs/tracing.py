"""OTel span 封装。

提供 ``session_span``/``retrieval_span``/``projection_span``/``llm_span`` 四类语义 span。
OTel SDK 不可用时降级为空 context manager，观测失败不反噬业务（只读旁路）。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

try:  # OTel 可选；未安装时降级为 no-op span。
    from opentelemetry import trace

    _TRACER: trace.Tracer | None = trace.get_tracer("rag-service")
except Exception:  # pragma: no cover - 环境差异
    _TRACER = None


class Tracing:
    """OTel span 封装。失败不反噬业务。"""

    def __init__(self, tracer=None) -> None:
        self._tracer = tracer or _TRACER

    @contextmanager
    def _span(self, name: str, **attributes) -> Iterator[None]:
        if self._tracer is None:
            yield
            return
        try:
            with self._tracer.start_as_current_span(name) as span:
                for k, v in attributes.items():
                    if v is not None:
                        span.set_attribute(k, v)
                yield
        except Exception as exc:  # 观测失败不反噬业务
            logger.debug("tracing span '%s' 失败: %s", name, exc)
            yield

    def session_span(self, *, session_id: str, route: str):
        return self._span("rag.session", session_id=session_id, route=route)

    def retrieval_span(self, *, route: str, kind: str):
        return self._span("rag.retrieval", route=route, kind=kind)

    def projection_span(self, *, context: str, event_type: str):
        return self._span("rag.projection", context=context, event_type=event_type)

    def llm_span(self, *, model: str, prompt_version: str):
        return self._span("rag.llm", model=model, prompt_version=prompt_version)
