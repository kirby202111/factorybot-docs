"""FastAPI 请求/响应 schema。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.domain.draft import Draft, DraftKind
from app.domain.report import DiagnosisReport, Hypothesis


# ---- L1 ----
class DiagnosisRequest(BaseModel):
    question: str
    serial_no: str | None = None
    work_order_id: str | None = None
    version: str | None = None
    version_kind: str | None = None         # route|bom|rule|asset|standard
    version_ref_id: str | None = None       # route_id / asset_id / standard_id ...
    subgraph_ref: str | None = None


class DiagnosisReportResponse(BaseModel):
    session_id: str | None = None
    summary: str
    confidence: float
    hypotheses: list[Hypothesis]
    subgraph_ref: str = ""
    version: str | None = None
    version_kind: str | None = None
    version_ref_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    disclaimer: str = ""
    needs_human_review: bool = False


# ---- L2 ----
class DraftRequest(BaseModel):
    diagnosis_report: DiagnosisReport
    draft_kind: DraftKind


class DraftResponse(Draft):
    pass


# ---- L3 ----
class L3StartRequest(BaseModel):
    work_order_id: str | None = None
    batch_id: str | None = None
    asset_id: str | None = None
    target_route_id: str | None = None
    target_route_version: str | None = None
    fault_time: str | None = None
    complaint_batch_id: str | None = None


class L3StartResponse(BaseModel):
    session_id: str
    scenario: str
    status: str
    created_at: str


class ConfirmRequest(BaseModel):
    step: str
    approved: bool
    user_id: str = "u_zhang"


class ConfirmResponse(BaseModel):
    session_id: str
    step: str
    decision: str


class L3StateResponse(BaseModel):
    session_id: str
    scenario: str
    status: str
    current_step: str
    pending_step: str | None = None
    suspend_reason: str = ""
