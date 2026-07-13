"""E 网关服务：收口 A/B + 委托 L1/L2。

编排：缓存 -> 意图路由 -> LangGraph 路由图（``recursion_limit=6``）-> 汇聚 -> 审计 + 缓存。
- E 不自己多步推理，轻量组合；深度多步推理委托 L1（决策 #4）。
- traceparent 全链路（决策 #1）：E 生成 trace_id，委托 L1/L2 时透传。
- 低置信/超时/递归越界 -> 转人工（宁可拦下让人判，不可错放）。
"""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.routes.agentic.domain.answer import AgentAnswer, AnswerSource, ChatRequest
from app.routes.agentic.domain.intent import IntentCategory
from app.shared.obs.port import ObservabilityPort
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class GatewayService:
    """E 统一问答入口 application service。"""

    def __init__(
        self,
        *,
        graph_builder: any,
        intent_router: any,
        cache: any,
        audit_repo: any,
        obs: ObservabilityPort,
    ) -> None:
        self._graph_builder = graph_builder
        self._intent_router = intent_router
        self._cache = cache
        self._audit_repo = audit_repo
        self._obs = obs
        self.tool_registry = None  # 供 ReadOnlyToolGate 扫描（build_gateway_service 注入）

    async def chat(self, request: ChatRequest, tenant: TenantContext) -> AgentAnswer:
        # 1. 缓存
        cached = await self._cache.get(request, tenant)
        if cached is not None:
            return cached

        # 2. 意图路由
        intent = await self._intent_router.classify(request.question)
        try:
            self._obs.metrics.agent_route.labels(intent=intent.value).inc()
        except Exception:
            pass

        # 3. trace_id + traceparent（决策 #1）
        trace_id = uuid4().hex
        traceparent = self._build_traceparent(trace_id)

        # 4. LangGraph 路由图（recursion_limit=6 硬上限靠框架兜底）
        answer = await self._run_graph(request, intent, tenant, trace_id, traceparent)

        # 5. 审计 + 缓存
        await self._audit_repo.record(request, intent, answer, tenant)
        await self._cache.set(request, tenant, answer)
        return answer

    async def _run_graph(
        self,
        request: ChatRequest,
        intent: IntentCategory,
        tenant: TenantContext,
        trace_id: str,
        traceparent: str,
    ) -> AgentAnswer:
        graph = self._graph_builder.build(intent, tenant)
        initial = {
            "question": request.question,
            "intent": intent,
            "tenant": tenant,
            "session_id": request.session_id or trace_id,
            "traceparent": traceparent,
            "tool_result": None,
            "tool_chain": [],
            "answer": None,
        }
        try:
            final_state = await asyncio.wait_for(
                graph.ainvoke(
                    initial,
                    config={"recursion_limit": 6, "configurable": {"thread_id": request.session_id or trace_id}},
                ),
                timeout=70.0,
            )
            return self._build_answer(request, intent, final_state, trace_id)
        except asyncio.TimeoutError:
            logger.warning("E 路由图超时，转人工: %s", request.question)
            return self._human_fallback(request, intent, trace_id, "路由图超时")
        except Exception as exc:  # GraphRecursionError 等
            logger.warning("E 路由图异常，转人工: %s", exc)
            return self._human_fallback(request, intent, trace_id, str(exc))

    def _build_answer(
        self, request: ChatRequest, intent: IntentCategory, state: dict, trace_id: str
    ) -> AgentAnswer:
        tool_chain = state.get("tool_chain") or []
        tool_result = state.get("tool_result")
        route_taken = self._route_taken(intent, tool_chain)
        summary, sources, confidence = self._materialize(tool_result, intent)
        return AgentAnswer(
            question=request.question,
            intent=intent.value,
            route_taken=route_taken,
            summary=summary,
            detail={"tool_result": self._serialize(tool_result)},
            sources=sources,
            confidence=confidence,
            tool_chain=tool_chain,
            trace_id=trace_id,
            needs_human_review=confidence < 0.6 or intent == IntentCategory.UNKNOWN,
        )

    @staticmethod
    def _route_taken(intent: IntentCategory, tool_chain: list[str]) -> str:
        if intent == IntentCategory.UNKNOWN:
            return "HUMAN"
        if intent.is_delegation():
            return "L1" if intent == IntentCategory.ROOT_CAUSE else "L2"
        if intent == IntentCategory.TRACE_FACT:
            return "A"
        if intent == IntentCategory.DOC_LOOKUP:
            return "B"
        return "HUMAN"

    def _materialize(self, tool_result: any, intent: IntentCategory) -> tuple[str, list[AnswerSource], float]:
        """把 tool_result 物化为 (summary, sources, confidence)。"""
        if tool_result is None:
            return "未能获取结果，建议转人工。", [], 0.0
        # tool_result 可能是 TraceSubgraph / list[ChunkHit] / L1/L2 视图（dict）
        summary = getattr(tool_result, "summary", None) or str(tool_result)[:200]
        confidence = 0.75
        sources: list[AnswerSource] = []
        if intent == IntentCategory.TRACE_FACT:
            subgraph_ref = getattr(tool_result, "subgraph_ref", None)
            if subgraph_ref:
                sources.append(AnswerSource(source_type="trace_subgraph", ref=subgraph_ref, route="A"))
        elif intent == IntentCategory.DOC_LOOKUP:
            for hit in tool_result or []:
                sources.append(
                    AnswerSource(source_type="sop_doc", ref=hit.chunk_id, route="B")
                )
        return summary, sources, confidence

    @staticmethod
    def _serialize(obj: any) -> any:
        if obj is None:
            return None
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if isinstance(obj, list):
            return [getattr(i, "model_dump", lambda: i)() if hasattr(i, "model_dump") else i for i in obj]
        return str(obj)

    def _human_fallback(
        self, request: ChatRequest, intent: IntentCategory, trace_id: str, reason: str
    ) -> AgentAnswer:
        return AgentAnswer(
            question=request.question,
            intent=intent.value,
            route_taken="HUMAN",
            summary=f"自动路由未完成（{reason}），已转人工。",
            detail={"reason": reason},
            sources=[],
            confidence=0.0,
            tool_chain=[],
            trace_id=trace_id,
            needs_human_review=True,
        )

    @staticmethod
    def _build_traceparent(trace_id: str) -> str:
        """W3C traceparent：00-<trace_id 32hex>-<span_id 16hex>-01。"""
        span_id = uuid4().hex[:16]
        return f"00-{trace_id}-{span_id}-01"
