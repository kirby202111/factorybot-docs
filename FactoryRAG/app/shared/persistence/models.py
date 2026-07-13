"""shared 幂等/位点基表（rag_shared schema，A/B 共用）。

B 的幂等/位点归 rag_shared；治理/审计聚合导出表在 rag_doc（路线模块内定义）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.persistence.base import Base


class IndexIdempotency(Base):
    """事件幂等表（A/B 共用）。

    PK (event_id, consumer_group)：重复投递同一 event_id 被挡在存储写入之前。
    """

    __tablename__ = "index_idempotency"
    __table_args__ = {"schema": "rag_shared"}

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    consumer_group: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndexOffset(Base):
    """消费者位点表（A/B 共用）。

    PK (consumer_group, topic, partition_no)：跨重启恢复，滞后度监控。
    """

    __tablename__ = "index_offset"
    __table_args__ = {"schema": "rag_shared"}

    consumer_group: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), primary_key=True)
    partition_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    offset_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
