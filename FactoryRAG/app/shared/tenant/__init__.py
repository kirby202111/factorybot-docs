"""shared/tenant -- 租户上下文 + 跨服务传递协议。

A/B/E 共用一个 ``TenantContext``，跨服务传递协议在 ``propagation.py`` 一处定义
（补齐各路线各自定义、无传递协议的缺口）。

口径见《rag-service-整体结构设计》§3.8、《技术选型和实现方案》§2.8。
"""
from app.shared.tenant.context import TenantContext
from app.shared.tenant.dependency import tenant_from_header, tenant_from_token
from app.shared.tenant.propagation import TenantPropagator

__all__ = ["TenantContext", "TenantPropagator", "tenant_from_token", "tenant_from_header"]
