"""L3 换线端到端：start -> 多次 confirm -> DONE。

默认 fixtures 为 tooling FAIL 路径（ASSET-01 钢网 ST-A != RR-B 期望 ST-B），
故走 root_cause(A) -> gate_disposition 分支。需确认 3 个 gate：
FIRST_ARTICLE -> PROCESS_SWITCH -> DISPOSITION -> DONE。
"""
import asyncio

import pytest

from app.container import get_container
from app.domain.l3_state import SessionStatus


async def _wait_pending(orch, session_id, timeout=5.0):
    """轮询直到出现 pending gate 或会话结束。返回 (pending_step, session)。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        session = await orch.get_session(session_id)
        if session is None:
            return None, None
        if session.status in (SessionStatus.DONE, SessionStatus.FAILED):
            return None, session
        pending = await orch.pending_step(session_id)
        if pending:
            return pending, session
        await asyncio.sleep(0.05)
    return None, await orch.get_session(session_id)


@pytest.mark.asyncio
async def test_l3_changeover_full_flow():
    c = get_container()
    orch = c.l3_orchestrator
    tenant = c.default_tenant()
    session = await orch.start(
        "changeover", tenant,
        work_order_id="WO-2026-0701", asset_id="ASSET-01",
        target_route_id="RR-B", target_route_version="v4",
    )
    sid = session.session_id

    confirmed_steps: list[str] = []
    for _ in range(10):
        pending, sess = await _wait_pending(orch, sid)
        if sess is not None and sess.status == SessionStatus.DONE:
            break
        if sess is not None and sess.status == SessionStatus.FAILED:
            raise AssertionError(f"会话失败: {sess.suspend_reason}")
        assert pending is not None, f"无 pending gate 但未结束: status={sess.status if sess else None}"
        decision = await orch.resume(sid, pending, approved=True, user_id="u_zhang")
        confirmed_steps.append((pending, decision))
        await asyncio.sleep(0.05)

    final = await orch.get_session(sid)
    assert final.status == SessionStatus.DONE, f"未完成: {final.status}, 步骤={confirmed_steps}"
    # 确认经过了 DISPOSITION（root_cause 分支）
    steps = [s for s, _ in confirmed_steps]
    assert "DISPOSITION" in steps, f"未走 root_cause 分支: {steps}"


@pytest.mark.asyncio
async def test_l3_write_tool_gate_assertion():
    """启动断言：L3 写工具都声明 requires_confirmation + writes_via。"""
    c = get_container()
    c.l3_registry.validate_on_startup()  # 不抛异常即通过
    write_tools = [t for t in c.l3_registry.all() if not t.read_only]
    assert write_tools, "应有受限写工具"
    assert all(t.requires_confirmation and t.writes_via for t in write_tools)
