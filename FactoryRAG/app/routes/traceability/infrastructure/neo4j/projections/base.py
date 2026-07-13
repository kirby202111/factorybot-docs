"""A 投影 handler 基类。

所有投影用 MERGE（幂等），禁 DELETE/REMOVE/覆盖性 SET（ReadOnlyProjectionGate 启动期扫描）。
``handle(event, tx)`` 的 ``tx`` 是 Neo4j driver（由 ConsumerGroup 的 tx_provider 注入），
handler 自行开 session。第二层幂等靠 MERGE by node_id。
"""
from __future__ import annotations

import logging
from typing import Any

from app.shared.kafka.domain_event import DomainEvent

logger = logging.getLogger(__name__)


class ProjectionHandlerBase:
    """A 投影 handler 公共基类。"""

    bounded_context: str = ""
    event_type: str = ""                # primary event type（Protocol 合规）
    event_types: list[str] = []         # 该 handler 处理的全部事件类型
    cypher_templates: list[str] = []    # 供 ReadOnlyProjectionGate 静态扫描

    def __init__(self, *, driver: Any, embedder: Any = None, trace_svc: Any = None) -> None:
        self._driver = driver
        self._embedder = embedder
        self._trace_svc = trace_svc

    async def handle(self, event: DomainEvent, tx: Any) -> None:
        """tx 是 Neo4j driver。子类实现具体投影。"""
        raise NotImplementedError

    async def _run(self, cypher: str, **params: Any) -> None:
        async with self._driver.session() as session:
            await session.run(cypher, **params)
