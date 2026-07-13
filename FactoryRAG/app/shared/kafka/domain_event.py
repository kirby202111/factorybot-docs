"""领域事件 envelope 公共定义。

对齐《消息处理实现说明》§4.3：``event_id``/``event_type``/``event_version``/
``occurred_at``/``source_service``/``trace_id``/``partition_key``。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DomainEvent(BaseModel):
    """领域事件 envelope。rag-service 只读消费。"""

    event_id: str = Field(description="事件唯一 ID，幂等键")
    event_type: str = Field(description="事件类型，如 ProcessRouteActivated")
    event_version: int = Field(default=1, description="事件 schema 版本")
    occurred_at: datetime = Field(description="事件发生时间")
    source_service: str = Field(default="", description="来源服务，如 mes-wip")
    trace_id: str = Field(default="", description="W3C trace_id，串联 MES/agent-service")
    partition_key: str = Field(default="", description="Kafka 分区键")
    payload: dict[str, Any] = Field(default_factory=dict, description="事件负载")
    metadata: dict[str, Any] = Field(default_factory=dict, description="租户等元数据")

    @property
    def tenant_scope(self) -> str | None:
        """从 envelope metadata 还原租户上下文（§3.8 传递协议）。"""
        return self.metadata.get("tenant_scope")

    @property
    def tenant_id(self) -> str | None:
        return self.metadata.get("tenant_id")

    @property
    def consumer_key(self) -> str:
        """幂等键：event_id + consumer_group（在 repo 层拼 consumer_group）。"""
        return self.event_id
