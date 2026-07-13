"""A 路线专属 ACL（物料/质量，降级补齐用）。

公共 MES 客户端（工艺/过点）在 shared ``MesClients``；A 专属的物料/质量客户端留此。
均继承 ``BaseReadonlyAclClient``，方法名禁止写动词（``ReadOnlyAclGate`` 启动期扫描）。
rag-service 从不回写 MES。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.shared.acl.base_client import BaseReadonlyAclClient


class ConsumptionView(BaseModel):
    sn: str
    work_order_id: str | None = None
    batch_no: str | None = None
    part_no: str | None = None
    consumed_qty: float | None = None
    occurred_at: datetime | None = None


class BomView(BaseModel):
    bom_id: str
    bom_version: str
    status: str
    items: list[dict[str, Any]] = []


class BatchView(BaseModel):
    batch_no: str
    part_no: str | None = None
    supplier_id: str | None = None
    available_qty: float | None = None


class QualityVerdictView(BaseModel):
    verdict_id: str
    sn: str | None = None
    business_verdict: str
    defect_code: str | None = None
    occurred_at: datetime | None = None


class MaterialAclClient(BaseReadonlyAclClient):
    """物料上下文只读客户端。含 ``CONSUMED_BATCH`` 边降级补齐（payload 有 sn 无 lot_no）。"""

    async def fetch_consumption(
        self, sn: str, work_order_id: str, tenant: Any | None = None
    ) -> list[ConsumptionView]:
        data = await self._get(
            "/api/material/consumption",
            params={"sn": sn, "work_order_id": work_order_id},
            tenant=tenant,
        )
        return [ConsumptionView.model_validate(i) for i in data.get("items", [])]

    async def fetch_bom(
        self, bom_id: str, bom_version: str, tenant: Any | None = None
    ) -> BomView:
        data = await self._get(
            f"/api/material/bom/{bom_id}", params={"version": bom_version}, tenant=tenant
        )
        return BomView.model_validate(data)

    async def fetch_batch(self, batch_no: str, tenant: Any | None = None) -> BatchView:
        data = await self._get(f"/api/material/batches/{batch_no}", tenant=tenant)
        return BatchView.model_validate(data)


class QualityAclClient(BaseReadonlyAclClient):
    """质量上下文只读客户端。"""

    async def fetch_verdicts(self, sn: str, tenant: Any | None = None) -> list[QualityVerdictView]:
        data = await self._get("/api/quality/verdicts", params={"sn": sn}, tenant=tenant)
        return [QualityVerdictView.model_validate(i) for i in data.get("items", [])]
