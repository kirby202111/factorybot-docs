"""FastAPI 依赖：从 JWT / X-Tenant-Scope 解析 TenantContext。"""
from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException, status

from app.shared.tenant.context import TenantContext


def tenant_from_header(
    x_tenant_id: str = Header(default="", alias="X-Tenant-Id"),
    x_tenant_scope: str = Header(default="", alias="X-Tenant-Scope"),
) -> TenantContext:
    """从 ``X-Tenant-Id`` / ``X-Tenant-Scope`` header 解析租户上下文。

    默认解析方式（车间内网信任网关注入的 header）；生产可换 ``tenant_from_token``。
    """
    if not x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-Tenant-Id（租户上下文）",
        )
    scopes = [s.strip() for s in x_tenant_scope.split(",") if s.strip()]
    return TenantContext(tenant_id=x_tenant_id, tenant_scopes=scopes)


def tenant_from_token(authorization: str = Header(default="")) -> TenantContext:
    """从 JWT 解析租户上下文（解析 ``tenant_id`` / ``tenant_scopes`` claim）。

    生产推荐；此处给出骨架，具体 JWT 库按部署接入。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer token"
        )
    # TODO: 接入实际 JWT 校验库（如 python-jose），解析 claims。
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="JWT 解析未接入，请使用 tenant_from_header",
    )


def tenant_from_event(event: Any) -> TenantContext:
    """Kafka 消费时从事件 envelope metadata 还原租户上下文。"""
    return TenantContext(
        tenant_id=event.metadata.get("tenant_id", ""),
        tenant_scopes=[
            s.strip() for s in (event.metadata.get("tenant_scope") or "").split(",") if s.strip()
        ],
    )
