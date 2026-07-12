"""物料上下文 ACL：只读（批次/BOM/齐套/供应商追溯）。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import (
    BomVersionView, KitStatusView, MaterialBatchView, SupplierTraceView, to_view,
)


class MaterialAclClient(BaseAclClient):
    """物料上下文·只读。"""

    async def query_material_batch(
        self, batch_no: str, tenant: TenantContext,
    ) -> MaterialBatchView:
        dto = await self._get(
            f"/api/material-batches/{batch_no}", tenant=tenant,
            fixture_rel="rest/material_batches", fixture_key=batch_no,
        )
        return to_view(MaterialBatchView, dto)

    async def query_bom_version(
        self, bom_id: str, version: str, tenant: TenantContext,
    ) -> BomVersionView:
        dto = await self._get(
            f"/api/boms/{bom_id}",
            tenant=tenant, params={"version": version},
            fixture_rel="rest/boms", fixture_key=f"{bom_id}:{version}",
        )
        return to_view(BomVersionView, dto)

    async def query_kit_status(
        self, work_order_id: str, tenant: TenantContext,
    ) -> KitStatusView:
        dto = await self._get(
            f"/api/work-orders/{work_order_id}/kit-status", tenant=tenant,
            fixture_rel="rest/kit_status", fixture_key=work_order_id,
        )
        view = to_view(KitStatusView, dto)
        # 派生 missing_material_ids + kit_ready
        view.missing_material_ids = [m.get("part_no", "") for m in view.missing_items]
        view.kit_ready = view.kit_rate >= 100 and not view.missing_items
        return view

    async def query_supplier_trace(
        self, batch_id: str, tenant: TenantContext,
    ) -> SupplierTraceView:
        dto = await self._get(
            f"/api/material-batches/{batch_id}/supplier-trace", tenant=tenant,
            fixture_rel="rest/supplier_trace", fixture_key=batch_id,
        )
        return to_view(SupplierTraceView, dto)
