"""路线 E Agentic RAG。

包结构投影到 rag-service 整体结构设计 §2/§11；``ai/route_graph_builder.py`` 保留在路线
infrastructure（不上移）。E 既是 rag-service 路线 E（收口 A/B），又是 agent-service L0。

调用方式（决策 #4）：A/B 经 ``shared/acl/`` Port（单服务内 InProcess Adapter，直调 application
service，不走本机 REST）；L1/L2 委托经 httpx REST（``POST /agent/diagnose`` 60s、
``POST /agent/draft`` 30s，透传 ``traceparent``，决策 #1）。
LangGraph ≥0.2（``Command(resume=…)`` 语义，与 agent-service 对齐）；``recursion_limit=6``。
``ReadOnlyToolGate``：ToolRegistry 拒绝注册 ``read_only=False`` 的工具。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.shared.web.container import Container


async def build_gateway_service(
    container: "Container",
    *,
    l1_http: Any,
    l2_http: Any,
) -> Any:
    """组合根入口：构造 E 的 GatewayService。

    ``l1_http``/``l2_http`` 由 Container 构造并管理生命周期（``dispose`` 关闭），
    本函数仅负责将其包装为 L1/L2 委托客户端并完成领域装配。
    """
    from app.routes.agentic.application.gateway_service import GatewayService
    from app.routes.agentic.application.intent_router import IntentRouter
    from app.routes.agentic.infrastructure.ai.delegator import SubAgentDelegator
    from app.routes.agentic.infrastructure.ai.route_graph_builder import RouteGraphBuilder
    from app.routes.agentic.infrastructure.ai.tool_executor import ToolExecutor
    from app.routes.agentic.infrastructure.acl.l1_delegation import L1DelegationClient
    from app.routes.agentic.infrastructure.acl.l2_delegation import L2DelegationClient
    from app.routes.agentic.infrastructure.persistence.audit_repo import (
        AnswerAuditRepo,
        RouteTraceRepo,
    )
    from app.routes.agentic.infrastructure.redis_.query_cache import QueryCache
    from app.routes.agentic.domain.tool import ToolRegistry

    settings = container.settings
    session_factory = await container.engines.mysql_session_factory()
    redis = await container.engines.redis()

    # ToolRegistry：注册 A/B 只读工具（InProcess Port 绑定）
    registry = ToolRegistry()
    registry.build_default(
        trace_rag_port=container.trace_rag,
        doc_rag_port=container.doc_rag,
    )
    registry.validate_on_startup()  # ReadOnlyToolGate：拒绝 read_only=False

    audit_repo = AnswerAuditRepo(session_factory=session_factory)
    route_trace_repo = RouteTraceRepo(session_factory=session_factory)

    l1_client = L1DelegationClient(http=l1_http, timeout=settings.agentic.l1_timeout)
    l2_client = L2DelegationClient(http=l2_http, timeout=settings.agentic.l2_timeout)

    tool_executor = ToolExecutor(
        registry=registry, trace_repo=route_trace_repo, obs=container.obs
    )
    delegator = SubAgentDelegator(
        l1_client=l1_client, l2_client=l2_client, trace_repo=route_trace_repo, obs=container.obs
    )
    graph_builder = RouteGraphBuilder(tool_executor=tool_executor, delegator=delegator)
    intent_router = IntentRouter(llm=container.llm)
    cache = QueryCache(redis=redis, ttl=settings.agentic.cache_ttl_seconds)

    gateway = GatewayService(
        graph_builder=graph_builder,
        intent_router=intent_router,
        cache=cache,
        audit_repo=audit_repo,
        obs=container.obs,
    )
    # 供 ReadOnlyToolGate 启动期扫描
    gateway.tool_registry = registry
    return gateway
