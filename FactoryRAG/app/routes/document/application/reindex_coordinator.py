"""B 重索引协调器。

决策 #3：工艺绑定型文档随 ``ProcessRouteActivated`` **联动 PUBLISHED**（直接置 PUBLISHED，
不设 SUBMITTED/PENDING_REBIND），责任归工艺 owner。老 chunk 不翻转，检索靠版本锚点隔离。
chunk 不可变使重索引幂等（chunk_id 不变，ChromaDB upsert 无副作用）。
"""
from __future__ import annotations

import logging
from typing import Any

from app.shared.kafka.domain_event import DomainEvent
from app.shared.obs.port import ObservabilityPort

logger = logging.getLogger(__name__)


class ReindexCoordinator:
    """事件 -> 重索引/状态翻转协调器。

    SRP：路由事件到 handler；幂等/位点由 shared ``ConsumerGroup`` 兜底。
    ``handlers`` 字典供 ``ReadOnlyIngestionGate`` 启动期扫描。
    """

    def __init__(
        self,
        *,
        doc_repo: Any,
        chunk_repo: Any,
        object_store: Any,
        parser: Any,
        chunk_selector: Any,
        embedder: Any,
        obs: ObservabilityPort,
    ) -> None:
        self._doc_repo = doc_repo
        self._chunk_repo = chunk_repo
        self._store = object_store
        self._parser = parser
        self._chunk_selector = chunk_selector
        self._embedder = embedder
        self._obs = obs
        self.handlers: dict[str, Any] = self._build_handlers()

    def _build_handlers(self) -> dict[str, Any]:
        from app.routes.document.infrastructure.handlers.process_route import (
            ProcessRouteActivatedHandler,
            ProcessRouteDeprecatedHandler,
        )
        from app.routes.document.infrastructure.handlers.reindex import (
            RagReindexRequestHandler,
        )
        from app.routes.document.infrastructure.handlers.quality import (
            QualityGateRuleActivatedHandler,
        )

        deps = dict(
            doc_repo=self._doc_repo,
            chunk_repo=self._chunk_repo,
            object_store=self._store,
            parser=self._parser,
            chunk_selector=self._chunk_selector,
            embedder=self._embedder,
            obs=self._obs,
        )
        handlers = [
            ProcessRouteActivatedHandler(**deps),
            ProcessRouteDeprecatedHandler(**deps),
            RagReindexRequestHandler(**deps),
            QualityGateRuleActivatedHandler(**deps),
        ]
        return {h.event_type: h for h in handlers}

    async def handle_event(self, event: DomainEvent, session: Any) -> None:
        """路由事件到 handler（由 ConsumerGroup 调用）。"""
        handler = self.handlers.get(event.event_type)
        if handler is None:
            logger.debug("B 无 handler 处理 %s，跳过", event.event_type)
            return
        await handler.handle(event, session)
