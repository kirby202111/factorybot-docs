"""可观测抽象接口（DIP）：业务节点依赖它而非 OTel/prometheus 具体实现。"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObservabilityPort(Protocol):
    """可观测抽象接口。

    业务节点（LLM/检索/投影/委托）依赖此 Port，而非 OpenTelemetry /
    prometheus / structlog 的具体实现。这把可观测后端变成可替换件。
    """

    def llm_span(self, *, model: str, prompt_version: str) -> AbstractContextManager[None]:
        """LLM 调用 span。"""
        ...

    def session_span(self, *, session_id: str, route: str) -> AbstractContextManager[None]:
        """会话级 span（一次问答请求）。"""
        ...

    def retrieval_span(self, *, route: str, kind: str) -> AbstractContextManager[None]:
        """检索 span（A Cypher / B 向量检索）。"""
        ...

    def projection_span(self, *, context: str, event_type: str) -> AbstractContextManager[None]:
        """事件投影 span（A 图投影 / B 重索引）。"""
        ...

    def record_llm(self, *, model: str, tokens: int, latency_ms: int, prompt_version: str) -> None:
        """记录一次 LLM 调用的指标。"""
        ...

    def record_retrieval(self, *, route: str, hits: int, latency_ms: int) -> None:
        """记录一次检索的指标。"""
        ...

    def record_projection(self, *, context: str, action: str, latency_ms: int) -> None:
        """记录一次事件投影的指标。"""
        ...

    def redact(self, text: str) -> str:
        """脱敏纯函数入口。"""
        ...
