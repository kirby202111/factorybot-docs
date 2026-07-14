"""工艺管理上下文 ACL：只读（工艺路线，route_version 强制 + ACTIVE 校验）+ 受限写。

route_version 强制是版本一致性三段链的关键：
  图 SNAPSHOT_OF_ROUTE{route_version} -> 诊断 evidence.route_version -> 草稿 Draft.route_version
  -> MES 应用服务校验 ACTIVE
ACL 层在查工艺时强制 route_version 非空且 dto.status == ACTIVE，物理杜绝失效工艺。
"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import (
    FirstArticleView, ProcessRouteView, QualificationView, to_view,
)


class InactiveRouteError(RuntimeError):
    """工艺路线非 ACTIVE，拒绝返回（防失效工艺）。"""


class ProcessManagementAclClient(BaseAclClient):
    """工艺管理上下文·只读。"""

    async def query_route(
        self, route_id: str, route_version: str, tenant: TenantContext,
    ) -> ProcessRouteView:
        # route_version 强制：空则拒绝（红线）
        if not route_version:
            raise ValueError(f"查工艺必须带 route_version: route_id={route_id}")
        dto = await self._get(
            f"/api/process-routes/{route_id}",
            tenant=tenant, params={"version": route_version},
            fixture_rel="rest/process_routes",
            fixture_key=f"{route_id}:{route_version}",
        )
        view = to_view(ProcessRouteView, dto)
        # ACTIVE 校验：失效工艺物理拒绝
        if view.status != "ACTIVE":
            raise InactiveRouteError(
                f"工艺路线 {route_id} v{route_version} 状态={view.status}，非 ACTIVE，拒绝"
            )
        return view

    async def query_first_article_status(
        self, work_order_id: str, tenant: TenantContext,
    ) -> FirstArticleView:
        dto = await self._get(
            f"/api/work-orders/{work_order_id}/first-article", tenant=tenant,
            fixture_rel="rest/first_article", fixture_key=work_order_id,
        )
        return to_view(FirstArticleView, dto)

    async def check_qualification(
        self, route_id: str, route_version: str, tenant: TenantContext,
    ) -> QualificationView:
        dto = await self._get(
            f"/api/process-routes/{route_id}/qualification",
            tenant=tenant, params={"version": route_version},
            fixture_rel="rest/qualification",
            fixture_key=f"{route_id}:{route_version}",
        )
        return to_view(QualificationView, dto)


class ProcessWriteAclClient(BaseAclClient):
    """工艺管理上下文·受限写：激活工艺路线 / 发布 SOP。"""

    async def activate_route(
        self, route_id: str, version: str, confirmation, tenant: TenantContext,
    ) -> dict:
        expected = f"activate_route:{confirmation.session_id}"
        if not confirmation.valid_for(expected):
            raise PermissionError(f"token action 不匹配: expected={expected}")
        return await self._post(
            f"/api/process-routes/{route_id}/activate",
            body={
                "route_id": route_id, "version": version,
                "confirmation_id": confirmation.id,
                "confirmed_by": confirmation.user_id,
            },
            tenant=tenant, confirmation=confirmation,
            fixture_rel="orchestration/write_results", fixture_key="route_activate",
        )

    async def publish_sop(
        self, route_id: str, version: str, sop_content: dict,
        confirmation, tenant: TenantContext,
    ) -> dict:
        expected = f"publish_sop:{confirmation.session_id}"
        if not confirmation.valid_for(expected):
            raise PermissionError(f"token action 不匹配: expected={expected}")
        return await self._post(
            "/api/sop/publish",
            body={
                "route_id": route_id, "version": version,
                "sop_content": sop_content,
                "confirmation_id": confirmation.id,
                "confirmed_by": confirmation.user_id,
            },
            tenant=tenant, confirmation=confirmation,
            fixture_rel="orchestration/write_results", fixture_key="sop_publish",
        )
