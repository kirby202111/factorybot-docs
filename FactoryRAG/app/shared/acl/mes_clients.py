"""对 MES 14 上下文只读 REST 的客户端集合（A/B 共享，降级补齐用）。

公共部分上移到 shared（工艺/过点上下文，A/B 都用）；A 专属的物料/质量 ACL
留在 ``routes/traceability/infrastructure/acl/``。所有客户端继承
``BaseReadonlyAclClient``，方法名禁止写动词（``ReadOnlyAclGate`` 启动期扫描）。

rag-service 从不回写 MES；图库/向量库崩返回 503 不阻塞生产。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from app.shared.acl.base_client import BaseReadonlyAclClient


# ── ACL 响应视图（只读，ACL 边界的防腐层 DTO）──


class RouteVersionView(BaseModel):
    """工艺路线版本视图。"""

    route_id: str
    route_version: str
    status: str                      # ACTIVE | DEPRECATED | ARCHIVED
    activated_at: datetime | None = None


class CheckpointView(BaseModel):
    """过点记录视图。"""

    checkpoint_id: str
    sn: str
    work_order_id: str | None = None
    route_id: str | None = None
    route_version: str | None = None
    station_id: str | None = None
    decision: str | None = None
    occurred_at: datetime | None = None


# ── 共享 MES 只读客户端 ──


class ProcessManagementAclClient(BaseReadonlyAclClient):
    """工艺管理上下文只读客户端（A/B 共享）。

    用于：B 检索时调用方仅有 route_id 无 route_version，查当前 ACTIVE 版本带入
    （仅"查当前生效"场景，历史回溯必须带具体版本）；A 投影降级补齐。
    """

    async def fetch_route_version(
        self, route_id: str, route_version: str, tenant: Any | None = None
    ) -> RouteVersionView:
        data = await self._get(
            f"/api/process-routes/{route_id}",
            params={"version": route_version},
            tenant=tenant,
        )
        return RouteVersionView.model_validate(data)

    async def fetch_active_route_version(self, route_id: str, tenant: Any | None = None) -> str:
        """查当前 ACTIVE 版本（仅"查当前生效"场景；历史回溯必须带具体版本）。"""
        data = await self._get(
            f"/api/process-routes/{route_id}/active", tenant=tenant
        )
        return str(data["route_version"])


class CheckpointAclClient(BaseReadonlyAclClient):
    """过点执行上下文只读客户端（A/B 共享）。"""

    async def fetch_checkpoints(self, sn: str, tenant: Any | None = None) -> list[CheckpointView]:
        data = await self._get("/api/checkpoints", params={"sn": sn}, tenant=tenant)
        return [CheckpointView.model_validate(item) for item in data.get("items", [])]

    async def fetch_route_version_by_sn(self, sn: str, tenant: Any | None = None) -> str | None:
        """该单件当时的 route_version（历史 SOP 检索用）。"""
        cps = await self.fetch_checkpoints(sn, tenant=tenant)
        for cp in cps:
            if cp.route_version:
                return cp.route_version
        return None


class MesClients:
    """对 MES 14 上下文只读 REST 的客户端集合（A/B 共享）。

    路线专属客户端（A 的物料/质量）由各路线自行注册到 ``extra``。
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        mes_base_url: str,
        tenant_propagator: Any | None = None,
    ) -> None:
        self._http = http
        self.process_management = ProcessManagementAclClient(http, mes_base_url, tenant_propagator)
        self.checkpoint = CheckpointAclClient(http, mes_base_url, tenant_propagator)
        self.extra: dict[str, BaseReadonlyAclClient] = {}

    def register(self, name: str, client: BaseReadonlyAclClient) -> None:
        self.extra[name] = client

    def all_clients(self) -> list[BaseReadonlyAclClient]:
        """供 ``ReadOnlyAclGate`` 扫描。"""
        return [self.process_management, self.checkpoint, *self.extra.values()]
