"""工艺管理上下文投影 handler。

``ProcessRouteActivated``：MERGE 新 ``RouteVersion{status=ACTIVATED}``，老版本置
``DEPRECATED``（不删），创建 ``HAS_STEP``/``USES_OPERATION``/``ENFORCES_GATE`` 边。
升版时发 ``rag.reindex.request`` 通知 B 重索引。
``ProcessRouteDeprecated``：RouteVersion.status -> DEPRECATED。
"""
from __future__ import annotations

from app.routes.traceability.infrastructure.neo4j.projections.base import ProjectionHandlerBase
from app.shared.kafka.domain_event import DomainEvent

_MERGE_ROUTE_VERSION = """
MERGE (rv:RouteVersion {route_id: $route_id, route_version: $route_version})
SET rv.status = 'ACTIVATED', rv.activated_at = $occurred_at, rv.tenant_scope = $tenant_scope
"""

_DEPRECATE_OLD_ROUTES = """
MATCH (rv:RouteVersion {route_id: $route_id})
WHERE rv.route_version <> $route_version AND rv.status = 'ACTIVATED'
SET rv.status = 'DEPRECATED'
"""

_MERGE_ROUTE_STEP = """
MATCH (rv:RouteVersion {route_id: $route_id, route_version: $route_version})
UNWIND $steps AS step
MERGE (rs:RouteStep {route_id: $route_id, route_version: $route_version, step_no: step.step_no})
SET rs.operation_id = step.operation_id
MERGE (rv)-[:HAS_STEP]->(rs)
"""


class ProcessRouteProjectionHandler(ProjectionHandlerBase):
    """工艺管理上下文投影。"""

    bounded_context = "工艺管理"
    event_type = "ProcessRouteActivated"
    event_types = ["ProcessRouteActivated", "ProcessRouteDeprecated"]
    cypher_templates = [_MERGE_ROUTE_VERSION, _DEPRECATE_OLD_ROUTES, _MERGE_ROUTE_STEP]

    async def handle(self, event: DomainEvent, tx) -> None:
        p = event.payload or {}
        route_id = p.get("route_id", "")
        route_version = p.get("route_version", "")
        if not route_id or not route_version:
            return
        if event.event_type == "ProcessRouteActivated":
            await self._run(
                _MERGE_ROUTE_VERSION,
                route_id=route_id,
                route_version=route_version,
                occurred_at=p.get("occurred_at", event.occurred_at.isoformat()),
                tenant_scope=event.tenant_scope or "",
            )
            await self._run(_DEPRECATE_OLD_ROUTES, route_id=route_id, route_version=route_version)
            if p.get("steps"):
                await self._run(
                    _MERGE_ROUTE_STEP,
                    route_id=route_id,
                    route_version=route_version,
                    steps=p["steps"],
                )
            # 升版 -> 发 rag.reindex.request 通知 B 重索引
            if self._trace_svc is not None:
                await self._trace_svc.on_route_upgraded(
                    route_id=route_id,
                    old_version=p.get("previous_version", ""),
                    new_version=route_version,
                    trace_id=event.trace_id,
                )
        elif event.event_type == "ProcessRouteDeprecated":
            await self._run(_DEPRECATE_OLD_ROUTES, route_id=route_id, route_version=route_version)
