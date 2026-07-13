"""跨服务租户传递协议（一处定义）。

出站 httpx 自动注入 ``X-Tenant-Scope``；Kafka 消费时从事件 envelope metadata 还原。
补齐各路线各自定义 TenantContext、无传递协议的缺口。
"""
from __future__ import annotations

from app.shared.tenant.context import TenantContext


class TenantPropagator:
    """租户跨服务传递协议。

    - 出站 httpx：``outbound_headers(tenant)`` 注入 ``X-Tenant-Id`` / ``X-Tenant-Scope``；
    - Kafka 消费：``to_event_metadata(tenant)`` 把租户塞进事件 envelope metadata；
    - Kafka 还原：``from_event_metadata(metadata)`` 还原 TenantContext。
    """

    @staticmethod
    def outbound_headers(tenant: TenantContext) -> dict[str, str]:
        return tenant.headers()

    @staticmethod
    def to_event_metadata(tenant: TenantContext) -> dict[str, str]:
        return {
            "tenant_id": tenant.tenant_id,
            "tenant_scope": ",".join(tenant.tenant_scopes),
        }

    @staticmethod
    def from_event_metadata(metadata: dict) -> TenantContext:
        scope_str = metadata.get("tenant_scope") or metadata.get("tenant_scopes") or ""
        if isinstance(scope_str, list):
            scopes = [s.strip() for s in scope_str if s]
        else:
            scopes = [s.strip() for s in str(scope_str).split(",") if s.strip()]
        return TenantContext(
            tenant_id=metadata.get("tenant_id", ""),
            tenant_scopes=scopes,
        )
