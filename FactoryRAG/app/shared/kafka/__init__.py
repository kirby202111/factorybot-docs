"""shared/kafka -- 事件消费基类。

上移 A/B 两路线高度同构的 ``consumer_group``/``idempotency_repo``/``offset_repo`` 基类；
各路线只保留自己的 ``handlers/``（事件 -> 投影动作映射）。

口径见《rag-service-整体结构设计》§3.5、《技术选型和实现方案》§2.5。
"""
from app.shared.kafka.consumer_group import ConsumerGroup
from app.shared.kafka.domain_event import DomainEvent
from app.shared.kafka.idempotency_repo import IdempotencyRepo
from app.shared.kafka.offset_repo import OffsetRepo
from app.shared.kafka.projection_handler import ProjectionHandler

__all__ = ["DomainEvent", "ConsumerGroup", "IdempotencyRepo", "OffsetRepo", "ProjectionHandler"]
