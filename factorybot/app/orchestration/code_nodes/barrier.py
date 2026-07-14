"""barrier 代码节点 + FailureTracker。

barrier：并行分支汇合，按结构化结果确定性分流（draft_release | root_cause | suspend）。
FailureTracker：按 session+capability 计 agent 连续失败，>=2 -> SUSPENDED。
"""
from __future__ import annotations

from app.infrastructure.persistence.repos import OrchestrationRepo


class FailureTracker:
    """agent 连续失败计数。>=2 次 -> SUSPENDED。成功则重置。"""

    def __init__(self, repo: OrchestrationRepo, threshold: int = 2) -> None:
        self._repo = repo
        self._threshold = threshold

    async def record_agent_result(self, session_id: str, capability: str, result: dict) -> bool:
        """返回 True=可继续，False=应挂起。"""
        if result.get("status") == "SUCCESS":
            await self._repo.reset_failure_count(session_id, capability)
            return True
        count = await self._repo.increment_failure_count(session_id, capability)
        if count >= self._threshold:
            await self._repo.update_status(
                session_id, "SUSPENDED", capability,
                f"agent {capability} 连续失败 {count} 次，已挂起",
            )
            return False
        return True


async def barrier_node(state: dict, repo: OrchestrationRepo, dispatcher=None) -> dict:
    """并行分支汇合：按 tooling/kitting 结构化结果确定性分流。"""
    t = state.get("tooling_result") or {}
    k = state.get("kitting_result") or {}
    if t.get("status") == "PASS" and k.get("status") == "PASS":
        return {"barrier_route": "draft_release", "current_step": "BARRIER"}
    if t.get("status") == "FAIL":
        return {
            "barrier_route": "root_cause",
            "expected": t.get("expected"),
            "actual": t.get("actual"),
            "mismatch_code": t.get("code"),
            "current_step": "BARRIER",
        }
    # kitting FAIL（且 tooling 非 FAIL）-> 挂起催料
    if dispatcher is not None:
        await dispatcher.push_exception_card(
            state.get("session_id", ""), "BARRIER", "物料齐套未达标，请催料",
        )
    await repo.update_status(state.get("session_id", ""), "SUSPENDED", "BARRIER", "物料齐套未达标")
    return {"barrier_route": "suspend", "status": "SUSPENDED", "current_step": "BARRIER"}


def barrier_route(state: dict) -> str:
    return state.get("barrier_route") or "draft_release"
