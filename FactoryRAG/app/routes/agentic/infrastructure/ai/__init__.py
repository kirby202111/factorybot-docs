"""E LangGraph 轻量路由图（``recursion_limit=6``）。

节点：``router``(透传) -> ``tool`` | ``delegate`` | ``converge`` -> END。
- ``tool``：``ToolExecutor`` 调 A/B 工具（TRACE_FACT/DOC_LOOKUP）；
- ``delegate``：``SubAgentDelegator`` 委托 L1/L2（ROOT_CAUSE/DRAFT_REQUEST）；
- ``converge``：汇聚结果。
E 不自己多步推理，``recursion_limit=6`` 硬上限靠框架兜底。LangGraph ≥0.2。
"""
from app.routes.agentic.infrastructure.ai.route_graph_builder import RouteGraphBuilder

__all__ = ["RouteGraphBuilder"]
