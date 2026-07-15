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
    assert ctx.role == "VIEWER"        # 缺失回退最低权限 VIEWER（#34，非 ENGINEER）


def test_real_mode_scopes_from_config_and_unconfigured_denied():
    """#34 方案 B：scopes 来自配置表，未配置租户 fail-closed 拒绝写。"""
    scopes = {"WS-B": ["pass:read", "rework:write"]}
    # 配置表内的租户拿到对应 scopes
    ctx = resolve_tenant_context(
        tenant_id="WS-B", workshop=None, line=None, role=None, user_id="u_li",
        is_mock=False, default=_default(), tenant_scopes=scopes,
    )
    assert ctx.scopes == ["pass:read", "rework:write"]
    # 未配置的租户 fail-closed -> scopes=[] 拒绝写
    ctx2 = resolve_tenant_context(
        tenant_id="WS-C", workshop=None, line=None, role=None, user_id="u_li",
        is_mock=False, default=_default(), tenant_scopes=scopes,
    )
    assert ctx2.scopes == []
    # 不传 tenant_scopes（None）同样 fail-closed，不再回退 WS-A 全量
    ctx3 = resolve_tenant_context(
        tenant_id="WS-B", workshop=None, line=None, role=None, user_id="u_li",
        is_mock=False, default=_default(),
    )
    assert ctx3.scopes == []
