"""L1 诊断路由：POST /agent/diagnose。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_container_dep, tenant_from_headers
from app.api.schemas import DiagnosisReportResponse, DiagnosisRequest
from app.application.diagnosis_service import DiagnosisService
from app.container import Container

router = APIRouter(tags=["L1-Diagnosis"])


@router.post("/agent/diagnose", response_model=DiagnosisReportResponse)
async def diagnose(
    req: DiagnosisRequest,
    tenant=Depends(tenant_from_headers),
    c: Container = Depends(get_container_dep),
) -> DiagnosisReportResponse:
    svc: DiagnosisService = c.diagnosis_service
    report = await svc.diagnose(
        req.question, tenant, serial_no=req.serial_no,
        work_order_id=req.work_order_id, route_version=req.route_version,
        subgraph_ref=req.subgraph_ref,
    )
    return DiagnosisReportResponse(
        summary=report.summary,
        confidence=report.confidence,
        hypotheses=report.hypotheses,
        subgraph_ref=report.subgraph_ref,
        route_version=report.route_version,
        evidence_refs=report.evidence_refs,
        disclaimer=report.disclaimer,
        needs_human_review=report.needs_human_review,
    )
