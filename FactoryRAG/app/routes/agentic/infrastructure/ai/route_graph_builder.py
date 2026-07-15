"""E LangGraph ``StateGraph`` 构建器。"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from app.routes.agentic.domain.intent import IntentCategory

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    """路由图状态。

    必须声明所有需在节点间透传的键：LangGraph ``StateGraph`` 按本 TypedDict 过滤状态，
    未声明的键会被丢弃。``audit_id``/``trace_id`` 若漏声明，``ToolExecutor``/``Delegator``
    读到的 ``audit_id`` 为空，``route_trace`` 行挂不上 ``answer_audit``，``/agent/explain/{audit_id}``
    证据链断裂（与 ``traceparent``/``session_id`` 同理须在此声明）。
    """

    question: str
    intent: IntentCategory
    tenant: Any
    session_id: str
    trace_id: str
    traceparent: str
    audit_id: str
    tool_result: Any
    tool_chain: list[str]
    answer: str


class RouteGraphBuilder:
    """构建 E 轻量路由图。

    节点：router -> {tool | delegate | converge} -> converge -> END。
    LangGraph 不可用时降级为简单顺序执行器（保持可跑）。
    """

    def __init__(self, *, tool_executor: Any, delegator: Any) -> None:
        self._tool_executor = tool_executor
        self._delegator = delegator

    def build(self, intent: IntentCategory, tenant: Any) -> Any:
        try:
            from langgraph.graph import END, StateGraph

            graph = StateGraph(AgentState)
            graph.add_node("router", self._router_node)
            graph.add_node("tool", self._tool_executor)
            graph.add_node("delegate", self._delegator)
            graph.add_node("converge", self._converge_node)
            graph.set_entry_point("router")
            graph.add_conditional_edges(
                "router",
                lambda s: self._route_decision(s.get("intent")),
                {"tool": "tool", "delegate": "delegate", "unknown": "converge"},
            )
            graph.add_edge("tool", "converge")
            graph.add_edge("delegate", "converge")
            graph.add_edge("converge", END)
            return graph.compile()
        except Exception as exc:  # langgraph 不可用 -> 降级
            logger.warning("LangGraph 不可用，RouteGraphBuilder 降级为顺序执行: %s", exc)
            return _FallbackGraph(self, intent)

    @staticmethod
    def _route_decision(intent: IntentCategory | None) -> str:
        if intent is None:
            return "unknown"
        if intent.is_delegation():
            return "delegate"
        if intent == IntentCategory.UNKNOWN:
            return "unknown"
        return "tool"

    async def _router_node(self, state: AgentState) -> AgentState:
        # 透传：意图已在 GatewayService 分类完成
        return state

    async def _converge_node(self, state: AgentState) -> AgentState:
        if not state.get("answer") and state.get("tool_result") is None:
            state["answer"] = "未产生结果，建议转人工。"
        return state


class _FallbackGraph:
    """LangGraph 不可用时的顺序执行降级图。"""

    def __init__(self, builder: RouteGraphBuilder, intent: IntentCategory) -> None:
        self._builder = builder
        self._intent = intent

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        """顺序执行降级：router 决策 -> 单一 tool/delegate -> converge。

        ``config`` 仅为与 LangGraph 编译图的 ``ainvoke(state, config)`` 签名对齐而保留，
        使调用方（``GatewayService``）无需区分真实图 / 降级图即可统一传参。

        降级路径为单趟定长执行（router -> 单分支 -> converge），无递归 / 循环，
        故 ``config["recursion_limit"]`` 在此不生效（定长即天然有界）；
        ``configurable``（如 ``thread_id`` 记忆检查点）亦忽略——降级仅保证"可跑"，
        真正的执行时限由调用方外层 ``asyncio.wait_for(timeout=70)`` 兜底。
        """
        decision = RouteGraphBuilder._route_decision(self._intent)
        if decision == "tool":
            state = await self._builder._tool_executor(state)
        elif decision == "delegate":
            state = await self._builder._delegator(state)
        state = await self._builder._converge_node(state)
        return state
