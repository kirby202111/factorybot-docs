"""消费者位点表操作基类（落 MySQL ``rag_shared``）。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.persistence.models import IndexOffset


class OffsetRepo:
    """消费者位点表操作基类。

    ``enable.auto.commit=false``，手动 ack：投影事务成功后同事务推进位点。
    位点落 MySQL 而非 Kafka ``__consumer_offsets``，便于跨重启恢复 + 滞后度监控。
    """

    def __init__(self, consumer_group: str) -> None:
        self._group = consumer_group

    async def get(self, session: AsyncSession, topic: str, partition: int) -> int | None:
        stmt = select(IndexOffset).where(
            IndexOffset.consumer_group == self._group,
            IndexOffset.topic == topic,
            IndexOffset.partition_no == partition,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        return row.offset_no if row else None

    async def advance(
        self, session: AsyncSession, topic: str, partition: int, offset: int
    ) -> None:
        stmt = select(IndexOffset).where(
            IndexOffset.consumer_group == self._group,
            IndexOffset.topic == topic,
            IndexOffset.partition_no == partition,
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if row is None:
            session.add(
                IndexOffset(
                    consumer_group=self._group,
                    topic=topic,
                    partition_no=partition,
                    offset_no=offset,
                    updated_at=now,
                )
            )
        else:
            row.offset_no = offset
            row.updated_at = now
        await session.flush()
