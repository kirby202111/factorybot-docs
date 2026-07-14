"""物料上下文 ACL：只读（批次/BOM/齐套/供应商追溯）。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import (
    BomVersionView,
    KitStatusView,
    MaterialBatchView,
    SupplierTraceView,
)


class MaterialAclClient(BaseAclClient):
    """物料上下文·只读。"""

    async def query_material_batch(
        self, batch_no: str, tenant: TenantContext,
    ) -> MaterialBatchView:
        return await self._get_view(
            MaterialBatchView, f"/api/material-batches/{batch_no}", tenant=tenant,
            fixture_rel="rest/material_batches", fixture_key=batch_no,
        )

    async def query_bom_version(
        self, bom_id: str, version: str, tenant: TenantContext,
    ) -> BomVersionView:
        return await self._get_view(
            BomVersionView, f"/api/boms/{bom_id}", tenant=tenant,
            params={"version": version},
            fixture_rel="rest/boms", fixture_key=f"{bom_id}:{version}",
        )

    async def query_kit_status(
        self, work_order_id: str, tenant: TenantContext,
    ) -> KitStatusView:
        view = await self._get_view(
            KitStatusView, f"/api/work-orders/{work_order_id}/kit-status", tenant=tenant,
            fixture_rel="rest/kit_status", fixture_key=work_order_id,
        )
        # 派生 missing_material_ids + kit_ready
        view.missing_material_ids = [m.get("part_no", "") for m in view.missing_items]
        view.kit_ready = view.kit_rate >= 100 and not view.missing_items
        return view

    async def query_supplier_trace(
        self, batch_id: str, tenant: TenantContext,
    ) -> SupplierTraceView:
        return await self._get_view(
            SupplierTraceView, f"/api/material-batches/{batch_id}/supplier-trace", tenant=tenant,
            fixture_rel="rest/supplier_trace", fixture_key=batch_id,
        )
