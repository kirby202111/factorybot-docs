"""A 子图审计仓库（MySQL rag_trace schema）。

工程师 UI 证据链回溯用；同源 trace_id 串联 agent-service 与 MES。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.routes.traceability.domain.subgraph import TraceSubgraph
from app.shared.persistence.base import Base


class SubgraphAuditModel(Base):
    """子图审计表（rag_trace）。供 /rag/trace/expand 与证据链回溯。"""

    __tablename__ = "subgraph_audit"
    __table_args__ = {"schema": "rag_trace"}

    subgraph_ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    seed_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    seed_value: Mapped[str] = mapped_column(String(128), nullable=False)
    version_kind: Mapped[str] = mapped_column(String(32), nullable=True)
    version_ref_id: Mapped[str] = mapped_column(String(128), nullable=True)
    version: Mapped[str] = mapped_column(String(32), nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubgraphRepo:
    """子图持久化（审计 + 回溯）。"""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def save(self, subgraph: TraceSubgraph, trace_id: str = "") -> None:
        anchor = subgraph.version_locked()
        async with self._session_factory() as session:
            session.add(
                SubgraphAuditModel(
                    subgraph_ref=subgraph.subgraph_ref,
                    seed_kind=subgraph.seed.props.get("seed_kind", subgraph.seed.label),
                    seed_value=subgraph.seed.props.get("seed_value", subgraph.seed.node_id),
                    version_kind=anchor.kind.value if anchor else None,
                    version_ref_id=anchor.ref_id if anchor else None,
                    version=anchor.version if anchor else None,
                    as_of=subgraph.as_of,
                    payload=json.loads(subgraph.model_dump_json()),
                    trace_id=trace_id,
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
