"""工装上下文 ACL：只读（钢网/程序/借还/收线记录）-- 编排 换线比对 + agent A 取证。"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.base import BaseAclClient
from app.infrastructure.acl.views import (
    ChangeoverCloseView,
    CurrentStencilView,
    LocalProgramView,
    StencilLendingView,
)


class ToolingAclClient(BaseAclClient):
    """工装上下文·只读。"""

    async def query_current_stencil(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> CurrentStencilView:
        return await self._get_view(
            CurrentStencilView, "/api/tooling/current-stencil", tenant=tenant,
            params={"asset_id": asset_id},
            fixture_rel="rest/current_stencil", fixture_key=asset_id,
        )

    async def query_local_program_version(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> LocalProgramView:
        return await self._get_view(
            LocalProgramView, "/api/tooling/local-program", tenant=tenant,
            params={"asset_id": asset_id},
            fixture_rel="rest/local_program", fixture_key=asset_id,
        )

    async def query_stencil_lending(
        self, stencil_id: str, tenant: TenantContext | None = None,
    ) -> StencilLendingView:
        return await self._get_view(
            StencilLendingView, f"/api/tooling/stencils/{stencil_id}/lending", tenant=tenant,
            fixture_rel="rest/stencil_lending", fixture_key=stencil_id,
        )

    async def query_last_changeover_close(
        self, asset_id: str, tenant: TenantContext | None = None,
    ) -> ChangeoverCloseView:
        return await self._get_view(
            ChangeoverCloseView, "/api/tooling/last-changeover-close", tenant=tenant,
            params={"asset_id": asset_id},
            fixture_rel="rest/last_changeover_close", fixture_key=asset_id,
        )
