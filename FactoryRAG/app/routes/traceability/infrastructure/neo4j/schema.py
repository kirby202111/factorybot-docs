"""Neo4j schema 初始化（非 Alembic，图库 DDL 幂等即可）。

启动时幂等执行约束/索引/向量索引 DDL。向量索引：bge-m3 1024 维 cosine，
用于 SeedResolver 的 DefectCatalog 缺陷描述语义入口。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 所有 DDL 均 IF NOT EXISTS（幂等）。
_CONSTRAINTS = [
    "CREATE CONSTRAINT checkpoint_node_id IF NOT EXISTS FOR (n:CheckpointRecord) REQUIRE n.node_id IS UNIQUE",
    "CREATE CONSTRAINT wipunit_sn IF NOT EXISTS FOR (n:WipUnit) REQUIRE n.sn IS UNIQUE",
    "CREATE CONSTRAINT routeversion_unique IF NOT EXISTS FOR (n:RouteVersion) REQUIRE (n.route_id, n.route_version) IS UNIQUE",
    "CREATE CONSTRAINT bom_unique IF NOT EXISTS FOR (n:Bom) REQUIRE (n.bom_id, n.bom_version) IS UNIQUE",
    "CREATE CONSTRAINT qualitygaterule_unique IF NOT EXISTS FOR (n:QualityGateRule) REQUIRE (n.rule_id, n.rule_version) IS UNIQUE",
    "CREATE CONSTRAINT inventorybatch_unique IF NOT EXISTS FOR (n:InventoryBatch) REQUIRE n.batch_no IS UNIQUE",
    "CREATE CONSTRAINT defectcatalog_code IF NOT EXISTS FOR (n:DefectCatalog) REQUIRE n.defect_code IS UNIQUE",
]

_INDEXES = [
    "CREATE INDEX checkpoint_sn IF NOT EXISTS FOR (n:CheckpointRecord) ON (n.sn)",
    "CREATE INDEX checkpoint_wo IF NOT EXISTS FOR (n:CheckpointRecord) ON (n.work_order_id)",
    "CREATE INDEX routeversion_status IF NOT EXISTS FOR (n:RouteVersion) ON (n.status)",
    "CREATE INDEX inventorybatch_partno IF NOT EXISTS FOR (n:InventoryBatch) ON (n.part_no)",
]

_VECTOR_INDEX = """
CREATE VECTOR INDEX defect_name_idx IF NOT EXISTS
FOR (d:DefectCatalog) ON (d.name_embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: 1024,
    `vector.similarity_function`: 'cosine'
  }
}
"""


class SchemaInitializer:
    """启动时幂等执行约束/索引/向量索引 DDL。"""

    async def ensure(self, driver: Any) -> None:
        async with driver.session() as session:
            for stmt in _CONSTRAINTS + _INDEXES:
                await session.run(stmt)
            await session.run(_VECTOR_INDEX)
        logger.info("Neo4j schema 约束/索引/向量索引已就绪（幂等）")
