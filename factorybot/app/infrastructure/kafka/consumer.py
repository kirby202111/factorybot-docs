"""领域事件订阅：主动触发 L2 SOP 草拟 / L3 故障复产 / L1 主动诊断。

mock 模式：从 data/kafka/*.json 加载事件，手动 dispatch（无真实 Kafka 消费循环）。
real 模式：aiokafka 消费 process.route.lifecycle / eam.asset.availability / mes.defect-rate-spike。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.infrastructure.mock.fixture_loader import get_fixtures
from app.infrastructure.obs.logging import get_logger

_log = get_logger("kafka.consumer")


class EventSubscriber:
    """real 模式：aiokafka 消费者。"""

    def __init__(self, bootstrap_servers: str, group_id: str) -> None:
        self._bootstrap = bootstrap_servers
        self._group_id = group_id
        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}

    def on(self, event_type: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._handlers[event_type] = handler

    async def start(self) -> None:  # pragma: no cover (real 模式才跑)
        from aiokafka import AIOKafkaConsumer  # type: ignore
        consumer = AIOKafkaConsumer(
            "process.route.lifecycle", "eam.asset.availability", "mes.defect-rate-spike",
            bootstrap_servers=self._bootstrap, group_id=self._group_id,
        )
        await consumer.start()
        try:
            async for msg in consumer:
                evt = msg.value
                h = self._handlers.get(evt.get("event_type"))
                if h:
                    await h(evt)
        finally:
            await consumer.stop()

    async def dispatch_fixture(self, fixture_rel: str) -> Any:
        """mock 用：加载 data/kafka/<rel>.json 事件并派发给对应 handler。"""
        evt = get_fixtures().raw(fixture_rel)
        h = self._handlers.get(evt.get("event_type"))
        if h:
            _log.info("event.dispatch", event_type=evt.get("event_type"))
            return await h(evt)
        _log.warning("event.no_handler", event_type=evt.get("event_type"))
        return None


_subscriber: EventSubscriber | None = None


def get_event_subscriber() -> EventSubscriber:
    global _subscriber
    if _subscriber is None:
        _subscriber = EventSubscriber(
            bootstrap_servers="", group_id="agent-service",
        )
    return _subscriber
