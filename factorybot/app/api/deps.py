"""FastAPI 依赖：租户解析 + 服务注入。"""
from __future__ import annotations

from fastapi import Header

from app.container import Container, get_container
from app.domain.tenant import TenantContext


def tenant_from_headers(
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_tenant_workshop: str | None = Header(None, alias="X-Tenant-Workshop"),
    x_tenant_line: str | None = Header(None, alias="X-Tenant-Line"),
    x_tenant_role: str | None = Header(None, alias="X-Tenant-Role"),
    x_tenant_user_id: str | None = Header(None, alias="X-Tenant-User-Id"),
) -> TenantContext:
    """从请求头解析租户；缺省用容器默认租户（mock 场景）。"""
    c = get_container()
    base = c.default_tenant()
    return TenantContext(
        tenant_id=x_tenant_id or base.tenant_id,
        workshop=x_tenant_workshop or base.workshop,
        line=x_tenant_line or base.line,
        role=x_tenant_role or base.role,
        user_id=x_tenant_user_id or base.user_id,
        scopes=base.scopes,
    )


def get_container_dep() -> Container:
    return get_container()
