"""B: ``POST /rag/docs/{query,search,ingest}``。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import (
    get_doc_ingestion_svc,
    get_doc_retrieval_svc,
    get_tenant,
)
from app.routes.document.domain.answer import (
    ChunkHit,
    DocAnswer,
    DocSearch,
    DocQuery,
    IngestCommand,
    IngestResponse,
)
from app.shared.tenant.context import TenantContext


def doc_router() -> APIRouter:
    router = APIRouter(prefix="/rag/docs", tags=["document-B"])

    @router.post("/query", response_model=DocAnswer)
    async def query(
        req: DocQuery,
        tenant: TenantContext = Depends(get_tenant),
        svc=Depends(get_doc_retrieval_svc),
    ) -> DocAnswer:
        """检索 + LLM 综合。工艺绑定型需 ROUTE 版本锚点（version+version_kind='route'，入口校验拒绝缺失）。"""
        return await svc.retrieve_and_synthesize(req, tenant)

    @router.post("/search", response_model=list[ChunkHit])
    async def search(
        req: DocSearch,
        tenant: TenantContext = Depends(get_tenant),
        svc=Depends(get_doc_retrieval_svc),
    ) -> list[ChunkHit]:
        """只检索 chunks，不综合（供 L1 Agent / UI 直接消费）。"""
        return await svc.search_chunks(req, tenant)

    @router.post("/ingest", response_model=IngestResponse)
    async def ingest(
        cmd: IngestCommand,
        tenant: TenantContext = Depends(get_tenant),
        svc=Depends(get_doc_ingestion_svc),
    ) -> IngestResponse:
        """文档摄入（管理接口）。chunk 不可变，state 固定 PUBLISHED。"""
        return await svc.ingest(cmd, tenant)

    return router
