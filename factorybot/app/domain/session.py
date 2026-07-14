"""诊断会话。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor


class SessionStatus(str, Enum):
    RUNNING = "RUNNING"
    DONE = "DONE"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


class DiagnosisSession(BaseModel):
    """一次 诊断会话。"""

    session_id: str
    user_id: str = "system"
    tenant: TenantContext
    question: str
    serial_no: str | None = None
    work_order_id: str | None = None
    batch_no: str | None = None
    subgraph_ref: str | None = None
    # 版本一致性三段链第二段：物理锁定的版本锚点（扁平三字段 + version_anchor() 属性）
    version: str | None = None
    version_kind: str | None = None
    version_ref_id: str | None = None
    status: SessionStatus = SessionStatus.RUNNING
    prompt_version: str = "p_v1"
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    def version_anchor(self) -> VersionAnchor | None:
        return VersionAnchor.from_flat(self.version, self.version_kind, self.version_ref_id)

    def touch(self) -> None:
        self.updated_at = datetime.now()
