"""E 意图路由器：NL -> IntentCategory。规则优先，LLM 兜底。"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.routes.agentic.domain.intent import IntentCategory

logger = logging.getLogger(__name__)

# 规则：关键词命中即路由（避免每问都调 LLM）。
_RULES: list[tuple[list[str], IntentCategory]] = [
    (["根因", "5M1E", "为什么不良", "不良原因"], IntentCategory.ROOT_CAUSE),
    (["怎么处置", "SOP", "怎么修", "流程", "操作步骤"], IntentCategory.DOC_LOOKUP),
    (["草拟", "生成返工单", "8D", "起草"], IntentCategory.DRAFT_REQUEST),
    (["过了哪几站", "用了哪批", "位置", "追溯", "哪一站"], IntentCategory.TRACE_FACT),
]


class IntentRouter:
    """NL -> IntentCategory。规则优先 -> LLM 结构化输出兜底 -> UNKNOWN。"""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def classify(self, question: str) -> IntentCategory:
        # ① 规则优先
        for keywords, intent in _RULES:
            if any(kw in question for kw in keywords):
                return intent
        # ② LLM 兜底
        try:
            prompt = [
                {
                    "role": "system",
                    "content": (
                        "把车间问题分类为意图。取值：TRACE_FACT/ROOT_CAUSE/DOC_LOOKUP/DRAFT_REQUEST/UNKNOWN。"
                        "输出 JSON: {intent}。"
                    ),
                },
                {"role": "user", "content": f"问题：{question}"},
            ]
            result = await self._llm.achat(prompt)
            data = json.loads(result.content)
            return IntentCategory(data.get("intent", "UNKNOWN"))
        except Exception as exc:
            logger.warning("意图 LLM 兜底失败，返回 UNKNOWN: %s", exc)
            return IntentCategory.UNKNOWN
