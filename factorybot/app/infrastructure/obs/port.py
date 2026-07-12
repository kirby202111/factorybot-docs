"""ObservabilityPort：抽象接口，业务节点依赖它而非 OTel/prometheus 具体实现（DIP）。

依赖倒置：L1/L2/L3 的业务代码只依赖 ObservabilityPort，不直接 import OTel 或
prometheus_client，便于替换实现与单测。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Protocol, Optional

from app.infrastructure.obs.context import ObservabilityContext


class ObservabilityPort(Protocol):
    """可观测抽象端口。"""

    @contextmanager
    def session_span(self, obs_ctx: ObservabilityContext) -> Iterator[Any]: ...

    @contextmanager
    def tool_span(self, obs_ctx: ObservabilityContext, tool_name: str,
                  bounded_context: str) -> Iterator[Any]: ...

    @contextmanager
    def llm_span(self, obs_ctx: ObservabilityContext, model: str,
                 prompt_version: str) -> Iterator[Any]: ...

    # 指标
    def tool_ok(self, tool: str, latency_s: float) -> None: ...
    def tool_denied(self, tool: str) -> None: ...
    def tool_error(self, tool: str) -> None: ...
    def llm_called(self, model: str, prompt_version: str, prompt_tokens: int,
                   completion_tokens: int, latency_ms: int, finish_reason: str,
                   obs_ctx: Optional[ObservabilityContext] = None) -> None: ...
    def low_confidence(self, level: str) -> None: ...
    def session_finished(self, level: str, status: str) -> None: ...
