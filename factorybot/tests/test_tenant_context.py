"""租户上下文解析：mock 回退 / real 强制身份 header（resolve_tenant_context 纯逻辑单测）。"""
import pytest
from fastapi import HTTPException

from app.api.deps import resolve_tenant_context
from app.domain.tenant import TenantContext


def _default() -> TenantContext:
    return TenantContext.default()


def test_mock_mode_falls_back_to_default_when_headers_absent():
    ctx = resolve_tenant_context(
        tenant_id=None, workshop=None, line=None, role=None, user_id=None,
        is_mock=True, default=_default(),
    )
    assert ctx.tenant_id == "WS-A"
    assert ctx.user_id == "u_zhang"


def test_mock_mode_uses_provided_headers():
    ctx = resolve_tenant_context(
        tenant_id="WS-B", workshop="SMT-2", line="L-02", role="LEAD",
        user_id="u_li", is_mock=True, default=_default(),
    )
    assert ctx.tenant_id == "WS-B" and ctx.user_id == "u_li" and ctx.role == "LEAD"


def test_real_mode_rejects_missing_identity_headers():
    with pytest.raises(HTTPException) as exc:
        resolve_tenant_context(
            tenant_id=None, workshop=None, line=None, role=None, user_id=None,
            is_mock=False, default=_default(),
        )
    assert exc.value.status_code == 400
    # 仅缺 user_id 同样拒绝
    with pytest.raises(HTTPException):
        resolve_tenant_context(
            tenant_id="WS-B", workshop=None, line=None, role=None, user_id=None,
            is_mock=False, default=_default(),
        )


def test_real_mode_uses_headers_and_blanks_optional():
    ctx = resolve_tenant_context(
        tenant_id="WS-B", workshop=None, line=None, role=None, user_id="u_li",
        is_mock=False, default=_default(),
    )
    assert ctx.tenant_id == "WS-B" and ctx.user_id == "u_li"
    assert ctx.workshop == ""          # 不泄漏 mock 默认 SMT-1
    assert ctx.role == "ENGINEER"      # 缺失回退 ENGINEER
