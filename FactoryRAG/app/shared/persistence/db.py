"""多存储 Engine 工厂：按 config 懒初始化，连接池分别配额。

单进程同时持有 Neo4j driver + ChromaDB client + MySQL asyncmy + Redis client。
ChromaDB 嵌入式跟随进程，Parquet 持久化到挂卷，无独立 service。
"""
from __future__ import annotations

import logging
from typing import Any

from app.shared.config.rag_settings import RagSettings

logger = logging.getLogger(__name__)


class DbEngines:
    """按 config 懒初始化的多存储 Engine 集合。

    连接池分别配额；lifespan 启动期做就绪探测，任一不可用按路线降级（§3.3）。
    """

    def __init__(self, settings: RagSettings) -> None:
        self._settings = settings
        self._mysql_engine: Any = None
        self._mysql_session_factory: Any = None
        self._neo4j_driver: Any = None
        self._chroma_client: Any = None
        self._redis: Any = None

    # ── MySQL（shared/A/E + B 幂等位点审计）──
    async def mysql(self) -> Any:
        if self._mysql_engine is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            cfg = self._settings.mysql
            self._mysql_engine = create_async_engine(
                cfg.dsn,
                pool_size=cfg.pool_size,
                max_overflow=cfg.max_overflow,
                pool_pre_ping=True,
            )
            self._mysql_session_factory = async_sessionmaker(
                self._mysql_engine, expire_on_commit=False
            )
        return self._mysql_engine

    async def mysql_session_factory(self) -> Any:
        await self.mysql()
        return self._mysql_session_factory

    # ── Neo4j（A 图主体 + DefectCatalog 向量索引）──
    async def neo4j(self) -> Any:
        if self._neo4j_driver is None:
            from neo4j import AsyncGraphDatabase  # type: ignore

            cfg = self._settings.neo4j
            self._neo4j_driver = AsyncGraphDatabase.driver(
                cfg.uri, auth=(cfg.username, cfg.password), max_connection_pool_size=cfg.max_connections
            )
        return self._neo4j_driver

    # ── ChromaDB（B 嵌入式，Parquet 持久化）──
    def chroma(self) -> Any:
        if self._chroma_client is None:
            import chromadb  # type: ignore
            from chromadb.config import Settings  # type: ignore

            cfg = self._settings.chroma
            self._chroma_client = chromadb.PersistentClient(
                path=cfg.persist_dir,
                settings=Settings(anonymized_telemetry=False),
            )
        return self._chroma_client

    # ── Redis（A 子图缓存 / B 检索缓存 / E 查询缓存）──
    async def redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis  # type: ignore

            self._redis = aioredis.from_url(self._settings.redis.url, decode_responses=True)
        return self._redis

    # ── 就绪探测（lifespan 调用，按路线降级）──
    async def probe(self) -> dict[str, bool]:
        """逐存储探测，返回 {存储名: 健康}。不可用项落 /ready，对应路线降级。"""
        health: dict[str, bool] = {}
        # MySQL
        try:
            from sqlalchemy import text

            engine = await self.mysql()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            health["mysql"] = True
        except Exception:
            health["mysql"] = False
        # Redis
        try:
            r = await self.redis()
            await r.ping()
            health["redis"] = True
        except Exception:
            health["redis"] = False
        # ChromaDB（嵌入式本地，基本不会失败）
        try:
            self.chroma().heartbeat()
            health["chroma"] = True
        except Exception:
            health["chroma"] = False
        # Neo4j（仅 A 开启时探测）
        if self._settings.traceability.enabled:
            try:
                drv = await self.neo4j()
                await drv.verify_connectivity()
                health["neo4j"] = True
            except Exception:
                health["neo4j"] = False
        return health

    async def dispose(self) -> None:
        if self._mysql_engine is not None:
            await self._mysql_engine.dispose()
        if self._neo4j_driver is not None:
            await self._neo4j_driver.close()
        if self._redis is not None:
            await self._redis.aclose()
