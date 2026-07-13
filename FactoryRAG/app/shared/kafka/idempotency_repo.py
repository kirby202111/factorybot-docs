"""``event_id`` 幂等表操作基类（A/B 共用，落 MySQL ``rag_shared``）。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.persistence.models import IndexIdempotency


class IdempotencyRepo:
    """事件幂等表操作基类。

    A/B 两路线共用模式：消费前查 ``event_id``+``consumer_group`` 是否已处理，
    已处理则跳过并推进位点；处理成功后同事务写入幂等记录。
    """

    def __init__(self, consumer_group: str) -> None:
        self._group = consumer_group

    async def exists(self, session: AsyncSession, event_id: str) -> bool:
        stmt = select(IndexIdempotency).where(
            IndexIdempotency.event_id == event_id,
            IndexIdempotency.consumer_group == self._group,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def record(self, session: AsyncSession, event_id: str, topic: str) -> None:
        session.add(
            IndexIdempotency(
                event_id=event_id,
                consumer_group=self._group,
                topic=topic,
                projected_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
