"""工单管理上下文 ACL：只读（工单 + 进度）。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import WorkOrderProgressView, WorkOrderView


class WorkOrderManagementAclClient(BaseAclClient):
    """工单管理上下文·只读。"""

    async def query_work_order(
        self, wo_id: str, tenant: TenantContext,
    ) -> WorkOrderView:
        return await self._get_view(
            WorkOrderView, f"/api/work-orders/{wo_id}", tenant=tenant,
            fixture_rel="rest/work_orders", fixture_key=wo_id,
        )

    async def query_wo_progress(
        self, wo_id: str, tenant: TenantContext,
    ) -> WorkOrderProgressView:
        return await self._get_view(
            WorkOrderProgressView, f"/api/work-orders/{wo_id}/progress", tenant=tenant,
            fixture_rel="rest/wo_progress", fixture_key=wo_id,
        )
