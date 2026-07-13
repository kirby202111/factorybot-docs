"""阿里云百炼（DashScope）embedding + rerank client。

- ``text-embedding-v4``：1024 维（默认），走百炼 OpenAI 兼容端点
- ``gte-rerank-v2``：cross-encoder 精排，走百炼原生 rerank 端点

与 ``BgeClient``/``BgeReranker`` 同实现 ``EmbeddingPort``/``RerankerPort``，
经 ``embedding_factory`` 按 ``settings.embedding.provider`` 选择，provider 可插拔。
无新依赖：复用 ``httpx``。车间网隔离场景若不能出网，回退 provider=bge 走本地 sidecar。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.shared.config.base import EmbeddingSettings
from app.shared.embedding.port import EmbeddingPort, RerankerPort

logger = logging.getLogger(__name__)

# 百炼 rerank 原生端点（与 embedding 的 OpenAI 兼容端点不同路径）。
_BAILIAN_RERANK_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
# text-embedding-v4 单次输入上限（百炼约束），超出分批。
_BAILIAN_EMBED_BATCH_LIMIT = 10


class BailianEmbeddingClient(EmbeddingPort):
    """百炼 ``text-embedding-v4`` 1024 维批量向量化，A/B 共用。

    走百炼 OpenAI 兼容模式：``POST {base_url}/embeddings``，
    ``Authorization: Bearer {api_key}``。``base_url`` 默认百炼兼容端点。
    """

    DIM = 1024

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings
        self._base_url = (settings.base_url or "").rstrip("/")
        self._model = settings.model or "text-embedding-v4"
        self._dim = settings.dim or self.DIM
        self._batch_size = settings.batch_size
        self._api_key = settings.api_key
        self._http = httpx.AsyncClient(timeout=60.0)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        # 百炼单次输入有上限，取 min 防超限；同时尊重配置 batch_size。
        step = max(1, min(self._batch_size, _BAILIAN_EMBED_BATCH_LIMIT))
        for i in range(0, len(texts), step):
            chunk = texts[i : i + step]
            out.extend(await self._embed_chunk(chunk))
        return out

    async def embed_one(self, text: str) -> list[float]:
        vecs = await self.embed_batch([text])
        return vecs[0]

    async def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        resp = await self._http.post(
            f"{self._base_url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "input": texts,
                "dimensions": self._dim,
                "encoding_format": "float",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # OpenAI 兼容响应：data[*].embedding，按 index 顺序还原。
        items = sorted(data["data"], key=lambda x: x.get("index", 0))
        return [list(map(float, item["embedding"])) for item in items]

    async def close(self) -> None:
        await self._http.aclose()


class BailianReranker(RerankerPort):
    """百炼 ``gte-rerank-v2`` cross-encoder 精排（B 用）。

    走百炼原生 rerank 端点，返回每个 doc 的相关性分数；
    ``rerank`` 签名与 ``BgeReranker.rerank`` 一致，下游 ``DocumentRetrievalService`` 零改动。
    """

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings
        self._model = settings.reranker_model or "gte-rerank-v2"
        self._api_key = settings.api_key
        self._http = httpx.AsyncClient(timeout=60.0)

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        """对 docs 按 query 相关性精排，返回 (原索引, 分数) 列表，截断 top_k。"""
        if not docs:
            return []
        resp = await self._http.post(
            _BAILIAN_RERANK_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "input": {"query": query, "documents": docs},
                "parameters": {"return_documents": False, "top_n": top_k},
            },
        )
        resp.raise_for_status()
        # 原生响应：output.results[*]（已按 relevance_score 降序，含原 documents 的 index）。
        results = resp.json().get("output", {}).get("results", [])
        ranked = [(int(r["index"]), float(r["relevance_score"])) for r in results]
        # 百炼已按分降序返回并截断 top_n；这里再兜底截断 top_k。
        return ranked[:top_k]

    async def close(self) -> None:
        await self._http.aclose()
