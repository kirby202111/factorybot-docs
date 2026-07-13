"""E 子代理委托器（``delegate`` 节点）。

委托 agent-service L1/L2（httpx REST，透传 ``traceparent``，决策 #1）。
E 不自己多步推理，深度多步推理仍委托 L1。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.routes.agentic.domain.intent import IntentCategory
from app.shared.obs.port import ObservabilityPort

logger = logging.getLogger(__name__)


class SubAgentDelegator:
    """``delegate`` 节点：委托 L1/L2。"""

    def __init__(
        self,
        *,
        l1_client: Any,
        l2_client: Any,
        trace_repo: Any,
        obs: ObservabilityPort,
    ) -> None:
        self._l1 = l1_client
        self._l2 = l2_client
        self._trace_repo = trace_repo
        self._obs = obs

    async def __call__(self, state: dict) -> dict:
        intent: IntentCategory = state.get("intent")
        tenant = state.get("tenant")
        traceparent = state.get("traceparent", "")
        question = state.get("question", "")
        started = time.perf_counter()
        try:
            if intent == IntentCategory.ROOT_CAUSE:
                view = await self._l1.delegate(question=question, tenant=tenant, traceparent=traceparent)
                tool_chain = ["L1:diagnose"]
            elif intent == IntentCategory.DRAFT_REQUEST:
                view = await self._l2.delegate(
                    draft_kind="rework", context={"question": question}, tenant=tenant, traceparent=traceparent
                )
                tool_chain = ["L2:draft"]
            else:
                state["answer"] = "无可委托的子代理，建议转人工。"
                return state
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._trace_repo.save_ok(tool_chain[0], view, latency_ms)
            try:
                self._obs.metrics.agent_delegation_duration.labels(kind=tool_chain[0]).observe(latency_ms / 1000)
            except Exception:
                pass
            state["tool_result"] = view
            state["tool_chain"] = tool_chain
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._trace_repo.save_error("delegation", str(exc), latency_ms)
            state["answer"] = f"子代理委托超时/失败，已转人工：{exc}"
            state["tool_chain"] = ["delegation:failed"]
        return state
