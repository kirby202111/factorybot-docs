"""A seed 解析器：NL -> 图 seed。

三段策略：① 正则匹配 SN/WO/BATCH -> 直接返回 Seed；② bge-m3 缺陷语义匹配
（DefectCatalog.name_embedding 向量近邻，score > 0.75）；③ LLM 结构化输出兜底。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.routes.traceability.domain.seed import Seed, SeedKind
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)

SN_PATTERN = re.compile(r"\bSN[-_]?[A-Z0-9]+(?:[-_][A-Z0-9]+)*", re.IGNORECASE)
WO_PATTERN = re.compile(r"\bWO[-_]?[A-Z0-9]+(?:[-_][A-Z0-9]+)*", re.IGNORECASE)
BATCH_PATTERN = re.compile(r"\b[Bb][-_]?[0-9A-Z]{4,}(?:[-_][0-9A-Z]+)*")


class SeedResolver:
    """NL -> 图 seed。regex-first -> bge-m3 缺陷匹配 -> LLM 兜底。"""

    def __init__(self, *, llm: Any, embedder: Any, driver: Any) -> None:
        self._llm = llm
        self._embedder = embedder
        self._driver = driver

    async def resolve(self, question: str, tenant: TenantContext) -> Seed:
        # ① 正则优先
        seed = self._regex_match(question)
        if seed is not None:
            return seed
        # ② 缺陷语义匹配
        seed = await self._match_defect(question)
        if seed is not None:
            return seed
        # ③ LLM 兜底
        return await self._llm_fallback(question)

    def _regex_match(self, question: str) -> Seed | None:
        if m := SN_PATTERN.search(question):
            return Seed(kind=SeedKind.WIP_UNIT, value=m.group(0))
        if m := WO_PATTERN.search(question):
            return Seed(kind=SeedKind.WORK_ORDER, value=m.group(0))
        if m := BATCH_PATTERN.search(question):
            return Seed(kind=SeedKind.INVENTORY_BATCH, value=m.group(0))
        return None

    async def _match_defect(self, question: str) -> Seed | None:
        """bge-m3 向量近邻查 DefectCatalog.name_embedding，score > 0.75 命中。"""
        try:
            vec = await self._embedder.embed_one(question)
            async with self._driver.session() as session:
                result = await session.run(
                    """
                    CALL db.index.vector.queryNodes('defect_name_idx', 1, $vec)
                    YIELD node, score
                    WHERE score > 0.75
                    RETURN node.defect_code AS defect_code, score
                    """,
                    vec=vec,
                )
                records = await result.data()
            if records:
                return Seed(kind=SeedKind.DEFECT, value=records[0]["defect_code"])
        except Exception as exc:
            logger.warning("缺陷语义匹配失败，降级 LLM 兜底: %s", exc)
        return None

    async def _llm_fallback(self, question: str) -> Seed:
        """LLM 结构化输出 Seed（兜底）。失败返回 DEFECT 空值 seed。"""
        prompt = [
            {"role": "system", "content": "从问题中抽取追溯种子。输出 JSON: {kind, value}。"},
            {"role": "user", "content": f"问题：{question}\nkind 取 WipUnit/WorkOrder/InventoryBatch/Defect。"},
        ]
        try:
            import json

            result = await self._llm.achat(prompt)
            data = json.loads(result.content)
            return Seed(kind=SeedKind(data.get("kind", "Defect")), value=data.get("value", ""))
        except Exception as exc:
            logger.warning("LLM seed 兜底失败: %s", exc)
            return Seed(kind=SeedKind.DEFECT, value="")
