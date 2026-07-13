"""lifespan 编排：启动断言 -> 存储就绪探测 -> 路线装配 -> consumer 启停。

把"只读旁路"从约定变成结构属性：最坏情况是"没检索出来"，不会产生写副作用。
口径见《rag-service-技术选型和实现方案》§3/§4.1。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.shared.config.rag_settings import RagSettings
from app.shared.web.container import Container

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI, settings: RagSettings, container: Container) -> AsyncIterator[None]:
    """启动编排。

    顺序：
    1. 启动断言（只读红线，§3）-- 任一失败即拒绝启动（fail-fast）；
    2. 存储就绪探测（按路线降级，§3.3）；
    3. 路线专属 schema 初始化（Neo4j SchemaInitializer / ChromaDB collection）；
       须先于 wire_routes：B 组合根读取 ``container.chroma_collection`` 构造
       ChunkRepo/VectorRetriever/BM25 投影，collection 在此步注入；
    4. 路线装配（按开关 wire_routes）；
    5. 装配后启动断言（B 摄入/E 工具注册）；
    yield；
    6. 关闭 consumer / 引擎。
    """
    # 1. 启动断言（只读红线）-- 预装配阶段
    await run_assertions(container.collect_pre_wiring_gates(), "pre-wiring")

    # 2. 存储就绪探测（按路线降级）
    health = await container.engines.probe()
    unhealthy = [k for k, ok in health.items() if not ok]
    if unhealthy:
        logger.warning("启动期存储不可用，对应路线将降级: %s", unhealthy)
    app.state.storage_health = health

    # 3. 路线专属 schema 初始化（须先于 wire_routes：组合根依赖 container.chroma_collection）
    await init_route_schemas(container)

    # 4. 路线装配（按开关）
    await container.wire_routes()

    # 4b. 启动断言 -- 装配后阶段（B 摄入/E 工具注册）
    await run_assertions(container.collect_post_wiring_gates(), "post-wiring")

    # 5. consumer 启停
    await container.start_consumers()

    logger.info(
        "rag-service 启动完成：document=%s traceability=%s agentic=%s",
        settings.document.enabled,
        settings.traceability.enabled,
        settings.agentic.enabled,
    )
    yield

    # 6. 关闭
    await container.stop_consumers()
    await container.dispose()
    logger.info("rag-service 已关闭")


async def run_assertions(gates: list, phase: str) -> None:
    """执行一组 ReadOnly*Gate 启动断言。任一失败即拒绝启动。"""
    for name, gate, target in gates:
        try:
            gate.assert_on(target)
            logger.info("启动断言通过[%s]: %s", phase, name)
        except Exception as exc:
            logger.error("启动断言失败[%s]: %s -> %s", phase, name, exc)
            raise


async def init_route_schemas(container: Container) -> None:
    """路线专属 schema 初始化（Neo4j SchemaInitializer / ChromaDB collection）。

    非Alembic：图库 DDL 幂等即可，ChromaDB collection 由代码初始化（chunk 不可变无 schema 演进）。
    """
    settings = container.settings
    if settings.document.enabled:
        from app.routes.document.infrastructure.chromadb.schema import ChromaCollectionInitializer

        initializer = ChromaCollectionInitializer(settings.chroma.collection)
        collection = initializer.ensure(container.engines.chroma())
        container.chroma_collection = collection
        logger.info("ChromaDB collection '%s' 已就绪", collection.name)
    if settings.traceability.enabled:
        from app.routes.traceability.infrastructure.neo4j.schema import SchemaInitializer

        driver = await container.engines.neo4j()
        await SchemaInitializer().ensure(driver)
        logger.info("Neo4j schema 已就绪")
