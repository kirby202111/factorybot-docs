"""返修 / 返工上下文 ACL：只读（维修历史/返工单）+ 受限写（批次隔离）。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import (
    IsolationResult, RepairHistoryView, ReworkOrderListView, to_view,
)


class ReworkAclClient(BaseAclClient):
    """返修上下文·只读。"""

    async def query_repair_history(
        self, serial_no: str, tenant: TenantContext,
    ) -> RepairHistoryView:
        dto = await self._get(
            f"/api/repair-history/{serial_no}", tenant=tenant,
            fixture_rel="rest/repair_history", fixture_key=serial_no,
        )
        return to_view(RepairHistoryView, dto)

    async def query_rework_orders(
        self, wo_id: str, tenant: TenantContext,
    ) -> ReworkOrderListView:
        dto = await self._get(
            f"/api/work-orders/{wo_id}/rework-orders", tenant=tenant,
            fixture_rel="rest/rework_orders", fixture_key=wo_id,
        )
        return to_view(ReworkOrderListView, dto)


class ReworkWriteAclClient(BaseAclClient):
    """返工上下文·受限写：下达批次隔离。

    只接受带 confirmation token 的请求：校验 token action 匹配 + store.validate。
    落库走返工上下文应用服务（聚合根不变式 + 事务发件箱）。
    """

    def __init__(self, *args, confirmation_store=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._store = confirmation_store

    async def issue_isolation(
        self, batch_set: list[str], reason: str,
        confirmation, tenant: TenantContext,
    ) -> IsolationResult:
        expected_action = f"issue_isolation:{confirmation.session_id}"
        # 1. token action 匹配
        if not confirmation.valid_for(expected_action):
            raise PermissionError(
                f"token action 不匹配: expected={expected_action}, got={confirmation.action}"
            )
        # 2. store 校验（存在性 + action 一致）
        if self._store is not None and not await self._store.validate(
            confirmation.id, expected_action
        ):
            raise PermissionError("token 无效或已过期")
        # 3. 调应用服务 REST（header 带 X-Confirmation-Token）
        dto = await self._post(
            "/api/isolation-orders",
            body={
                "batches": batch_set, "reason": reason,
                "confirmation_id": confirmation.id,
                "confirmed_by": confirmation.user_id,
            },
            tenant=tenant, confirmation=confirmation,
            fixture_rel="l3/write_results", fixture_key="isolation",
        )
        return to_view(IsolationResult, dto)

    async def create_repair_order(
        self, asset_id: str, fault_time: str, description: str,
        confirmation, tenant: TenantContext,
    ) -> dict:
        """创建维修单（设备故障复产场景）。同样校验 token。"""
        expected_action = f"create_repair:{confirmation.session_id}"
        if not confirmation.valid_for(expected_action):
            raise PermissionError(f"token action 不匹配: expected={expected_action}")
        if self._store is not None and not await self._store.validate(
            confirmation.id, expected_action
        ):
            raise PermissionError("token 无效或已过期")
        return await self._post(
            "/api/repair-orders",
            body={
                "asset_id": asset_id, "fault_time": fault_time,
                "fault_description": description,
                "confirmation_id": confirmation.id,
                "confirmed_by": confirmation.user_id,
            },
            tenant=tenant, confirmation=confirmation,
            fixture_rel="l3/write_results", fixture_key="repair_order",
        )
