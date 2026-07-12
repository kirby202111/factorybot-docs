"""L3 编排路由：start / confirm / state。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_container_dep, tenant_from_headers
from app.api.schemas import (
    ConfirmRequest, ConfirmResponse, L3StartRequest, L3StartResponse, L3StateResponse,
)
from app.application.l3_orchestrator import L3Orchestrator
from app.container import Container

router = APIRouter(prefix="/agent/l3", tags=["L3-Orchestration"])


@router.post("/{scenario}/start", response_model=L3StartResponse)
async def start_l3(
    scenario: str,
    req: L3StartRequest,
    tenant=Depends(tenant_from_headers),
    c: Container = Depends(get_container_dep),
) -> L3StartResponse:
    orch: L3Orchestrator = c.l3_orchestrator
    session = await orch.start(
        scenario, tenant,
        work_order_id=req.work_order_id, batch_id=req.batch_id,
        asset_id=req.asset_id, target_route_id=req.target_route_id,
        target_route_version=req.target_route_version, fault_time=req.fault_time,
        complaint_batch_id=req.complaint_batch_id,
    )
    return L3StartResponse(
        session_id=session.session_id, scenario=session.scenario.value,
        status=session.status.value, created_at=session.created_at.isoformat(),
    )


@router.post("/{session_id}/confirm", response_model=ConfirmResponse)
async def confirm_gate(
    session_id: str,
    req: ConfirmRequest,
    c: Container = Depends(get_container_dep),
) -> ConfirmResponse:
    orch: L3Orchestrator = c.l3_orchestrator
    decision = await orch.resume(session_id, req.step, req.approved, req.user_id)
    return ConfirmResponse(session_id=session_id, step=req.step, decision=decision)


@router.get("/{session_id}/state", response_model=L3StateResponse)
async def get_state(
    session_id: str,
    c: Container = Depends(get_container_dep),
) -> L3StateResponse:
    orch: L3Orchestrator = c.l3_orchestrator
    session = await orch.get_session(session_id)
    if session is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="session not found")
    pending = await orch.pending_step(session_id)
    return L3StateResponse(
        session_id=session.session_id, scenario=session.scenario.value,
        status=session.status.value, current_step=session.current_step,
        pending_step=pending, suspend_reason=session.suspend_reason,
    )
