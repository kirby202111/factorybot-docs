"""编排 换线端到端：start -> 多次 confirm -> DONE。

默认 fixtures 为 tooling FAIL 路径（ASSET-01 钢网 ST-A != RR-B 期望 ST-B），
故走 root_cause(A) -> gate_disposition 分支。需确认 3 个 gate：
FIRST_ARTICLE -> PROCESS_SWITCH -> DISPOSITION -> DONE。
"""
import asyncio

import pytest

from app.container import get_container
from app.domain.errors import ResourceAccessError
from app.domain.orchestration_state import OrchestrationSession, ScenarioType, SessionStatus
from app.domain.tenant import TenantContext


async def _wait_pending(orch, session_id, tenant, timeout=5.0):
    """轮询直到出现 pending gate 或会话结束。返回 (pending_step, session)。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        session = await orch.get_session(session_id, tenant)
        if session is None:
            return None, None
        if session.status in (SessionStatus.DONE, SessionStatus.FAILED):
            return None, session
        pending = await orch.pending_step(session_id)
        if pending:
            return pending, session
        await asyncio.sleep(0.05)
    return None, await orch.get_session(session_id, tenant)


@pytest.mark.asyncio
async def test_orchestration_changeover_full_flow():
    c = get_container()
    orch = c.orchestration_service
    tenant = c.default_tenant()
    session = await orch.start(
        "changeover", tenant,
        work_order_id="WO-2026-0701", asset_id="ASSET-01",
        target_route_id="RR-B", target_route_version="v4",
    )
    sid = session.session_id

    confirmed_steps: list[str] = []
    for _ in range(10):
        pending, sess = await _wait_pending(orch, sid, tenant)
        if sess is not None and sess.status == SessionStatus.DONE:
            break
        if sess is not None and sess.status == SessionStatus.FAILED:
            raise AssertionError(f"会话失败: {sess.suspend_reason}")
        assert pending is not None, f"无 pending gate 但未结束: status={sess.status if sess else None}"
        decision = await orch.resume(sid, pending, approved=True, tenant=tenant)
        confirmed_steps.append((pending, decision))
        await asyncio.sleep(0.05)

    final = await orch.get_session(sid, tenant)
    assert final.status == SessionStatus.DONE, f"未完成: {final.status}, 步骤={confirmed_steps}"
    # 确认经过了 DISPOSITION（root_cause 分支）
    steps = [s for s, _ in confirmed_steps]
    assert "DISPOSITION" in steps, f"未走 root_cause 分支: {steps}"


@pytest.mark.asyncio
async def test_orchestration_write_tool_gate_assertion():
    """启动断言：编排 写工具都声明 requires_confirmation + writes_via。"""
    c = get_container()
    c.orchestration_registry.validate_on_startup()  # 不抛异常即通过
    write_tools = [t for t in c.orchestration_registry.all() if not t.read_only]
    assert write_tools, "应有受限写工具"
    assert all(t.requires_confirmation and t.writes_via for t in write_tools)


@pytest.mark.asyncio
async def test_orchestration_tenant_isolation():
    """跨租户访问编排会话被拒：get_session / resume 抛 ResourceAccessError（路由层 -> 404）。

    直接在 service 层断言归属校验：resume 内先经 get_session 校验归属，跨租户时
    不会触达 graph，故无需驱动编排即可验证隔离不变量。
    """
    c = get_container()
    owner = c.default_tenant()
    other = TenantContext(
        tenant_id="WS-B", workshop="SMT-2", line="L-02",
        role="ENGINEER", user_id="u_li", scopes=owner.scopes,
    )
    session = OrchestrationSession(
        session_id="S-ISO-TEST", scenario=ScenarioType.CHANGEOVER,
        work_order_id="WO-ISO-1", tenant_context=owner.model_dump(),
        status=SessionStatus.RUNNING, current_step="FIRST_ARTICLE",
    )
    await c.orchestration_repo.create_session(session)

    # 跨租户读状态 -> 拒（隐藏存在性，统一 ResourceAccessError）
    with pytest.raises(ResourceAccessError):
        await c.orchestration_service.get_session(session.session_id, other)
    # 跨租户确认 gate -> 拒
    with pytest.raises(ResourceAccessError):
        await c.orchestration_service.resume(
            session.session_id, "FIRST_ARTICLE", approved=True, tenant=other)
    # 归属租户仍可读
    assert await c.orchestration_service.get_session(session.session_id, owner) is not None
