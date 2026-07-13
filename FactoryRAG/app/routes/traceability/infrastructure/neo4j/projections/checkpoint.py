"""过点执行上下文投影 handler。

处理 ``CheckpointScanned``/``CheckpointReleased``/``CheckpointBlocked``/
``TestResultStructured``/``RoutingProgressed``。三事件 co-MERGE 到同一 CheckpointRecord 节点
（Scanned 设 equipment_id/scanned_by，Released 设 route_version/decision=PASS，
Blocked 设 decision=BLOCK/blocking_reason）。不创建 NEXT 边（partition_key=record_id 非 sn）。
"""
from __future__ import annotations

from typing import Any

from app.routes.traceability.infrastructure.neo4j.projections.base import ProjectionHandlerBase
from app.shared.kafka.domain_event import DomainEvent

_MERGE_CHECKPOINT = """
MERGE (cr:CheckpointRecord {node_id: $node_id})
SET cr.station_id = $station_id,
    cr.sn = $sn,
    cr.work_order_id = $work_order_id,
    cr.scanned_by = coalesce($scanned_by, cr.scanned_by),
    cr.equipment_id = coalesce($equipment_id, cr.equipment_id),
    cr.route_version = coalesce($route_version, cr.route_version),
    cr.decision = coalesce($decision, cr.decision),
    cr.blocking_reason = coalesce($blocking_reason, cr.blocking_reason),
    cr.occurred_at = $occurred_at,
    cr.tenant_scope = $tenant_scope
"""

_MERGE_WIP = """
MERGE (w:WipUnit {sn: $sn})
SET w.work_order_id = $work_order_id, w.occurred_at = $occurred_at,
    w.tenant_scope = $tenant_scope
MERGE (wo:WorkOrder {work_order_id: $work_order_id})
MERGE (cr:CheckpointRecord {node_id: $node_id})
MERGE (cr)-[:FOR_UNIT]->(w)
MERGE (cr)-[:BELONGS_TO]->(wo)
"""

_MERGE_SNAPSHOT_EDGE = """
MATCH (cr:CheckpointRecord {node_id: $node_id}), (rv:RouteVersion {route_id: $route_id, route_version: $route_version})
MERGE (cr)-[:SNAPSHOT_OF_ROUTE {route_version: $route_version}]->(rv)
"""


class CheckpointProjectionHandler(ProjectionHandlerBase):
    """过点执行上下文投影。"""

    bounded_context = "在制品执行"
    event_type = "CheckpointScanned"
    event_types = [
        "CheckpointScanned",
        "CheckpointReleased",
        "CheckpointBlocked",
        "TestResultStructured",
        "RoutingProgressed",
    ]
    cypher_templates = [_MERGE_CHECKPOINT, _MERGE_WIP, _MERGE_SNAPSHOT_EDGE]

    async def handle(self, event: DomainEvent, tx: Any) -> None:
        p = event.payload or {}
        node_id = p.get("checkpoint_id") or f"CheckpointRecord:{p.get('sn')}-{p.get('station_id')}"
        await self._run(
            _MERGE_CHECKPOINT,
            node_id=node_id,
            station_id=p.get("station_id", ""),
            sn=p.get("sn", ""),
            work_order_id=p.get("work_order_id", ""),
            scanned_by=p.get("scanned_by"),
            equipment_id=p.get("equipment_id"),
            route_version=p.get("route_version"),
            decision=p.get("decision"),
            blocking_reason=p.get("blocking_reason"),
            occurred_at=p.get("occurred_at", event.occurred_at.isoformat()),
            tenant_scope=event.tenant_scope or "",
        )
        if p.get("sn") and p.get("work_order_id"):
            await self._run(
                _MERGE_WIP,
                node_id=node_id,
                sn=p["sn"],
                work_order_id=p["work_order_id"],
                occurred_at=p.get("occurred_at", event.occurred_at.isoformat()),
                tenant_scope=event.tenant_scope or "",
            )
        if p.get("route_id") and p.get("route_version"):
            await self._run(
                _MERGE_SNAPSHOT_EDGE,
                node_id=node_id,
                route_id=p["route_id"],
                route_version=p["route_version"],
            )
