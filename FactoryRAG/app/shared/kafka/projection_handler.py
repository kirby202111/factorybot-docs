"""事件 -> 投影 handler 协议（ISP）。

A 的图投影 / B 的重索引都实现它。各路线只保留自己的 ``handlers/``。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.shared.kafka.domain_event import DomainEvent


@runtime_checkable
class ProjectionHandler(Protocol):
    """事件 -> 投影 handler 协议（接口隔离）。

    每个 handler 只处理自己限界上下文的事件（ISP）。
    ``handle`` 接收一个事务句柄（``tx``）：A 是 Neo4j 事务，B 是 MySQL 会话。
    """

    event_type: str
    bounded_context: str

    async def handle(self, event: DomainEvent, tx: Any) -> None:
        """把事件投影到存储（图/索引）。tx 由 ConsumerGroup 注入。"""
        ...
