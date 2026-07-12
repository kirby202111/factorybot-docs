"""ObservabilityContext：不可变上下文，随会话流动。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ObservabilityContext:
    session_id: str
    trace_id: str
    tenant_id: str
    workshop: str = ""
    line: str = ""
    level: str = "L1"            # L1 | L2 | L3
    prompt_version: str = "p_v1"
    step_no: int = 0
    capability: Optional[str] = None

    def base_attributes(self) -> dict[str, str]:
        return {
            "agent.session_id": self.session_id,
            "agent.level": self.level,
            "agent.tenant_id": self.tenant_id,
            "agent.tenant.workshop": self.workshop,
            "agent.tenant.line": self.line,
            "agent.llm.prompt_version": self.prompt_version,
        }

    def with_step(self, step_no: int) -> "ObservabilityContext":
        return ObservabilityContext(
            session_id=self.session_id,
            trace_id=self.trace_id,
            tenant_id=self.tenant_id,
            workshop=self.workshop,
            line=self.line,
            level=self.level,
            prompt_version=self.prompt_version,
            step_no=step_no,
            capability=self.capability,
        )
