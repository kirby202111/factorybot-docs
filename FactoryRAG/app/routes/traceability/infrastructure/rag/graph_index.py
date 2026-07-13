"""A LlamaIndex PropertyGraphIndex 封装（可选上层）。

MVP 走裸 Cypher（GraphRetriever）；本封装为后续 LlamaIndex 集成预留。
A 的检索编排用 LlamaIndex 0.10+ ``PropertyGraphIndex``；LLM 抽象归 shared。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class GraphIndex:
    """LlamaIndex ``PropertyGraphIndex`` 封装（预留，MVP 不启用）。"""

    def __init__(self, *, driver: Any, llm: Any, embedder: Any) -> None:
        self._driver = driver
        self._llm = llm
        self._embedder = embedder
        self._index: Any = None

    async def build(self) -> Any:
        """构造 PropertyGraphIndex（懒加载，MVP 未启用）。"""
        try:
            from llama_index.core import PropertyGraphIndex  # type: ignore
            from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore  # type: ignore
        except Exception as exc:  # pragma: no cover
            logger.info("LlamaIndex 不可用，GraphIndex 走裸 Cypher 降级: %s", exc)
            return None
        # 实际构造省略；MVP 用 GraphRetriever 的裸 Cypher。
        return self._index
