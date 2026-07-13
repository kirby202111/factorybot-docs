"""pytest 共享 fixture。"""
from __future__ import annotations

import pytest

from app.shared.tenant.context import TenantContext


@pytest.fixture
def tenant() -> TenantContext:
    """跨路线测试通用的租户上下文。"""
    return TenantContext(tenant_id="t-tenant", tenant_scopes=["workshop:PCBA", "line:SMT-1"])
