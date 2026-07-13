"""aiokafka 消费者基类：手动 ack + 位点落 MySQL + 消费者组按主题前缀分组。

A/B 两路线逻辑高度同构，本类上移公共骨架；各路线只保留 ``handlers/``。
双重幂等：① MySQL ``index_idempotency`` 表（event_id+consumer_group）② 存储层 MERGE/upsert
（Neo4j MERGE / ChromaDB upsert by chunk_id）。位点：``enable.auto.commit=false``，
投影事务成功后同 MySQL 事务推进位点。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.shared.kafka.domain_event import DomainEvent
from app.shared.kafka.idempotency_repo import IdempotencyRepo
from app.shared.kafka.offset_repo import OffsetRepo
from app.shared.kafka.projection_handler import ProjectionHandler

logger = logging.getLogger(__name__)

# 事务句柄提供者：接收消费者 MySQL 会话，返回投影用事务句柄。
# B：返回同一 MySQL 会话（状态更新与幂等/位点同事务）；
# A：忽略 MySQL 会话，返回 Neo4j session（图投影，第二层幂等靠 MERGE）。
TxProvider = Callable[[Any], Awaitable[Any]]
# 事件类型 -> handler 路由器。
HandlerRouter = Callable[[str], ProjectionHandler | None]


class ConsumerGroup:
    """aiokafka 消费者基类。

    SRP：只管"消费循环 + 幂等 + 位点 + ack"；投影动作委托给 ``ProjectionHandler``。
    """

    def __init__(
        self,
        *,
        topics: list[str],
        group_id: str,
        bootstrap: str,
        idem_repo: IdempotencyRepo,
        offset_repo: OffsetRepo,
        session_factory: Any,                # async_sessionmaker（MySQL，幂等/位点）
        tx_provider: TxProvider,             # 投影事务句柄提供者（Neo4j session / MySQL session）
        route_handler: HandlerRouter,
    ) -> None:
        self._topics = topics
        self._group_id = group_id
        self._bootstrap = bootstrap
        self._idem = idem_repo
        self._offset = offset_repo
        self._session_factory = session_factory
        self._tx_provider = tx_provider
        self._route = route_handler
        self._consumer: Any = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def topics(self) -> list[str]:
        return list(self._topics)

    @property
    def group_id(self) -> str:
        return self._group_id

    async def start(self) -> None:
        """启动消费循环（后台任务）。"""
        try:
            from aiokafka import AIOKafkaConsumer  # type: ignore
        except Exception:  # pragma: no cover - 测试/无 Kafka 环境降级
            logger.warning("aiokafka 不可用，消费者组 %s 不启动", self._group_id)
            return

        self._consumer = AIOKafkaConsumer(
            *self._topics,
            group_id=self._group_id,
            bootstrap_servers=self._bootstrap,
            enable_auto_commit=False,          # 手动 ack
            auto_offset_reset="earliest",
            value_deserializer=lambda b: b,
        )
        await self._consumer.start()
        self._stopping = False
        self._task = asyncio.create_task(self._loop())
        logger.info("消费者组 %s 启动，订阅 %s", self._group_id, self._topics)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._consumer:
            await self._consumer.stop()
        logger.info("消费者组 %s 已停止", self._group_id)

    async def _loop(self) -> None:
        assert self._consumer is not None
        try:
            async for msg in self._consumer:
                await self._process(msg)
                await self._consumer.commit()  # 手动 ack
                if self._stopping:
                    break
        except asyncio.CancelledError:  # pragma: no cover
            raise

    async def _process(self, msg: Any) -> None:
        """单条消息处理：解析 -> 幂等 -> 投影 -> 幂等记录+位点推进（同 MySQL 事务）。"""
        import json

        try:
            payload = json.loads(msg.value)
        except Exception:
            logger.warning("消息解析失败，跳过: topic=%s partition=%s offset=%s", msg.topic, msg.partition, msg.offset)
            return

        event = DomainEvent.model_validate(payload)
        handler = self._route(event.event_type)
        if handler is None:
            logger.debug("无 handler 处理 %s，跳过", event.event_type)
            return

        async with self._session_factory() as session:
            # ① 幂等检查
            if await self._idem.exists(session, event.event_id):
                logger.debug("事件 %s 已处理，跳过", event.event_id)
                return
            # ② 投影（tx 句柄由 provider 提供；存储层 MERGE/upsert 是第二层幂等）
            tx = await self._tx_provider(session)
            try:
                await handler.handle(event, tx)
            except Exception:
                logger.exception("投影失败: event=%s handler=%s", event.event_id, type(handler).__name__)
                raise
            # ③ 幂等记录 + 位点推进（同 MySQL 事务）
            await self._idem.record(session, event.event_id, msg.topic)
            await self._offset.advance(session, msg.topic, msg.partition, msg.offset)
            await session.commit()
