"""E: ``POST /agent/chat`` / ``GET /agent/explain/{audit_id}``。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_gateway_svc, get_tenant
from app.routes.agentic.domain.answer import AgentAnswer, AnswerAuditView, ChatRequest
from app.shared.tenant.context import TenantContext


def chat_router() -> APIRouter:
    router = APIRouter(prefix="/agent", tags=["agentic-E"])

    @router.post("/chat", response_model=AgentAnswer)
    async def chat(
        req: ChatRequest,
        tenant: TenantContext = Depends(get_tenant),
        svc=Depends(get_gateway_svc),
    ) -> AgentAnswer:
        """统一问答入口：意图路由到 A/B + 委托 agent-service L1/L2。"""
        return await svc.chat(req, tenant)

    @router.get("/explain/{audit_id}", response_model=AnswerAuditView)
    async def explain(
        audit_id: str,
        tenant: TenantContext = Depends(get_tenant),
        svc=Depends(get_gateway_svc),
    ) -> AnswerAuditView:
        """回溯路由决策与工具链（工程师 UI 证据链）。"""
        view = await svc._audit_repo.find_by_id(audit_id)
        if view is None:
            raise HTTPException(status_code=404, detail="audit_id 不存在")
        return AnswerAuditView(**view)

    return router
