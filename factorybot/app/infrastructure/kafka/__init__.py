"""Kafka：动作卡生产 + 领域事件订阅。

mock 模式：MockActionCardProducer（仅日志记录）+ 从 data/kafka fixtures 手动触发事件。
real 模式：aiokafka 生产/消费。
"""
from app.infrastructure.kafka.producer import (
    ActionCardProducer, MockActionCardProducer, get_action_card_producer,
)
from app.infrastructure.kafka.consumer import EventSubscriber, get_event_subscriber

__all__ = [
    "ActionCardProducer", "MockActionCardProducer", "get_action_card_producer",
    "EventSubscriber", "get_event_subscriber",
]
