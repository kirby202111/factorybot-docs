"""B 强制版本红线（§1.2 红线 #1）。

工艺绑定型（PROCESS_BOUND）route_version 必填，入口校验拒绝缺失，**绝不退回"查最新 ACTIVE"**
（避开在制品不切换工艺语义陷阱）。设备绑定型（ASSET_BOUND）asset_id 必填。
通用知识型（GENERAL）不带版本，正常推进。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routes.document.application.retrieval_service import DocumentRetrievalService
from app.routes.document.domain.answer import DocQuery
from app.routes.document.domain.document import DocumentCategory
from app.shared.tenant.context import TenantContext


def _make_svc() -> DocumentRetrievalService:
    """retriever 返回空命中 -> _synthesize 早退不调 LLM，便于聚焦版本红线。"""
    retriever = AsyncMock()
    retriever.retrieve.return_value = []
    return DocumentRetrievalService(
        retriever=retriever,
        reranker=AsyncMock(),
        llm=AsyncMock(),
        redis=None,
        cache_ttl=300,
        obs=MagicMock(),  # record_retrieval 为同步调用
    )


async def test_process_bound_missing_route_version_raises(tenant: TenantContext):
    svc = _make_svc()
    req = DocQuery(question="q", doc_category=DocumentCategory.PROCESS_BOUND)
    with pytest.raises(ValueError, match="route_version"):
        await svc.query(req, tenant)


async def test_asset_bound_missing_asset_id_raises(tenant: TenantContext):
    svc = _make_svc()
    req = DocQuery(question="q", doc_category=DocumentCategory.ASSET_BOUND)
    with pytest.raises(ValueError, match="asset_id"):
        await svc.query(req, tenant)


async def test_process_bound_with_route_version_passes_enforcement(tenant: TenantContext):
    """带 route_version 通过版本红线；检索空命中 -> 低置信转人工。"""
    svc = _make_svc()
    req = DocQuery(
        question="q", doc_category=DocumentCategory.PROCESS_BOUND, route_version="v3"
    )
    answer = await svc.query(req, tenant)
    assert answer.needs_human_review is True
    assert answer.confidence == 0.0


async def test_general_does_not_require_route_version(tenant: TenantContext):
    """通用知识型不强制版本，正常推进到空命中转人工。"""
    svc = _make_svc()
    req = DocQuery(question="q", doc_category=DocumentCategory.GENERAL)
    answer = await svc.query(req, tenant)
    assert answer.needs_human_review is True
