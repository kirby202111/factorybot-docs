"""设备数据接入 / 设备工装台账上下文 ACL：只读。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import AssetStatusView, DeviceParamsView


class DeviceDataAclClient(BaseAclClient):
    """设备数据接入上下文·只读（设备参数时序）。"""

    async def query_device_params(
        self, asset_id: str, time_range_start: str, time_range_end: str,
        tenant: TenantContext,
    ) -> DeviceParamsView:
        return await self._get_view(
            DeviceParamsView, f"/api/device-data/{asset_id}/params", tenant=tenant,
            params={"from": time_range_start, "to": time_range_end},
            fixture_rel="rest/device_params", fixture_key=asset_id,
        )


class EquipmentAssetLedgerAclClient(BaseAclClient):
    """设备工装台账上下文·只读（资产状态）。"""

    async def query_asset_status(
        self, asset_id: str, tenant: TenantContext,
    ) -> AssetStatusView:
        return await self._get_view(
            AssetStatusView, f"/api/assets/{asset_id}/status", tenant=tenant,
            fixture_rel="rest/asset_status", fixture_key=asset_id,
        )
