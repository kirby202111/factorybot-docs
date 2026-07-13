"""不可变的可观测上下文，随会话流动。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ObservabilityContext:
    """随会话流动的可观测上下文（不可变）。

    字段对齐 agent-service 五层可观测模型：
    ``session_id``/``trace_id``/``tenant``/``route``/``prompt_version``/``step_no``。
    """

    session_id: str = ""
    trace_id: str = ""
    tenant: str = ""
    route: str = ""               # "A" | "B" | "E" | "shared"
    prompt_version: str = ""
    step_no: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def with_step(self, step_no: int) -> "ObservabilityContext":
        return ObservabilityContext(
            session_id=self.session_id,
            trace_id=self.trace_id,
            tenant=self.tenant,
            route=self.route,
            prompt_version=self.prompt_version,
            step_no=step_no,
            extra=dict(self.extra),
        )

    def as_logging_dict(self) -> dict[str, Any]:
        """供 structlog 注入的扁平字段。"""
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "tenant": self.tenant,
            "route": self.route,
            "prompt_version": self.prompt_version,
            "step_no": self.step_no,
        }
        d.update(self.extra)
        return d
