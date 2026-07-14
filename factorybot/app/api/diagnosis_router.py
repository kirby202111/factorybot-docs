"""诊断路由：POST /agent/diagnose。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_container_dep, tenant_from_headers
from app.api.schemas import DiagnosisReportResponse, DiagnosisRequest
from app.application.diagnosis_service import DiagnosisService
from app.container import Container
from app.domain.version import VersionAnchor

router = APIRouter(tags=["Diagnosis"])


@router.post("/agent/diagnose", response_model=DiagnosisReportResponse)
async def diagnose(
    req: DiagnosisRequest,
    tenant=Depends(tenant_from_headers),
    c: Container = Depends(get_container_dep),
) -> DiagnosisReportResponse:
    svc: DiagnosisService = c.diagnosis_service
    anchor = VersionAnchor.from_flat(req.version, req.version_kind, req.version_ref_id)
    report = await svc.diagnose(
        req.question, tenant, serial_no=req.serial_no,
        work_order_id=req.work_order_id, version_anchor=anchor,
        subgraph_ref=req.subgraph_ref,
    )
    return DiagnosisReportResponse(
        summary=report.summary,
        confidence=report.confidence,
        hypotheses=report.hypotheses,
        subgraph_ref=report.subgraph_ref,
        version=report.version,
        version_kind=report.version_kind,
        version_ref_id=report.version_ref_id,
        evidence_refs=report.evidence_refs,
        disclaimer=report.disclaimer,
        needs_human_review=report.needs_human_review,
    )
