"""SQLAlchemy 2.0 DeclarativeBase（async + asyncmy，MySQL）。"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 MySQL ORM 模型的基类。

    schema 分配（§5.1）：
    - ``rag_shared``：DomainEvent 幂等/位点基表（A/B 共用）
    - ``rag_trace``：A 的 index_idempotency/index_offset/subgraph_audit
    - ``rag_doc``：B 的治理/审计聚合导出表
    - ``rag_agentic``：E 的 answer_audit/route_trace

    各模型用 ``__table_args__ = {"schema": "rag_xxx"}`` 显式归属 schema。
    """

    pass
