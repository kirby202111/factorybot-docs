"""initial: 建 4 schema + 6 表

Revision ID: 0001
Revises:
Create Date: 2026-07-13

口径见《rag-service-整体结构设计》§5.1。表定义与 ORM model 一一对应：
- rag_shared：index_idempotency / index_offset（app.shared.persistence.models）
- rag_trace：subgraph_audit（traceability.infrastructure.neo4j.subgraph_repo）
- rag_doc：knowledge_document / document_version（document.infrastructure.chromadb.document_repo）
- rag_agentic：answer_audit / route_trace（agentic.infrastructure.persistence.models）

Neo4j 用 SchemaInitializer、ChromaDB 用 ChromaCollectionInitializer，均非 Alembic。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCHEMAS = ("rag_shared", "rag_trace", "rag_doc", "rag_agentic")


def upgrade() -> None:
    # ── 4 schema（MySQL 中 schema == database）──
    for schema in _SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # ── rag_shared：事件幂等 + 消费者位点（A/B 共用）──
    op.create_table(
        "index_idempotency",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("consumer_group", sa.String(64), primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        schema="rag_shared",
    )
    op.create_table(
        "index_offset",
        sa.Column("consumer_group", sa.String(64), primary_key=True),
        sa.Column("topic", sa.String(128), primary_key=True),
        sa.Column("partition_no", sa.Integer, primary_key=True),
        sa.Column("offset_no", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="rag_shared",
    )

    # ── rag_trace：子图审计（/rag/trace/expand + 证据链回溯）──
    op.create_table(
        "subgraph_audit",
        sa.Column("subgraph_ref", sa.String(255), primary_key=True),
        sa.Column("seed_kind", sa.String(32), nullable=False),
        sa.Column("seed_value", sa.String(128), nullable=False),
        sa.Column("version_kind", sa.String(32), nullable=True),
        sa.Column("version_ref_id", sa.String(128), nullable=True),
        sa.Column("version", sa.String(32), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="rag_trace",
    )

    # ── rag_doc：文档元数据（chunk 向量在 ChromaDB，非 MySQL）──
    op.create_table(
        "knowledge_document",
        sa.Column("document_id", sa.String(64), primary_key=True),
        sa.Column("doc_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("tenant_scope", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="rag_doc",
    )
    op.create_table(
        "document_version",
        sa.Column("version_id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("version_no", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("file_ref", sa.String(512), nullable=False),
        sa.Column("file_content_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("bindings", sa.JSON, nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="rag_doc",
    )
    op.create_index(
        "ix_document_version_document_id",
        "document_version",
        ["document_id"],
        schema="rag_doc",
    )

    # ── rag_agentic：答案审计 + 路由 trace（/agent/explain 证据链）──
    op.create_table(
        "answer_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("intent", sa.String(32), nullable=False),
        sa.Column("route_taken", sa.String(32), nullable=False),
        sa.Column("tool_chain", sa.JSON, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("needs_human_review", sa.Integer, nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="rag_agentic",
    )
    op.create_table(
        "route_trace",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("audit_id", sa.String(36), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("view_summary", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("traceparent", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="rag_agentic",
    )
    op.create_index(
        "ix_route_trace_audit_id",
        "route_trace",
        ["audit_id"],
        schema="rag_agentic",
    )


def downgrade() -> None:
    # 表按依赖逆序删；schema 删除时 MySQL 会级联删表，此处显式删表便于审阅。
    op.drop_index("ix_route_trace_audit_id", table_name="route_trace", schema="rag_agentic")
    op.drop_table("route_trace", schema="rag_agentic")
    op.drop_table("answer_audit", schema="rag_agentic")

    op.drop_index("ix_document_version_document_id", table_name="document_version", schema="rag_doc")
    op.drop_table("document_version", schema="rag_doc")
    op.drop_table("knowledge_document", schema="rag_doc")

    op.drop_table("subgraph_audit", schema="rag_trace")

    op.drop_table("index_offset", schema="rag_shared")
    op.drop_table("index_idempotency", schema="rag_shared")

    for schema in _SCHEMAS:
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
