"""E ``_FallbackGraph`` 路径（langgraph 不可用时）按意图走 tool/delegate/converge。

RouteGraphBuilder.build 在 langgraph 不可用时降级为 _FallbackGraph 顺序执行器；
此处直接构造 _FallbackGraph 验证三意图分支：TRACE_FACT -> tool，ROOT_CAUSE -> delegate，
UNKNOWN -> 仅 converge。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from app.routes.agentic.domain.intent import IntentCategory
from app.routes.agentic.infrastructure.ai.route_graph_builder import (
    RouteGraphBuilder,
    _FallbackGraph,
)
from app.shared.tenant.context import TenantContext


def _builder(tool_executor, delegator) -> RouteGraphBuilder:
    return RouteGraphBuilder(tool_executor=tool_executor, delegator=delegator)


async def test_fallback_trace_fact_routes_to_tool(tenant: TenantContext):
    te = AsyncMock(
        return_value={"tool_result": "subgraph", "tool_chain": ["query_traceability_graph"]}
    )
    dele = AsyncMock()
    graph = _FallbackGraph(_builder(te, dele), IntentCategory.TRACE_FACT)

    state = {"question": "SN-001 追溯", "intent": IntentCategory.TRACE_FACT, "tenant": tenant}
    result = await graph.ainvoke(state, {})

    te.assert_awaited_once()
    dele.assert_not_awaited()
    assert result["tool_result"] == "subgraph"


async def test_fallback_root_cause_routes_to_delegate(tenant: TenantContext):
    te = AsyncMock()
    dele = AsyncMock(
        return_value={"tool_result": "l1view", "tool_chain": ["L1:diagnose"]}
    )
    graph = _FallbackGraph(_builder(te, dele), IntentCategory.ROOT_CAUSE)

    state = {"question": "根因", "intent": IntentCategory.ROOT_CAUSE, "tenant": tenant}
    result = await graph.ainvoke(state, {})

    dele.assert_awaited_once()
    te.assert_not_awaited()
    assert result["tool_result"] == "l1view"


async def test_fallback_unknown_routes_to_converge_only(tenant: TenantContext):
    te = AsyncMock()
    dele = AsyncMock()
    graph = _FallbackGraph(_builder(te, dele), IntentCategory.UNKNOWN)

    state = {"question": "无关问题", "intent": IntentCategory.UNKNOWN, "tenant": tenant}
    result = await graph.ainvoke(state, {})

    te.assert_not_awaited()
    dele.assert_not_awaited()
    # converge：无 answer 且无 tool_result -> 兜底转人工
    assert result["answer"] == "未产生结果，建议转人工。"
