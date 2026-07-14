"""A 图检索器：seed -> 5M1E 子图（Cypher 多跳 + 版本锁定）。

核心：版本锁定走 ``SNAPSHOT_OF_ROUTE{route_version}`` 快照边（生产时物理锁定），
**不取**当前 ``status=ACTIVATED`` 的 RouteVersion。``CALL {}`` 子查询避免笛卡尔积。
``NEXT`` 边不物化（partition_key=record_id 非 sn，跨站同 SN 不有序），查询时 ORDER BY occurred_at。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.routes.traceability.domain.seed import Seed, SeedKind
from app.routes.traceability.domain.subgraph import (
    FiveM1ECluster,
    TraceEdge,
    TraceNode,
    TraceSubgraph,
)
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)

# WIP seed -> 5M1E 子图（CALL {} 子查询避免笛卡尔积；版本锁定走 SNAPSHOT_OF_ROUTE 边）。
_WIP_5M1E_CYPHER = """
MATCH (w:WipUnit {sn: $sn})
WHERE w.tenant_scope IN $tenant_scopes AND w.occurred_at <= $as_of
CALL {
  WITH w
  OPTIONAL MATCH (cr:CheckpointRecord)-[:FOR_UNIT]->(w) WHERE cr.occurred_at <= $as_of
  OPTIONAL MATCH (cr)-[:PRODUCED_TESTRESULT]->(t:TestResult)
  OPTIONAL MATCH (t)-[:JUDGED_BY]->(qv:QualityVerdict)
  OPTIONAL MATCH (qv)-[:CITES_DEFECT]->(dc:DefectCatalog)
  OPTIONAL MATCH (qv)-[ur:UNDER_RULE]->(qgr:QualityGateRule)
  RETURN collect(DISTINCT cr { .node_id, .station_id, .scanned_by, .decision, .occurred_at, .route_version }) AS man_checkpoints,
         collect(DISTINCT t { .test_id, .test_type, .raw_verdict }) AS measurements,
         collect(DISTINCT qv { .verdict_id, .business_verdict }) AS measurement_verdicts,
         collect(DISTINCT dc { .defect_code, .severity, .name }) AS defects
}
CALL {
  WITH w
  OPTIONAL MATCH (cr:CheckpointRecord)-[:FOR_UNIT]->(w) WHERE cr.occurred_at <= $as_of
  OPTIONAL MATCH (cr)-[:USED_EQUIPMENT]->(eq:Asset)
  RETURN collect(DISTINCT eq { .asset_id, .status }) AS machines
}
CALL {
  WITH w
  OPTIONAL MATCH (cr:CheckpointRecord)-[:FOR_UNIT]->(w) WHERE cr.occurred_at <= $as_of
  OPTIONAL MATCH (cr)-[sr:SNAPSHOT_OF_ROUTE]->(rvSnap:RouteVersion)
  OPTIONAL MATCH (rvSnap)-[:HAS_STEP]->(rs:RouteStep)
  RETURN collect(DISTINCT rvSnap { .route_id, .route_version, .status }) AS method_route,
         collect(DISTINCT rs { .step_no, .operation_id }) AS method_steps
}
CALL {
  WITH w
  OPTIONAL MATCH (w)-[:CONSUMED_BATCH]->(ib:InventoryBatch)
  OPTIONAL MATCH (ib)-[:SUPPLIED_BY]->(sup:Supplier)
  OPTIONAL MATCH (w)-[:BELONGS_TO]->(wo:WorkOrder)
  OPTIONAL MATCH (wo)-[:BINDS_BOM]->(bom:Bom)
  RETURN collect(DISTINCT ib { .batch_no, .location, .available_qty }) AS materials,
         collect(DISTINCT bom { .bom_id, .bom_version, .status }) AS method_bom,
         collect(DISTINCT sup { .supplier_id }) AS suppliers
}
RETURN w, man_checkpoints, measurements, measurement_verdicts, defects,
       machines, method_route, method_steps, method_bom, materials, suppliers
"""

_BATCH_5M1E_CYPHER = """
MATCH (ib:InventoryBatch {batch_no: $batch_no})
WHERE ib.tenant_scope IN $tenant_scopes AND ib.occurred_at <= $as_of
OPTIONAL MATCH (w:WipUnit)-[:CONSUMED_BATCH]->(ib)
WHERE w.tenant_scope IN $tenant_scopes AND w.occurred_at <= $as_of
RETURN ib, collect(DISTINCT w { .sn, .work_order_id }) AS affected_units
"""


class GraphRetriever:
    """seed -> 5M1E TraceSubgraph。"""

    def __init__(self, *, driver: Any, embedder: Any) -> None:
        self._driver = driver
        self._embedder = embedder

    async def expand_5m1e(
        self,
        seed: Seed,
        as_of: datetime,
        tenant: TenantContext,
        *,
        version: str | None = None,
        version_kind: str | None = None,
    ) -> TraceSubgraph:
        if seed.kind == SeedKind.WIP_UNIT:
            return await self._expand_from_wip(
                seed, as_of, tenant, version=version, version_kind=version_kind
            )
        if seed.kind == SeedKind.INVENTORY_BATCH:
            return await self._expand_from_batch(seed, as_of, tenant)
        # WorkOrder/Defect/Asset -> MVP 退化为 WipUnit 风格的空子图（phase 2 扩展）
        return self._empty_subgraph(seed, as_of)

    async def _expand_from_wip(
        self,
        seed: Seed,
        as_of: datetime,
        tenant: TenantContext,
        *,
        version: str | None = None,
        version_kind: str | None = None,
    ) -> TraceSubgraph:
        async with self._driver.session() as session:
            result = await session.run(
                _WIP_5M1E_CYPHER,
                sn=seed.value,
                tenant_scopes=tenant.tenant_scopes or [""],
                as_of=as_of.isoformat(),
            )
            records = await result.data()
        if not records:
            return self._empty_subgraph(seed, as_of)
        return self._map_wip(seed, as_of, records[0], version=version, version_kind=version_kind)

    async def _expand_from_batch(
        self, seed: Seed, as_of: datetime, tenant: TenantContext
    ) -> TraceSubgraph:
        async with self._driver.session() as session:
            result = await session.run(
                _BATCH_5M1E_CYPHER,
                batch_no=seed.value,
                tenant_scopes=tenant.tenant_scopes or [""],
                as_of=as_of.isoformat(),
            )
            records = await result.data()
        if not records:
            return self._empty_subgraph(seed, as_of)
        ib = records[0]["ib"]
        seed_node = TraceNode(
            label="InventoryBatch",
            bounded_context="物料",
            node_id=f"InventoryBatch:{ib.get('batch_no', seed.value)}",
            props={**ib, "seed_kind": seed.kind.value, "seed_value": seed.value},
        )
        return TraceSubgraph(seed=seed_node, clusters=FiveM1ECluster(), as_of=as_of)

    def _map_wip(
        self,
        seed: Seed,
        as_of: datetime,
        rec: dict,
        *,
        version: str | None = None,
        version_kind: str | None = None,
    ) -> TraceSubgraph:
        w = rec.get("w", {})
        seed_node = TraceNode(
            label="WipUnit",
            bounded_context="在制品执行",
            node_id=f"WipUnit:{w.get('sn', seed.value)}",
            props={**w, "seed_kind": seed.kind.value, "seed_value": seed.value},
        )
        man_all = [self._node("CheckpointRecord", "在制品执行", c) for c in rec.get("man_checkpoints", []) if c]
        # 历史回溯锁定具体版本：MVP 图仅锁 route，故仅在 kind=route（或未指定 kind）时
        # 按 CheckpointRecord.route_version 过滤过点节点。
        if version and (not version_kind or version_kind == "route"):
            man_all = [n for n in man_all if n.props.get("route_version") == version]
        measurement = (
            [self._node("TestResult", "质量", t) for t in rec.get("measurements", []) if t]
            + [self._node("QualityVerdict", "质量", q) for q in rec.get("measurement_verdicts", []) if q]
            + [self._node("DefectCatalog", "质量", d) for d in rec.get("defects", []) if d]
        )
        machine = [self._node("Asset", "设备", m) for m in rec.get("machines", []) if m]
        method = (
            [self._node("RouteVersion", "工艺管理", r) for r in rec.get("method_route", []) if r]
            + [self._node("RouteStep", "工艺管理", s) for s in rec.get("method_steps", []) if s]
            + [self._node("Bom", "物料", b) for b in rec.get("method_bom", []) if b]
        )
        material = (
            [self._node("InventoryBatch", "物料", m) for m in rec.get("materials", []) if m]
            + [self._node("Supplier", "物料", s) for s in rec.get("suppliers", []) if s]
        )
        clusters = FiveM1ECluster(
            man=man_all, machine=machine, material=material, method=method, measurement=measurement
        )
        edges = self._derive_edges(seed_node, clusters)
        return TraceSubgraph(seed=seed_node, clusters=clusters, edges=edges, as_of=as_of)

    @staticmethod
    def _node(label: str, ctx: str, props: dict) -> TraceNode:
        node_id = props.get("node_id") or f"{label}:{props.get('sn') or props.get('verdict_id') or props.get('defect_code') or props.get('batch_no') or props.get('asset_id') or props.get('route_id') or ''}"
        return TraceNode(label=label, bounded_context=ctx, node_id=node_id, props=props)

    @staticmethod
    def _derive_edges(seed_node: TraceNode, clusters: FiveM1ECluster) -> list[TraceEdge]:
        edges: list[TraceEdge] = []
        for cr in clusters.man:
            edges.append(TraceEdge(rel="FOR_UNIT", from_id=cr.node_id, to_id=seed_node.node_id))
            rv = cr.props.get("route_version")
            if rv:
                # SNAPSHOT_OF_ROUTE{route_version}：物理锁定版本（核心安全契约）
                edges.append(
                    TraceEdge(
                        rel="SNAPSHOT_OF_ROUTE",
                        from_id=cr.node_id,
                        to_id=f"RouteVersion:{cr.props.get('route_id','')}@{rv}",
                        version=rv,
                    )
                )
        return edges

    @staticmethod
    def _empty_subgraph(seed: Seed, as_of: datetime) -> TraceSubgraph:
        seed_node = TraceNode(
            label=seed.kind.value,
            bounded_context="未知",
            node_id=f"{seed.kind.value}:{seed.value}",
            props={"seed_kind": seed.kind.value, "seed_value": seed.value},
        )
        return TraceSubgraph(seed=seed_node, clusters=FiveM1ECluster(), as_of=as_of)
