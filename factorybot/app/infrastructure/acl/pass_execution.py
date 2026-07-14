"""过点执行上下文 ACL：只读（过点记录/测试结果）+ 受限写（过点放行）。"""
from __future__ import annotations

from typing import Optional

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import PassRecordView, TestResultView, to_view


class PassExecutionAclClient(BaseAclClient):
    """过点执行上下文·只读。"""

    async def query_pass_records(
        self, serial_no: str, tenant: TenantContext,
    ) -> list[PassRecordView]:
        dto = await self._get(
            f"/api/pass-records/{serial_no}", tenant=tenant,
            fixture_rel="rest/pass_records", fixture_key=serial_no,
        )
        records = dto.get("records", []) if isinstance(dto, dict) else (dto or [])
        return [to_view(PassRecordView, r) for r in records]

    async def query_test_results(
        self, serial_no: str, tenant: TenantContext,
    ) -> list[TestResultView]:
        dto = await self._get(
            f"/api/test-results/{serial_no}", tenant=tenant,
            fixture_rel="rest/test_results", fixture_key=serial_no,
        )
        results = dto.get("results", []) if isinstance(dto, dict) else (dto or [])
        return [to_view(TestResultView, r) for r in results]


class PassExecutionWriteAclClient(BaseAclClient):
    """过点执行上下文·受限写：放行生产（走应用服务过点主事务 + 事务发件箱）。"""

    async def release(
        self, work_order_id: str, confirmation, tenant: TenantContext,
    ) -> dict:
        """POST /api/pass-execution/release，header 带 X-Confirmation-Token。"""
        await self._validate_confirmation(confirmation, "release")
        return await self._post(
            "/api/pass-execution/release",
            body={
                "work_order_id": work_order_id,
                "confirmation_id": confirmation.id,
                "confirmed_by": confirmation.user_id,
            },
            tenant=tenant, confirmation=confirmation,
            fixture_rel="orchestration/write_results", fixture_key="pass_release",
        )
