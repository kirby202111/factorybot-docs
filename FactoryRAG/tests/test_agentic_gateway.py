"""E ``GatewayService.chat`` 端到端集成测试（补 P0 #5 缺口）。

真实组件最大化：真实 ``IntentRouter`` / ``RouteGraphBuilder``（真 langgraph ``CompiledStateGraph``）/
``ToolExecutor`` / ``SubAgentDelegator`` / ``L1|L2DelegationClient``，仅伪造叶端基础设施
（A/B Port handler、L1/L2 httpx、Redis、MySQL session、obs）。

覆盖：
- 三意图分支正常路径：TRACE_FACT -> A 工具 / DOC_LOOKUP -> B 工具 / ROOT_CAUSE -> L1 委托
  （+ DRAFT_REQUEST -> L2 委托）
- L1 委托超时/失败 -> 转人工（``trace_repo.save_error``）
- 图执行异常 / 超时（``_run_graph`` 两条 except 分支）-> 转人工
- 缓存命中短路（不分类、不跑图、不审计、不回写缓存）
- 权限不足 / 工具执行失败 -> 转人工
- UNKNOWN 兜底 -> 转人工
- ``IntentRouter`` 规则优先 + LLM 兜底（成功 / 失败 -> UNKNOWN）

与 ``test_route_graph.py`` 互补：彼处仅覆盖 langgraph 不可用时的 ``_FallbackGraph`` 降级。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.routes.agentic.application.gateway_service import GatewayService
from app.routes.agentic.application.intent_router import IntentRouter
from app.routes.agentic.domain.answer import AgentAnswer, ChatRequest
from app.routes.agentic.domain.intent import IntentCategory
from app.routes.agentic.domain.tool import ToolRegistry
from app.routes.agentic.infrastructure.acl.l1_delegation import L1DelegationClient
from app.routes.agentic.infrastructure.acl.l2_delegation import L2DelegationClient
from app.routes.agentic.infrastructure.ai.delegator import SubAgentDelegator
from app.routes.agentic.infrastructure.ai.route_graph_builder import RouteGraphBuilder
from app.routes.agentic.infrastructure.ai.tool_executor import ToolExecutor
from app.routes.document.domain.answer import ChunkHit
from app.routes.traceability.domain.subgraph import TraceNode, TraceSubgraph
from app.shared.tenant.context import TenantContext


# ──────────────────────────────────────────────────────────────────
# 伪造件
# ──────────────────────────────────────────────────────────────────
class _LLMResult:
    """桩 LLM 应答（``.content`` 契约）。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeCache:
    """内存查询缓存（满足 ``QueryCache.get/set`` 契约，可控命中）。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], AgentAnswer] = {}
        self.get_calls = 0
        self.set_calls = 0

    @staticmethod
    def _key(request: ChatRequest, tenant: TenantContext) -> tuple[str, str]:
        return (request.question, tenant.tenant_id)

    async def get(self, request: ChatRequest, tenant: TenantContext) -> AgentAnswer | None:
        self.get_calls += 1
        return self.store.get(self._key(request, tenant))

    async def set(self, request: ChatRequest, tenant: TenantContext, answer: AgentAnswer) -> None:
        self.set_calls += 1
        self.store[self._key(request, tenant)] = answer

    def prime(self, request: ChatRequest, tenant: TenantContext, answer: AgentAnswer) -> None:
        """预置缓存命中。"""
        self.store[self._key(request, tenant)] = answer


class _RaisingGraph:
    """``ainvoke`` 抛异常的伪图（测 ``_run_graph`` 两条 except 分支）。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        raise self._exc


class _RaisingGraphBuilder:
    """``build`` 返回抛异常图的伪 builder（绕过真实 langgraph，直击异常分支）。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def build(self, intent: IntentCategory, tenant: TenantContext) -> _RaisingGraph:
        return _RaisingGraph(self._exc)


def _ok_http(json_body: dict) -> MagicMock:
    """``raise_for_status`` 通过 + ``.json()`` 返回 ``json_body`` 的伪 httpx。"""
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_body)
    http = MagicMock()
    http.post = AsyncMock(return_value=resp)
    return http


def _raising_http(exc: BaseException) -> MagicMock:
    """``.post`` 抛 ``exc`` 的伪 httpx（模拟 L1/L2 超时/失败）。"""
    http = MagicMock()
    http.post = AsyncMock(side_effect=exc)
    return http


def _mock_trace_repo() -> MagicMock:
    """伪 ``RouteTraceRepo``（断言 save_ok/save_error/save_denied 调用）。"""
    repo = MagicMock()
    repo.save_ok = AsyncMock()
    repo.save_error = AsyncMock()
    repo.save_denied = AsyncMock()
    return repo


def _subgraph() -> TraceSubgraph:
    seed = TraceNode(
        label="CheckpointRecord",
        bounded_context="在制品执行",
        node_id="CheckpointRecord:CP-1",
        props={"seed_kind": "WipUnit", "seed_value": "SN-001"},
    )
    return TraceSubgraph(seed=seed, as_of=datetime(2026, 7, 15, tzinfo=timezone.utc))


def _chunk_hits() -> list[ChunkHit]:
    return [
        ChunkHit(
            chunk_id="chk-1",
            doc_id="SOP-014",
            version_id="v3",
            text="回流焊参数",
            locator={"page": 1},
            section_type="procedure",
        ),
        ChunkHit(
            chunk_id="chk-2",
            doc_id="SOP-014",
            version_id="v3",
            text="锡膏管控",
            locator={"page": 2},
            section_type="procedure",
        ),
    ]


def _build_gateway(
    *,
    trace_rag_port: Any | None = None,
    doc_rag_port: Any | None = None,
    l1_http: MagicMock | None = None,
    l2_http: MagicMock | None = None,
    cache: Any | None = None,
    audit_repo: Any | None = None,
    route_trace_repo: MagicMock | None = None,
    llm: Any | None = None,
    graph_builder: Any | None = None,
    obs: Any | None = None,
) -> GatewayService:
    """真实 E 组件 + 伪造叶端基础设施装配 ``GatewayService``。"""
    obs = obs or MagicMock()
    trace_repo = route_trace_repo or _mock_trace_repo()

    registry = ToolRegistry()
    registry.build_default(
        trace_rag_port=trace_rag_port or AsyncMock(),
        doc_rag_port=doc_rag_port or AsyncMock(),
    )

    l1_client = L1DelegationClient(http=l1_http or _ok_http({"summary": "l1"}), timeout=60.0)
    l2_client = L2DelegationClient(http=l2_http or _ok_http({"summary": "l2"}), timeout=30.0)

    tool_executor = ToolExecutor(registry=registry, trace_repo=trace_repo, obs=obs)
    delegator = SubAgentDelegator(l1_client=l1_client, l2_client=l2_client, trace_repo=trace_repo, obs=obs)
    gb = graph_builder or RouteGraphBuilder(tool_executor=tool_executor, delegator=delegator)

    gateway = GatewayService(
        graph_builder=gb,
        intent_router=IntentRouter(llm=llm or MagicMock()),
        cache=cache or _FakeCache(),
        audit_repo=audit_repo or AsyncMock(),
        obs=obs,
    )
    gateway.tool_registry = registry  # 供 ReadOnlyToolGate 启动期扫描
    return gateway


@pytest.fixture
def rag_tenant() -> TenantContext:
    """带 trace:read/doc:read 作用域的租户（A/B 工具放行）。"""
    return TenantContext(tenant_id="t-rag", tenant_scopes=["trace:read", "doc:read"])


# ──────────────────────────────────────────────────────────────────
# 三意图分支正常路径
# ──────────────────────────────────────────────────────────────────
async def test_chat_trace_fact_routes_to_a_tool(rag_tenant: TenantContext):
    """TRACE_FACT：规则命中 -> 真 langgraph -> A 工具 -> 子图物化为 trace_subgraph 来源。"""
    trace_rag = MagicMock()
    trace_rag.expand = AsyncMock(return_value=_subgraph())
    cache = _FakeCache()
    audit = AsyncMock()
    trace_repo = _mock_trace_repo()
    gw = _build_gateway(
        trace_rag_port=trace_rag, cache=cache, audit_repo=audit, route_trace_repo=trace_repo
    )

    answer = await gw.chat(ChatRequest(question="SN-001 追溯"), rag_tenant)

    assert answer.intent == "TRACE_FACT"
    assert answer.route_taken == "A"
    assert answer.tool_chain == ["query_traceability_graph"]
    assert answer.confidence == 0.75
    assert answer.needs_human_review is False
    assert len(answer.sources) == 1
    assert answer.sources[0].source_type == "trace_subgraph"
    assert answer.sources[0].route == "A"
    assert answer.sources[0].ref.startswith("WipUnit:SN-001@")
    assert "tool_result" in answer.detail  # _serialize 物化子图

    # A 工具经 InProcess Port 调用，seed_kind/seed_value 原语透传（决策 #4）
    trace_rag.expand.assert_awaited_once()
    expand_args = trace_rag.expand.await_args.args
    assert expand_args[0] == "WipUnit"        # seed_kind
    assert expand_args[1] == "SN-001 追溯"     # seed_value = 问题文本
    assert expand_args[2] is rag_tenant       # 租户透传

    # trace 全链路（决策 #1）：save_ok 带 traceparent + tenant_id
    trace_repo.save_ok.assert_awaited_once()
    ok_kwargs = trace_repo.save_ok.await_args.kwargs
    assert ok_kwargs["traceparent"].startswith("00-")
    assert ok_kwargs["tenant_id"] == "t-rag"
    # 证据链：route_trace.audit_id 必须非空且 == answer_audit.audit_id
    # （AgentState 漏声明 audit_id 时此处为 ''，/agent/explain/{audit_id} 取不到 trace）
    assert ok_kwargs["audit_id"]
    assert ok_kwargs["audit_id"] == audit.record.await_args.kwargs["audit_id"]

    # 审计 + 缓存回写
    audit.record.assert_awaited_once()
    assert audit.record.await_args.kwargs["audit_id"]
    assert cache.get_calls == 1
    assert cache.set_calls == 1


async def test_chat_doc_lookup_routes_to_b_tool(rag_tenant: TenantContext):
    """DOC_LOOKUP：规则命中 -> B 工具 -> list[ChunkHit] 物化为 sop_doc 来源。"""
    doc_rag = MagicMock()
    doc_rag.search = AsyncMock(return_value=_chunk_hits())
    gw = _build_gateway(doc_rag_port=doc_rag)

    answer = await gw.chat(ChatRequest(question="怎么修 SOP 回流焊"), rag_tenant)

    assert answer.intent == "DOC_LOOKUP"
    assert answer.route_taken == "B"
    assert answer.tool_chain == ["search_docs"]
    assert answer.confidence == 0.75
    assert [s.source_type for s in answer.sources] == ["sop_doc", "sop_doc"]
    assert [s.ref for s in answer.sources] == ["chk-1", "chk-2"]
    assert all(s.route == "B" for s in answer.sources)
    doc_rag.search.assert_awaited_once()


async def test_chat_root_cause_delegates_l1(rag_tenant: TenantContext):
    """ROOT_CAUSE：规则命中 -> 委托 L1（真 L1DelegationClient + 伪 httpx），透传 traceparent。"""
    l1_http = _ok_http({"summary": "根因：锡膏批次异常", "hypotheses": []})
    audit = AsyncMock()
    trace_repo = _mock_trace_repo()
    gw = _build_gateway(l1_http=l1_http, audit_repo=audit, route_trace_repo=trace_repo)

    answer = await gw.chat(ChatRequest(question="根因分析 锡桥不良"), rag_tenant)

    assert answer.intent == "ROOT_CAUSE"
    assert answer.route_taken == "L1"
    assert answer.tool_chain == ["L1:diagnose"]
    assert answer.needs_human_review is False

    l1_http.post.assert_awaited_once()
    post_args = l1_http.post.await_args
    assert post_args.args[0] == "/agent/diagnose"
    headers = post_args.kwargs["headers"]
    assert headers["traceparent"].startswith("00-")          # 决策 #1 traceparent 透传
    assert headers["X-Tenant-Id"] == "t-rag"                 # 租户 header 透传
    assert post_args.kwargs["timeout"] == 60.0

    # 委托路径同样写 route_trace 且 audit_id 挂得上（Delegator 读 state.audit_id）
    trace_repo.save_ok.assert_awaited_once()
    assert trace_repo.save_ok.await_args.kwargs["audit_id"] == audit.record.await_args.kwargs["audit_id"]


async def test_chat_draft_request_delegates_l2(rag_tenant: TenantContext):
    """DRAFT_REQUEST：规则命中 -> 委托 L2（``POST /agent/draft``）。"""
    l2_http = _ok_http({"draft": "rework-001"})
    gw = _build_gateway(l2_http=l2_http)

    answer = await gw.chat(ChatRequest(question="草拟返工单"), rag_tenant)

    assert answer.intent == "DRAFT_REQUEST"
    assert answer.route_taken == "L2"
    assert answer.tool_chain == ["L2:draft"]
    l2_http.post.assert_awaited_once()
    assert l2_http.post.await_args.args[0] == "/agent/draft"


# ──────────────────────────────────────────────────────────────────
# 委托超时 / 图异常 -> 转人工
# ──────────────────────────────────────────────────────────────────
async def test_chat_l1_delegation_timeout_falls_to_human(rag_tenant: TenantContext):
    """L1 委托超时：httpx.TimeoutException 经 L1 client 透传 -> Delegator 兜底转人工。"""
    l1_http = _raising_http(httpx.TimeoutException("upstream timeout"))
    trace_repo = _mock_trace_repo()
    gw = _build_gateway(l1_http=l1_http, route_trace_repo=trace_repo)

    answer = await gw.chat(ChatRequest(question="根因分析 锡桥不良"), rag_tenant)

    assert answer.tool_chain == ["delegation:failed"]  # 所尝试的委托记在 tool_chain
    assert answer.route_taken == "HUMAN"               # 节点内失败统一转人工
    assert answer.confidence == 0.0
    assert answer.needs_human_review is True
    # 失败原因保留（Delegator 写入的 state["answer"]），不再退化为通用文案
    assert "子代理委托超时/失败" in answer.summary
    assert "upstream timeout" in answer.summary
    assert answer.detail["reason"] == answer.summary
    trace_repo.save_error.assert_awaited_once()
    assert trace_repo.save_error.await_args.kwargs["tenant_id"] == "t-rag"


async def test_chat_graph_exception_falls_to_human(rag_tenant: TenantContext):
    """图执行抛通用异常 -> ``_run_graph`` except 兜底 -> 转人工。"""
    gw = _build_gateway(graph_builder=_RaisingGraphBuilder(RuntimeError("graph boom")))

    answer = await gw.chat(ChatRequest(question="SN-001 追溯"), rag_tenant)

    assert answer.route_taken == "HUMAN"
    assert answer.needs_human_review is True
    assert answer.confidence == 0.0
    assert answer.tool_chain == []
    assert "graph boom" in answer.summary


async def test_chat_graph_timeout_falls_to_human(rag_tenant: TenantContext):
    """图执行抛 ``asyncio.TimeoutError`` -> ``_run_graph`` except 分支 -> 转人工（路由图超时）。"""
    gw = _build_gateway(graph_builder=_RaisingGraphBuilder(asyncio.TimeoutError()))

    answer = await gw.chat(ChatRequest(question="SN-001 追溯"), rag_tenant)

    assert answer.route_taken == "HUMAN"
    assert answer.needs_human_review is True
    assert "路由图超时" in answer.summary


# ──────────────────────────────────────────────────────────────────
# 缓存命中短路
# ──────────────────────────────────────────────────────────────────
async def test_chat_cache_hit_short_circuits(rag_tenant: TenantContext):
    """缓存命中：直接返回，不分类意图、不跑图、不审计、不回写缓存。"""
    cache = _FakeCache()
    audit = AsyncMock()
    trace_rag = MagicMock()
    trace_rag.expand = AsyncMock()
    gw = _build_gateway(trace_rag_port=trace_rag, cache=cache, audit_repo=audit)

    cached = AgentAnswer(
        question="SN-001 追溯",
        intent="TRACE_FACT",
        route_taken="A",
        summary="cached answer",
        confidence=0.9,
        trace_id="t-cached",
    )
    cache.prime(ChatRequest(question="SN-001 追溯"), rag_tenant, cached)

    answer = await gw.chat(ChatRequest(question="SN-001 追溯"), rag_tenant)

    assert answer.summary == "cached answer"
    assert answer.trace_id == "t-cached"
    trace_rag.expand.assert_not_awaited()    # 图未跑
    audit.record.assert_not_awaited()        # 未审计
    assert cache.set_calls == 0              # 未回写


# ──────────────────────────────────────────────────────────────────
# 权限不足 / 工具失败 / UNKNOWN -> 转人工
# ──────────────────────────────────────────────────────────────────
async def test_chat_permission_denied_falls_to_human(tenant: TenantContext):
    """权限不足：conftest tenant 无 trace:read -> ToolExecutor save_denied -> 转人工。

    注：conftest ``tenant`` 作用域为 ``["workshop:PCBA", "line:SMT-1"]``，不含 ``trace:read``。
    """
    trace_rag = MagicMock()
    trace_rag.expand = AsyncMock()
    trace_repo = _mock_trace_repo()
    gw = _build_gateway(trace_rag_port=trace_rag, route_trace_repo=trace_repo)

    answer = await gw.chat(ChatRequest(question="SN-001 追溯"), tenant)

    assert answer.route_taken == "HUMAN"               # 权限不足统一转人工
    assert answer.needs_human_review is True
    assert answer.confidence == 0.0
    # 失败原因保留（ToolExecutor 写入的 state["answer"]）
    assert answer.summary == "权限不足，建议转人工。"
    assert answer.detail["reason"] == "权限不足，建议转人工。"
    trace_rag.expand.assert_not_awaited()    # 拒绝在 invoke 之前
    trace_repo.save_denied.assert_awaited_once()
    assert trace_repo.save_denied.await_args.args[0] == "query_traceability_graph"
    assert trace_repo.save_denied.await_args.kwargs["tenant_id"] == "t-tenant"


async def test_chat_tool_failure_falls_to_human(rag_tenant: TenantContext):
    """工具执行失败：A Port 抛异常 -> ToolExecutor save_error -> 转人工。"""
    trace_rag = MagicMock()
    trace_rag.expand = AsyncMock(side_effect=RuntimeError("neo4j down"))
    trace_repo = _mock_trace_repo()
    gw = _build_gateway(trace_rag_port=trace_rag, route_trace_repo=trace_repo)

    answer = await gw.chat(ChatRequest(question="SN-001 追溯"), rag_tenant)

    assert answer.route_taken == "HUMAN"               # 工具失败统一转人工
    assert answer.needs_human_review is True
    assert answer.confidence == 0.0
    assert answer.tool_chain == ["query_traceability_graph"]
    # 失败原因保留（ToolExecutor 写入的 state["answer"]）
    assert "执行失败" in answer.summary
    assert "neo4j down" in answer.summary
    assert answer.detail["reason"] == answer.summary
    trace_repo.save_error.assert_awaited_once()
    assert trace_repo.save_error.await_args.args[0] == "query_traceability_graph"


async def test_chat_unknown_intent_falls_to_human(rag_tenant: TenantContext):
    """UNKNOWN：无规则命中 + LLM 兜底失败 -> 仅 converge -> 转人工。"""
    llm = MagicMock()
    llm.achat = AsyncMock(side_effect=RuntimeError("llm down"))
    trace_rag = MagicMock()
    trace_rag.expand = AsyncMock()
    gw = _build_gateway(trace_rag_port=trace_rag, llm=llm)

    answer = await gw.chat(ChatRequest(question="今天天气怎么样"), rag_tenant)

    assert answer.intent == "UNKNOWN"
    assert answer.route_taken == "HUMAN"
    assert answer.needs_human_review is True
    # converge 兜底文案保留为 summary + reason（不再退化为通用"未能获取结果"）
    assert answer.summary == "未产生结果，建议转人工。"
    assert answer.detail["reason"] == "未产生结果，建议转人工。"
    trace_rag.expand.assert_not_awaited()    # UNKNOWN 不调工具/委托


# ──────────────────────────────────────────────────────────────────
# IntentRouter 规则优先 + LLM 兜底
# ──────────────────────────────────────────────────────────────────
async def test_intent_router_rule_skips_llm():
    """规则命中即返回，不调 LLM（避免每问都调 LLM）。"""
    llm = MagicMock()
    llm.achat = AsyncMock()
    router = IntentRouter(llm=llm)

    intent = await router.classify("这个 SN 追溯一下")

    assert intent == IntentCategory.TRACE_FACT
    llm.achat.assert_not_awaited()


async def test_intent_router_llm_fallback_classifies():
    """无规则命中 -> LLM 结构化输出兜底分类。"""
    llm = MagicMock()
    llm.achat = AsyncMock(return_value=_LLMResult('{"intent": "TRACE_FACT"}'))
    router = IntentRouter(llm=llm)

    intent = await router.classify("某个不含关键词的问题")

    assert intent == IntentCategory.TRACE_FACT
    llm.achat.assert_awaited_once()


async def test_intent_router_llm_failure_returns_unknown():
    """LLM 兜底异常（含畸形 JSON）-> UNKNOWN。"""
    llm = MagicMock()
    llm.achat = AsyncMock(return_value=_LLMResult("not json"))
    router = IntentRouter(llm=llm)

    intent = await router.classify("某个不含关键词的问题")

    assert intent == IntentCategory.UNKNOWN
