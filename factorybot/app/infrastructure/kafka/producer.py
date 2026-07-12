"""动作卡生产者：双通道之一（Kafka 持久兜底；WebSocket 实时见 ActionCardDispatcher）。"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.infrastructure.obs.logging import get_logger

_log = get_logger("kafka.producer")


class ActionCardProducer:
    """real 模式：aiokafka 发 topic=agent.action_cards。"""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def _ensure(self) -> None:
        if self._producer is None:
            from aiokafka import AIOKafkaProducer  # type: ignore
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap,
                value_serializer=lambda v: v.encode("utf-8"),
            )
            await self._producer.start()

    async def send(self, topic: str, key: str, value: str,
                   headers: Optional[dict] = None) -> None:
        await self._ensure()
        assert self._producer is not None
        await self._producer.send_and_wait(
            topic, value=value, key=key.encode("utf-8"),
            headers=[(k, str(v).encode("utf-8")) for k, v in (headers or {}).items()],
        )

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()


class MockActionCardProducer:
    """mock 模式：仅记录日志，不发真实 Kafka。动作卡仍可被 dispatcher 的 WebSocket 通道推送。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, topic: str, key: str, value: str,
                   headers: Optional[dict] = None) -> None:
        self.sent.append({"topic": topic, "key": key, "value": value, "headers": headers})
        _log.info("action_card.kafka.mock", topic=topic, key=key, card=value[:200])

    async def close(self) -> None:
        pass


_producer: Any = None


def get_action_card_producer() -> ActionCardProducer | MockActionCardProducer:
    global _producer
    if _producer is not None:
        return _producer
    from app.config import get_settings
    s = get_settings()
    if s.kafka_bootstrap_servers and not s.is_mock:
        _producer = ActionCardProducer(s.kafka_bootstrap_servers)
    else:
        _producer = MockActionCardProducer()
    return _producer
