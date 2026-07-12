"""LlmCallLogger：落 llm_call_log 表（mock 模式下进程内记录）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class LlmCallRecord:
    call_id: str
    session_id: str
    step_no: int
    model: str
    prompt_version: str
    prompt_token_count: int
    completion_token_count: int
    latency_ms: int
    finish_reason: str = ""
    tool_calls_produced: int = 0
    capability: str = ""
    phase: str = ""
    occurred_at: datetime = field(default_factory=datetime.now)


class LlmCallLogger:
    """LLM 调用日志仓储。真实模式落 MySQL llm_call_log；mock 模式进程内 list。"""

    def __init__(self) -> None:
        self._records: list[LlmCallRecord] = []

    async def log(self, rec: LlmCallRecord) -> None:
        self._records.append(rec)

    async def list_for_session(self, session_id: str) -> list[LlmCallRecord]:
        return [r for r in self._records if r.session_id == session_id]
