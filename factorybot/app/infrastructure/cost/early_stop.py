"""EarlyStopDetector：冗余检测 + 模型自评 enough_evidence -> 触发转人工。

避免 ReAct 在证据已充分后继续无效调工具（省钱 + 防过拟合式循环）。
"""
from __future__ import annotations

from typing import Optional


class EarlyStopDetector:
    def __init__(self, max_tool_calls: int = 8, min_evidence: int = 2) -> None:
        self._max_tool_calls = max_tool_calls
        self._min_evidence = min_evidence

    def should_stop(self, tool_call_count: int, evidence_count: int,
                    model_self_assess: Optional[bool] = None) -> tuple[bool, str]:
        """返回 (是否停止, 原因)。"""
        if tool_call_count >= self._max_tool_calls:
            return True, f"已达工具调用上限 {self._max_tool_calls}"
        if model_self_assess is True and evidence_count >= self._min_evidence:
            return True, "模型自评证据充分 (enough_evidence)"
        if evidence_count >= self._min_evidence * 2:
            return True, "证据冗余，足够转人工"
        return False, ""
