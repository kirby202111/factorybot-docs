"""InProcessTraceRagAdapter / InProcessDocRagAdapter 直调 svc（mock svc）。

验证 Port 改收原语后：调用方（A/E）只传原语，Adapter 内部构造各路线 DTO 并直调
application service。枚举值字符串（"WipUnit" / "PROCESS_BOUND" / "SOP"）在 Adapter
内还原为路线枚举。版本锚点以 version/version_kind/version_ref_id 原语传递。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.routes.document.domain.answer import DocSearch
from app.routes.document.domain.document import DocType, DocumentCategory
from app.routes.traceability.domain.seed import ExpandRequest, SeedKind, TraceQuery
from app.shared.acl.adapters import InProcessDocRagAdapter, InProcessTraceRagAdapter
from app.shared.tenant.context import TenantContext


# ── TraceRagPort InProcess ──


async def test_trace_adapter_expand_builds_expand_request(tenant: TenantContext):
    svc = AsyncMock()
    svc.expand_subgraph.return_value = "subgraph"
    adapter = InProcessTraceRagAdapter(svc)
    as_of = datetime(2026, 7, 13, tzinfo=timezone.utc)

    result = await adapter.expand(
        "WipUnit", "SN-001", tenant, as_of=as_of, version="v3", version_kind="route"
    )

    assert result == "subgraph"
    svc.expand_subgraph.assert_awaited_once()
    req, passed_tenant = svc.expand_subgraph.call_args.args
    assert isinstance(req, ExpandRequest)
    assert req.kind == SeedKind.WIP_UNIT          # "WipUnit" -> SeedKind.WIP_UNIT
    assert req.value == "SN-001"
    assert req.version == "v3"
    assert req.version_kind == "route"
    assert req.as_of == as_of
    assert passed_tenant is tenant


async def test_trace_adapter_query_builds_trace_query_with_seed(tenant: TenantContext):
    svc = AsyncMock()
    svc.retrieve_and_synthesize.return_value = "answer"
    adapter = InProcessTraceRagAdapter(svc)

    await adapter.query(
        "问题", tenant, seed_kind="WipUnit", seed_value="SN-001", version="v3", version_kind="route"
    )

    req, passed_tenant = svc.retrieve_and_synthesize.call_args.args
    assert isinstance(req, TraceQuery)
    assert req.question == "问题"
    assert req.seed is not None and req.seed.kind == SeedKind.WIP_UNIT
    assert req.seed.value == "SN-001"
    assert req.version == "v3"
    assert passed_tenant is tenant


async def test_trace_adapter_query_without_seed(tenant: TenantContext):
    svc = AsyncMock()
    svc.retrieve_and_synthesize.return_value = "answer"
    adapter = InProcessTraceRagAdapter(svc)

    await adapter.query("问题", tenant)

    req, _ = svc.retrieve_and_synthesize.call_args.args
    assert isinstance(req, TraceQuery)
    assert req.seed is None                       # 未传 seed -> None（交由 SeedResolver）


# ── DocRagPort InProcess ──


async def test_doc_adapter_search_builds_doc_search(tenant: TenantContext):
    svc = AsyncMock()
    svc.search_chunks.return_value = []
    adapter = InProcessDocRagAdapter(svc)

    await adapter.search(
        "SOP 查询", tenant, version="v3", version_kind="route", doc_types=["SOP", "MANUAL"]
    )

    req, passed_tenant = svc.search_chunks.call_args.args
    assert isinstance(req, DocSearch)
    assert req.question == "SOP 查询"             # 原语 query -> DocSearch.question
    assert req.version == "v3"
    assert req.version_kind == "route"
    assert req.doc_types == [DocType.SOP, DocType.MANUAL]
    assert passed_tenant is tenant


async def test_doc_adapter_query_builds_doc_query_process_bound(tenant: TenantContext):
    svc = AsyncMock()
    svc.retrieve_and_synthesize.return_value = "answer"
    adapter = InProcessDocRagAdapter(svc)

    await adapter.query(
        "处置 SOP", tenant, version="v3", version_kind="route", doc_category="PROCESS_BOUND"
    )

    req, _ = svc.retrieve_and_synthesize.call_args.args
    assert req.question == "处置 SOP"
    assert req.doc_category == DocumentCategory.PROCESS_BOUND
    assert req.version == "v3"
    assert req.version_kind == "route"


async def test_doc_adapter_query_defaults_to_general(tenant: TenantContext):
    svc = AsyncMock()
    svc.retrieve_and_synthesize.return_value = "answer"
    adapter = InProcessDocRagAdapter(svc)

    await adapter.query("通用问题", tenant)

    req, _ = svc.retrieve_and_synthesize.call_args.args
    assert req.doc_category == DocumentCategory.GENERAL   # 缺省归 GENERAL


async def test_doc_adapter_normalizes_empty_version_to_none(tenant: TenantContext):
    """空串 version 归一化为 None（避免 "" 被当版本过滤）。"""
    svc = AsyncMock()
    svc.search_chunks.return_value = []
    adapter = InProcessDocRagAdapter(svc)

    await adapter.search("q", tenant, version="")

    req, _ = svc.search_chunks.call_args.args
    assert req.version is None
