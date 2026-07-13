"""E 工具执行器（``tool`` 节点）。

调 A/B 工具（经 ToolRegistry handler -> InProcess Port，决策 #4）。
权限校验 + trace 记录。E 不自己多步推理。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.routes.agentic.domain.intent import IntentCategory
from app.shared.obs.port import ObservabilityPort

logger = logging.getLogger(__name__)


class ToolExecutor:
    """``tool`` 节点：调 A/B 只读工具。"""

    INTENT_TOOL = {
        IntentCategory.TRACE_FACT: "query_traceability_graph",
        IntentCategory.DOC_LOOKUP: "search_docs",
    }

    def __init__(self, *, registry: Any, trace_repo: Any, obs: ObservabilityPort) -> None:
        self._registry = registry
        self._trace_repo = trace_repo
        self._obs = obs

    async def __call__(self, state: dict) -> dict:
        intent: IntentCategory = state.get("intent")
        tool_name = self.INTENT_TOOL.get(intent)
        if tool_name is None:
            state["answer"] = "无可调用的工具，建议转人工。"
            return state
        descriptor = self._registry.get(tool_name)
        if descriptor is None:
            state["answer"] = f"工具 {tool_name} 未注册。"
            return state

        tenant = state.get("tenant")
        # 权限校验
        if not tenant.can_access(descriptor.required_tenant_scopes):
            await self._trace_repo.save_denied(tool_name, "tenant scope 不足")
            state["answer"] = "权限不足，建议转人工。"
            state["tool_chain"] = [tool_name]
            return state

        started = time.perf_counter()
        try:
            view = await self._invoke(descriptor, intent, state)
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._trace_repo.save_ok(tool_name, view, latency_ms)
            state["tool_result"] = view
            state["tool_chain"] = [tool_name]
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._trace_repo.save_error(tool_name, str(exc), latency_ms)
            state["answer"] = f"工具 {tool_name} 执行失败：{exc}"
            state["tool_chain"] = [tool_name]
        return state

    async def _invoke(self, descriptor: Any, intent: IntentCategory, state: dict) -> Any:
        """按意图构造参数调 handler。"""
        question = state.get("question", "")
        tenant = state.get("tenant")
        if intent == IntentCategory.TRACE_FACT:
            from app.routes.traceability.domain.seed import SeedKind

            # 简化：用问题文本作为 seed value（生产由 SeedResolver 解析）
            return await descriptor.handler(
                seed_value=question, seed_kind=SeedKind.WIP_UNIT, tenant=tenant
            )
        if intent == IntentCategory.DOC_LOOKUP:
            return await descriptor.handler(
                query=question, route_version="", tenant=tenant
            )
        return None
