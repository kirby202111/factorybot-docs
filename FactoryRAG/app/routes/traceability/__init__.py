"""路线 A 追溯型 RAG。

包结构投影到 rag-service 整体结构设计 §2/§11；neo4j/projections/rag/graph_index.py
保留在路线 infrastructure（不上移）。A 保留：neo4j/(driver/schema/retriever/projections)、
rag/graph_index.py。

存储：Neo4j（图主体 + DefectCatalog 原生向量索引 1024 维 cosine）+ MySQL（幂等/位点/审计）+
Redis（子图缓存）。
核心安全契约：图 ``SNAPSHOT_OF_ROUTE{route_version}`` 快照边物理锁定版本；工艺升版只追加
新 RouteVersion 节点 + 老节点置 DEPRECATED，历史快照边永不改。A 升版发 ``rag.reindex.request``
内部事件通知 B 重索引。
GraphProjector 订阅 MVP 4 上下文事件（mes.checkpoint.lifecycle/mes.testresult.structured/
mes.routing.progress/process.route.lifecycle/material.*/quality.*）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.shared.web.container import Container


async def build_trace_services(container: "Container") -> Any:
    """组合根入口：构造 A 的 TraceRetrievalService 并装配投影 consumer。"""
    from app.routes.traceability.application.seed_resolver import SeedResolver
    from app.routes.traceability.application.trace_retrieval_service import TraceRetrievalService
    from app.routes.traceability.infrastructure.neo4j.retriever import GraphRetriever
    from app.routes.traceability.infrastructure.neo4j.subgraph_repo import SubgraphRepo

    settings = container.settings
    driver = await container.engines.neo4j()
    redis = await container.engines.redis()
    session_factory = await container.engines.mysql_session_factory()

    retriever = GraphRetriever(driver=driver, embedder=container.embedding)
    seed_resolver = SeedResolver(llm=container.llm, embedder=container.embedding, driver=driver)
    subgraph_repo = SubgraphRepo(session_factory=session_factory)

    # DocRagPort：A 的 suggested_action 经它拉 B 的 SOP 片段（带 route_version_filter）。
    doc_rag = container.doc_rag

    trace_svc = TraceRetrievalService(
        retriever=retriever,
        seed_resolver=seed_resolver,
        llm=container.llm,
        subgraph_repo=subgraph_repo,
        redis=redis,
        cache_ttl=settings.traceability.subgraph_cache_ttl_seconds,
        doc_rag=doc_rag,
        obs=container.obs,
    )

    _wire_trace_consumer(container=container, session_factory=session_factory, trace_svc=trace_svc)
    return trace_svc


def _wire_trace_consumer(*, container: "Container", session_factory: Any, trace_svc: Any) -> None:
    """构造 A 的 GraphProjector ConsumerGroup（MVP 4 上下文）并注册到 container.consumers。"""
    from app.routes.traceability.infrastructure.neo4j.projections.registry import (
        build_projection_registry,
    )
    from app.shared.kafka import ConsumerGroup, IdempotencyRepo, OffsetRepo

    driver = container.engines._neo4j_driver  # lifespan 已初始化
    registry = build_projection_registry(driver=driver, embedder=container.embedding, trace_svc=trace_svc)
    group_id = f"{container.settings.kafka.consumer_group_prefix}-trace"
    idem_repo = IdempotencyRepo(consumer_group=group_id)
    offset_repo = OffsetRepo(consumer_group=group_id)

    def route_handler(event_type: str):
        return registry.handler_for(event_type)

    async def tx_provider(session: Any) -> Any:
        # A：图投影用 Neo4j session（第二层幂等靠 MERGE）；幂等/位点在 MySQL session
        from neo4j import AsyncGraphDatabase  # type: ignore  # noqa: F401

        return driver  # GraphProjector 内部自行开 session

    consumer = ConsumerGroup(
        topics=registry.topics,
        group_id=group_id,
        bootstrap=container.settings.kafka.bootstrap,
        idem_repo=idem_repo,
        offset_repo=offset_repo,
        session_factory=session_factory,
        tx_provider=tx_provider,
        route_handler=route_handler,
    )
    container.consumers.append(consumer)
    # 供 ReadOnlyProjectionGate / RawDataTopicGate 启动期扫描
    container.trace_projection_registry = registry
