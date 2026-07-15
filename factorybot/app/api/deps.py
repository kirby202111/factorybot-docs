"""FastAPI 依赖：租户解析 + 服务注入。"""
from __future__ import annotations

from fastapi import Header, HTTPException

from app.container import Container, get_container
from app.domain.tenant import TenantContext


def resolve_tenant_context(
    *, tenant_id: str | None, workshop: str | None, line: str | None,
    role: str | None, user_id: str | None,
    is_mock: bool, default: TenantContext,
) -> TenantContext:
    """纯逻辑：按模式解析租户上下文（脱离 FastAPI Header 依赖，便于单测）。

    mock 模式：header 缺失时回退默认租户（dev/test 便利）。
    real 模式：X-Tenant-Id / X-Tenant-User-Id 必填（缺失返 400），避免无鉴权调用方
    被当作默认租户 WS-A；workshop/line/role 缺失用空串，不泄漏 mock 默认值。
    """
    if is_mock:
        return TenantContext(
            tenant_id=tenant_id or default.tenant_id,
            workshop=workshop or default.workshop,
            line=line or default.line,
            role=role or default.role,
            user_id=user_id or default.user_id,
            scopes=default.scopes,
        )
    # real 模式：身份 header 强制，杜绝无鉴权调用方回退为默认租户
    if not tenant_id or not user_id:
        raise HTTPException(
            status_code=400,
            detail="real 模式必须提供 X-Tenant-Id 与 X-Tenant-User-Id 请求头",
        )
    return TenantContext(
        tenant_id=tenant_id,
        workshop=workshop or "",
        line=line or "",
        role=role or "ENGINEER",
        user_id=user_id,
        scopes=default.scopes,
    )


def tenant_from_headers(
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_tenant_workshop: str | None = Header(None, alias="X-Tenant-Workshop"),
    x_tenant_line: str | None = Header(None, alias="X-Tenant-Line"),
    x_tenant_role: str | None = Header(None, alias="X-Tenant-Role"),
    x_tenant_user_id: str | None = Header(None, alias="X-Tenant-User-Id"),
) -> TenantContext:
    """FastAPI 依赖：从请求头解析租户，委托 resolve_tenant_context。"""
    c = get_container()
    return resolve_tenant_context(
        tenant_id=x_tenant_id, workshop=x_tenant_workshop, line=x_tenant_line,
        role=x_tenant_role, user_id=x_tenant_user_id,
        is_mock=c.settings.is_mock, default=c.default_tenant(),
    )


def get_container_dep() -> Container:
    return get_container()
