"""质量上下文 ACL：只读（不良率）。"""
from __future__ import annotations

from typing import Optional

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import DefectRateView, to_view


class QualityAclClient(BaseAclClient):
    """质量上下文·只读。"""

    async def query_defect_rate(
        self, tenant: TenantContext,
        batch_no: Optional[str] = None,
        work_order_id: Optional[str] = None,
        time_range_start: Optional[str] = None,
        time_range_end: Optional[str] = None,
    ) -> DefectRateView:
        key = batch_no or work_order_id or "_default"
        dto = await self._get(
            "/api/quality/defect-rate",
            tenant=tenant,
            params={
                "batch_no": batch_no, "wo_id": work_order_id,
                "from": time_range_start, "to": time_range_end,
            },
            fixture_rel="rest/defect_rate", fixture_key=key,
        )
        return to_view(DefectRateView, dto)
