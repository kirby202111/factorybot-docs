"""设备遥测 / FMEA / 产品敏感度 ACL：只读 -- 编排 agent B 故障隔离范围判定取证。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import (
    EquipmentTelemetryView,
    ProcessFmeaView,
    ProductSensitivityView,
)


class EquipmentTelemetryAclClient(BaseAclClient):
    """设备遥测·只读（agent B 工具集）。"""

    async def query_equipment_telemetry(
        self, asset_id: str, start: str, end: str,
        tenant: TenantContext | None = None,
    ) -> EquipmentTelemetryView:
        return await self._get_view(
            EquipmentTelemetryView, f"/api/equipment/{asset_id}/telemetry", tenant=tenant,
            params={"from": start, "to": end},
            fixture_rel="rest/equipment_telemetry", fixture_key=asset_id,
        )

    async def query_fault_history(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> dict:
        # 返回原始 dict（非 View），不走 _get_view
        return await self._get(
            f"/api/equipment/{asset_id}/fault-history", tenant=tenant,
            fixture_rel="rest/fault_history", fixture_key=asset_id,
        )

    async def query_process_fmea(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> ProcessFmeaView:
        return await self._get_view(
            ProcessFmeaView, f"/api/equipment/{asset_id}/fmea", tenant=tenant,
            fixture_rel="rest/process_fmea", fixture_key=asset_id,
        )

    async def query_batches_in_window(
        self, start: str, end: str, tenant: TenantContext | None = None,
    ) -> dict:
        # 返回原始 dict（非 View），不走 _get_view
        return await self._get(
            "/api/wip/batches-in-window",
            tenant=tenant, params={"from": start, "to": end},
            fixture_rel="rest/batches_in_window", fixture_key="_default",
        )

    async def query_product_sensitivity(
        self, batch_ids: list[str], tenant: TenantContext | None = None,
    ) -> ProductSensitivityView:
        return await self._get_view(
            ProductSensitivityView, "/api/products/sensitivity", tenant=tenant,
            params={"batch_ids": ",".join(batch_ids)},
            fixture_rel="rest/product_sensitivity", fixture_key="_default",
        )
