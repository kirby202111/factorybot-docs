"""工装上下文 ACL：只读（钢网/程序/借还/收线记录）-- 编排 换线比对 + agent A 取证。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import (
    ChangeoverCloseView, CurrentStencilView, LocalProgramView, StencilLendingView, to_view,
)


class ToolingAclClient(BaseAclClient):
    """工装上下文·只读。"""

    async def query_current_stencil(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> CurrentStencilView:
        dto = await self._get(
            "/api/tooling/current-stencil",
            tenant=tenant, params={"asset_id": asset_id},
            fixture_rel="rest/current_stencil", fixture_key=asset_id,
        )
        return to_view(CurrentStencilView, dto)

    async def query_local_program_version(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> LocalProgramView:
        dto = await self._get(
            "/api/tooling/local-program",
            tenant=tenant, params={"asset_id": asset_id},
            fixture_rel="rest/local_program", fixture_key=asset_id,
        )
        return to_view(LocalProgramView, dto)

    async def query_stencil_lending(
        self, stencil_id: str, tenant: TenantContext | None = None,
    ) -> StencilLendingView:
        dto = await self._get(
            f"/api/tooling/stencils/{stencil_id}/lending",
            tenant=tenant,
            fixture_rel="rest/stencil_lending", fixture_key=stencil_id,
        )
        return to_view(StencilLendingView, dto)

    async def query_last_changeover_close(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> ChangeoverCloseView:
        dto = await self._get(
            "/api/tooling/last-changeover-close",
            tenant=tenant, params={"asset_id": asset_id},
            fixture_rel="rest/last_changeover_close", fixture_key=asset_id,
        )
        return to_view(ChangeoverCloseView, dto)
