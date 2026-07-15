"""DI 容器：组合根，注册 LLM/Embedding/各 Port 的 Adapter 绑定。

单服务内全部为 InProcess Adapter；拆服务时仅改容器绑定，业务代码零改动（§8.1）。
本模块是**唯一**允许 import 路线 application 的地方（组合根），路线间互调仍走 Port。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.shared.acl import (
    InProcessDocRagAdapter,
    InProcessTraceRagAdapter,
    MesClients,
)
from app.shared.acl.ports import DocRagPort, TraceRagPort
from app.shared.ai import llm_factory
from app.shared.config.rag_settings import RagSettings
from app.shared.embedding import build_embedding, build_reranker
from app.shared.obs import Observability
from app.shared.persistence.db import DbEngines
from app.shared.tenant.propagation import TenantPropagator

logger = logging.getLogger(__name__)


class Container:
    """DI 容器。

    组合根职责：构造 shared 设施 + 按路线开关懒装配路线 service + 绑定 Port->Adapter。
    """

    def __init__(self, settings: RagSettings) -> None:
        self.settings = settings
        self.obs = Observability(service_name=settings.otel.service_name)
        self.engines = DbEngines(settings)
        self.llm = llm_factory(settings.llm, self.obs)
        self.embedding = build_embedding(settings.embedding)
        self.reranker = build_reranker(settings.embedding)
        self.tenant_propagator = TenantPropagator()

        self._http = httpx.AsyncClient(timeout=30.0)
        self.mes_clients = MesClients(
            http=self._http,
            mes_base_url=settings.mes.base_url,  # MES 只读 REST 网关
            tenant_propagator=self.tenant_propagator,
        )

        # 路线 service 懒装配（按开关）
        self._trace_svc: Any = None
        self._doc_ingestion_svc: Any = None
        self._doc_retrieval_svc: Any = None
        self._reindex_coordinator: Any = None
        self._gateway_svc: Any = None
        # E 委托 L1/L2 的 httpx 客户端（_wire_agentic 构造，dispose 关闭）
        self._l1_http: httpx.AsyncClient | None = None
        self._l2_http: httpx.AsyncClient | None = None

        # Port -> Adapter 绑定（拆服务时换 Http Adapter）
        self.trace_rag: TraceRagPort | None = None
        self.doc_rag: DocRagPort | None = None

        # consumer 列表（lifespan 启停）
        self.consumers: list[Any] = []
        # ChromaDB collection（B 路线 schema 初始化后注入）
        self.chroma_collection: Any = None
        # A 图投影注册表（wire_traceability 后注入，供 RawDataTopicGate 运行期复用）
        self.trace_projection_registry: Any = None
        # 启动断言 gate 收集（lifespan 执行）
        self._wired = False

    # ── 路线装配（lifespan 在就绪探测后调用）──
    async def wire_routes(self) -> None:
        if self._wired:
            return
        if self.settings.document.enabled:
            await self._wire_document()
        if self.settings.traceability.enabled:
            await self._wire_traceability()
        if self.settings.agentic.enabled:
            await self._wire_agentic()
        self._wired = True

    async def _wire_document(self) -> None:
        from app.routes.document import build_document_services

        (
            self._doc_ingestion_svc,
            self._doc_retrieval_svc,
            self._reindex_coordinator,
        ) = await build_document_services(self)
        self.doc_rag = InProcessDocRagAdapter(self._doc_retrieval_svc)
        logger.info("路线 B（document）已装配，DocRagPort -> InProcessDocRagAdapter")

    async def _wire_traceability(self) -> None:
        from app.routes.traceability import build_trace_services

        self._trace_svc = await build_trace_services(self)
        self.trace_rag = InProcessTraceRagAdapter(self._trace_svc)
        logger.info("路线 A（traceability）已装配，TraceRagPort -> InProcessTraceRagAdapter")

    async def _wire_agentic(self) -> None:
        from app.routes.agentic import build_gateway_service

        agentic = self.settings.agentic
        self._l1_http = httpx.AsyncClient(base_url=agentic.l1_base_url, timeout=agentic.l1_timeout)
        self._l2_http = httpx.AsyncClient(base_url=agentic.l2_base_url, timeout=agentic.l2_timeout)
        self._gateway_svc = await build_gateway_service(
            self, l1_http=self._l1_http, l2_http=self._l2_http
        )
        logger.info("路线 E（agentic）已装配")

    # ── consumer 启停 ──
    async def start_consumers(self) -> None:
        for consumer in self.consumers:
            await consumer.start()

    async def stop_consumers(self) -> None:
        for consumer in self.consumers:
            await consumer.stop()

    # ── 启动断言 gate 收集 ──
    def collect_pre_wiring_gates(self) -> list[tuple[str, Any, Any]]:
        """预装配断言（lifespan 第 1 步）：扫描静态数据，不依赖路线实例。

        - ``ReadOnlyAclGate``：扫描 MesClients（container init 即就绪）；
        - ``ReadOnlyProjectionGate``（A）：扫描投影 handler 类的 Cypher 模板（静态）；
        - ``RawDataTopicGate``（A）：扫描声明主题列表（静态，无 dc.*）。
        """
        from app.shared.acl import ReadOnlyAclGate

        gates: list[tuple[str, Any, Any]] = [
            ("ReadOnlyAclGate", ReadOnlyAclGate(), self.mes_clients.all_clients())
        ]
        if self.settings.traceability.enabled:
            from app.routes.traceability.domain.projection import (
                RawDataTopicGate,
                ReadOnlyProjectionGate,
            )
            from app.routes.traceability.infrastructure.neo4j.projections.registry import (
                TRACE_TOPICS,
                get_projection_handler_classes,
            )

            gates.append(("ReadOnlyProjectionGate", ReadOnlyProjectionGate(), get_projection_handler_classes()))
            gates.append(("RawDataTopicGate", RawDataTopicGate(), [TRACE_TOPICS]))
        return gates

    def collect_post_wiring_gates(self) -> list[tuple[str, Any, Any]]:
        """装配后断言（lifespan wire_routes 之后）：扫描路线实例。"""
        gates: list[tuple[str, Any, Any]] = []
        if self.settings.document.enabled and self._reindex_coordinator is not None:
            from app.routes.document.domain.projection import ReadOnlyIngestionGate

            gates.append(("ReadOnlyIngestionGate", ReadOnlyIngestionGate(), self._reindex_coordinator))
        if self.settings.agentic.enabled and self._gateway_svc is not None:
            from app.routes.agentic.domain.tool import ReadOnlyToolGate

            gates.append(("ReadOnlyToolGate", ReadOnlyToolGate(), self._gateway_svc))
        return gates

    async def dispose(self) -> None:
        await self.engines.dispose()
        await self._http.aclose()
        # E 委托 L1/L2 客户端（仅 agentic 启用时构造）
        for client in (self._l1_http, self._l2_http):
            if client is not None:
                await client.aclose()
        await self.embedding.close()
        await self.reranker.close()
