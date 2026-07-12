"""L1 诊断会话。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.domain.tenant import TenantContext


class SessionStatus(str, Enum):
    RUNNING = "RUNNING"
    DONE = "DONE"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


class DiagnosisSession(BaseModel):
    """一次 L1 诊断会话。"""

    session_id: str
    user_id: str = "system"
    tenant: TenantContext
    question: str
    serial_no: str | None = None
    work_order_id: str | None = None
    batch_no: str | None = None
    subgraph_ref: str | None = None
    route_version: str | None = None
    status: SessionStatus = SessionStatus.RUNNING
    prompt_version: str = "p_v1"
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    def touch(self) -> None:
        self.updated_at = datetime.now()
