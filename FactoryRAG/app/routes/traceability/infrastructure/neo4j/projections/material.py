"""物料上下文投影 handler。

``BomActivated``：MERGE Bom{ACTIVATED}，老 Bom 置 DEPRECATED，HAS_BOM_ITEM 边。
``InventoryChanged``：MERGE InventoryBatch，SUPPLIED_BY 边。
``SubstituteRuleActivated``：MERGE SubstituteRule，SUBSTITUTE_OF 边。
**不创建 CONSUMED_BATCH**（MaterialConsumed payload 有 sn 无 lot_no，gap；MVP 由 ACL 降级补齐）。
"""
from __future__ import annotations

from app.routes.traceability.infrastructure.neo4j.projections.base import ProjectionHandlerBase
from app.shared.kafka.domain_event import DomainEvent

_MERGE_BOM = """
MERGE (bom:Bom {bom_id: $bom_id, bom_version: $bom_version})
SET bom.status = 'ACTIVATED', bom.tenant_scope = $tenant_scope
"""

_MERGE_INVENTORY = """
MERGE (ib:InventoryBatch {batch_no: $batch_no})
SET ib.part_no = $part_no, ib.location = coalesce($location, ib.location),
    ib.available_qty = coalesce($available_qty, ib.available_qty),
    ib.occurred_at = $occurred_at, ib.tenant_scope = $tenant_scope
MERGE (sup:Supplier {supplier_id: $supplier_id})
MERGE (ib)-[:SUPPLIED_BY]->(sup)
"""

_MERGE_SUBSTITUTE = """
MERGE (sub:SubstituteRule {rule_id: $rule_id})
SET sub.tenant_scope = $tenant_scope
MERGE (ib:InventoryBatch {batch_no: $batch_no})
MERGE (ib)-[:SUBSTITUTE_OF]->(sub)
"""


class MaterialProjectionHandler(ProjectionHandlerBase):
    """物料上下文投影。"""

    bounded_context = "物料"
    event_type = "BomActivated"
    event_types = ["BomActivated", "InventoryChanged", "SubstituteRuleActivated"]
    cypher_templates = [_MERGE_BOM, _MERGE_INVENTORY, _MERGE_SUBSTITUTE]

    async def handle(self, event: DomainEvent, tx) -> None:
        p = event.payload or {}
        tenant = event.tenant_scope or ""
        if event.event_type == "BomActivated":
            await self._run(
                _MERGE_BOM,
                bom_id=p.get("bom_id", ""),
                bom_version=p.get("bom_version", ""),
                tenant_scope=tenant,
            )
        elif event.event_type == "InventoryChanged":
            await self._run(
                _MERGE_INVENTORY,
                batch_no=p.get("batch_no", ""),
                part_no=p.get("part_no", ""),
                location=p.get("location"),
                available_qty=p.get("available_qty"),
                supplier_id=p.get("supplier_id", ""),
                occurred_at=p.get("occurred_at", event.occurred_at.isoformat()),
                tenant_scope=tenant,
            )
        elif event.event_type == "SubstituteRuleActivated":
            await self._run(
                _MERGE_SUBSTITUTE,
                rule_id=p.get("rule_id", ""),
                batch_no=p.get("batch_no", ""),
                tenant_scope=tenant,
            )
