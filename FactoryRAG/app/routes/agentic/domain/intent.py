"""E 意图分类枚举。"""
from __future__ import annotations

from enum import Enum


class IntentCategory(str, Enum):
    """E 意图路由分类。

    - ``TRACE_FACT``：路由到 A 工具 ``query_traceability_graph``；
    - ``ROOT_CAUSE``：委托 agent-service L1 ``/agent/diagnose``；
    - ``DOC_LOOKUP``：路由到 B 工具 ``search_docs``；
    - ``DRAFT_REQUEST``：委托 agent-service L2 ``/agent/draft``；
    - ``UNKNOWN``：兜底转人工。
    """

    TRACE_FACT = "TRACE_FACT"
    ROOT_CAUSE = "ROOT_CAUSE"
    DOC_LOOKUP = "DOC_LOOKUP"
    DRAFT_REQUEST = "DRAFT_REQUEST"
    UNKNOWN = "UNKNOWN"

    def is_tool_route(self) -> bool:
        """是否路由到 A/B 工具（非委托）。"""
        return self in (IntentCategory.TRACE_FACT, IntentCategory.DOC_LOOKUP)

    def is_delegation(self) -> bool:
        """是否委托 L1/L2。"""
        return self in (IntentCategory.ROOT_CAUSE, IntentCategory.DRAFT_REQUEST)
