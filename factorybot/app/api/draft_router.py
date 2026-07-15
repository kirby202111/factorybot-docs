"""草稿路由：POST /agent/draft, GET /agent/draft/{id}/evidence。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_container_dep, tenant_from_headers
from app.api.schemas import DraftRequest, DraftResponse
from app.application.draft_service import DraftService
from app.container import Container

router = APIRouter(tags=["Draft"])


@router.post("/agent/draft", response_model=DraftResponse)
async def draft(
    req: DraftRequest,
    tenant=Depends(tenant_from_headers),
    c: Container = Depends(get_container_dep),
) -> DraftResponse:
    svc: DraftService = c.draft_service
    draft = await svc.draft(req.diagnosis_report, req.draft_kind, tenant)
    return DraftResponse(**draft.model_dump())


@router.get("/agent/draft/{draft_id}/evidence")
async def draft_evidence(
    draft_id: str,
    tenant=Depends(tenant_from_headers),
    c: Container = Depends(get_container_dep),
) -> list[dict]:
    svc: DraftService = c.draft_service
    return await svc.get_evidence(draft_id, tenant)
