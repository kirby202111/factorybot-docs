"""Alembic 迁移环境（async + asyncmy，MySQL 多 schema）。

- DB URL 从 ``RagSettings``（``RAG_MYSQL__DSN``）注入，覆盖 alembic.ini 占位；
- ``target_metadata = Base.metadata``，import 全部 model 模块以注册到 metadata；
- async engine + ``connection.run_sync(do_run_migrations)`` 跑 DDL。

口径见《rag-service-整体结构设计》§5.1。Neo4j/ChromaDB 不走 Alembic（各自有 Initializer）。
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import load_settings
from app.shared.persistence.base import Base

# ── 注册全部 ORM model 到 Base.metadata ──
# import 即注册；这些模块只依赖 sqlalchemy + pydantic，不触发重依赖（chromadb/neo4j/langgraph）。
import app.shared.persistence.models  # noqa: F401  (rag_shared: index_idempotency/index_offset)
import app.routes.traceability.infrastructure.neo4j.subgraph_repo  # noqa: F401  (rag_trace: subgraph_audit)
import app.routes.document.infrastructure.chromadb.document_repo  # noqa: F401  (rag_doc: knowledge_document/document_version)
import app.routes.agentic.infrastructure.persistence.models  # noqa: F401  (rag_agentic: answer_audit/route_trace)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 用 RagSettings 的 MySQL DSN 覆盖 alembic.ini 占位。
_settings = load_settings()
config.set_main_option("sqlalchemy.url", _settings.mysql.dsn)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不连库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式（async）：asyncmy engine + run_sync 跑迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
