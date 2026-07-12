"""plan 代码节点：不调 LLM，按 scenario 标记会话 RUNNING。"""
from __future__ import annotations


class PlanNode:
    async def plan(self, state: dict) -> dict:
        return {"status": "RUNNING", "current_step": "PLAN"}

    async def done(self, state: dict) -> dict:
        return {"status": "DONE", "current_step": "DONE"}
