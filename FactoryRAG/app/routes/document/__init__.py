"""路线 B 文档型 RAG。

包结构投影到 rag-service 整体结构设计 §2/§11；``llm_factory``/``embedding``/
``obs``/``config``/``kafka`` 基类见 shared/。PGVector -> ChromaDB 改造已完成
（chunk 不可变 + 强制带 route_version + MinIO 重建兜底）。

存储：ChromaDB（chunk 向量+metadata，chunk 不可变）+ MinIO（原始文件）+
MySQL（幂等/位点/审计）+ Redis（检索缓存）。
审核流：工艺绑定型文档随 ProcessRouteActivated **联动 PUBLISHED**（决策 #3），
去掉 SUBMITTED/PENDING_REBIND 中间态；通用知识型/设备绑定型仍走独立 DRAFT->PUBLISHED。
版本绑定：MVP 按 route_version，DocumentBinding 预留 rule_id+rule_version 双轨字段（决策 #2）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.shared.web.container import Container


async def build_document_services(container: "Container") -> tuple[Any, Any, Any]:
    """组合根入口：构造 B 的三个 application service。

    返回 ``(DocumentIngestionService, DocumentRetrievalService, ReindexCoordinator)``。
    lifespan 在存储就绪探测后调用，故 MySQL 会话工厂与 ChromaDB collection 已就绪。
    """
    from app.routes.document.application.chunking import ChunkStrategySelector
    from app.routes.document.application.ingestion_service import DocumentIngestionService
    from app.routes.document.application.reindex_coordinator import ReindexCoordinator
    from app.routes.document.application.retrieval_service import DocumentRetrievalService
    from app.routes.document.infrastructure.chromadb.chunk_repo import ChunkRepo
    from app.routes.document.infrastructure.chromadb.document_repo import DocumentRepo
    from app.routes.document.infrastructure.chromadb.retriever import VectorRetriever
    from app.routes.document.infrastructure.minio_.object_store import ObjectStore
    from app.routes.document.infrastructure.parser import DocumentParser

    settings = container.settings
    session_factory = await container.engines.mysql_session_factory()
    redis = await container.engines.redis()

    object_store = ObjectStore(
        endpoint=settings.minio.endpoint,
        access_key=settings.minio.access_key,
        secret_key=settings.minio.secret_key,
        bucket=settings.minio.bucket,
        secure=settings.minio.secure,
    )
    chunk_repo = ChunkRepo(collection=container.chroma_collection)
    document_repo = DocumentRepo(session_factory=session_factory)
    retriever = VectorRetriever(collection=container.chroma_collection, embedder=container.embedding)
    chunk_selector = ChunkStrategySelector()

    ingestion_svc = DocumentIngestionService(
        object_store=object_store,
        parser=DocumentParser(),
        chunk_selector=chunk_selector,
        embedder=container.embedding,
        doc_repo=document_repo,
        chunk_repo=chunk_repo,
        obs=container.obs,
    )
    retrieval_svc = DocumentRetrievalService(
        retriever=retriever,
        reranker=container.reranker,
        llm=container.llm,
        redis=redis,
        cache_ttl=settings.document.cache_ttl_seconds,
        obs=container.obs,
        top_k=settings.document.retrieval_top_k,
        top_n=settings.document.rerank_top_n,
    )
    reindex_coordinator = ReindexCoordinator(
        doc_repo=document_repo,
        chunk_repo=chunk_repo,
        object_store=object_store,
        parser=DocumentParser(),
        chunk_selector=chunk_selector,
        embedder=container.embedding,
        obs=container.obs,
    )

    # B Kafka 消费者（process.route.lifecycle / quality.gate.lifecycle / rag.reindex.*）
    _wire_document_consumer(
        container=container,
        coordinator=reindex_coordinator,
        session_factory=session_factory,
    )

    return ingestion_svc, retrieval_svc, reindex_coordinator


def _wire_document_consumer(
    *,
    container: "Container",
    coordinator: Any,
    session_factory: Any,
) -> None:
    """构造 B 的 ConsumerGroup 并注册到 container.consumers。"""
    from app.shared.kafka import ConsumerGroup, IdempotencyRepo, OffsetRepo

    group_id = f"{container.settings.kafka.consumer_group_prefix}-doc"
    idem_repo = IdempotencyRepo(consumer_group=group_id)
    offset_repo = OffsetRepo(consumer_group=group_id)

    def route_handler(event_type: str):
        return coordinator.handlers.get(event_type)

    async def tx_provider(session: Any) -> Any:
        # B：状态更新与幂等/位点同 MySQL 事务
        return session

    consumer = ConsumerGroup(
        topics=[
            "process.route.lifecycle",
            "quality.gate.lifecycle",
            "rag.reindex.request",
        ],
        group_id=group_id,
        bootstrap=container.settings.kafka.bootstrap,
        idem_repo=idem_repo,
        offset_repo=offset_repo,
        session_factory=session_factory,
        tx_provider=tx_provider,
        route_handler=route_handler,
    )
    container.consumers.append(consumer)
