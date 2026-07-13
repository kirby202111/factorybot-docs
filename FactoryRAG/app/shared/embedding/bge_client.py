"""bge-m3 1024 维批量推理 + bge-reranker-v2-m3 cross-encoder 精排。

走 bge-inference sidecar（车间网隔离必备）；本地 sentence-transformers 作为兜底。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.shared.config.base import EmbeddingSettings
from app.shared.embedding.port import EmbeddingPort

logger = logging.getLogger(__name__)


class BgeClient(EmbeddingPort):
    """bge-m3 1024 维批量推理，A/B 共用。

    优先走 bge-inference sidecar（HTTP）；若 sidecar 不可达且配置允许，
    回退到本地 sentence-transformers + FlagEmbedding 推理（车间网隔离场景）。
    """

    DIM = 1024

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings
        self._base_url = settings.base_url
        self._model = settings.model
        self._dim = settings.dim or self.DIM
        self._batch_size = settings.batch_size
        self._http = httpx.AsyncClient(timeout=60.0)
        self._local_embedder: Any = None  # 懒加载本地兜底

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            chunk = texts[i : i + self._batch_size]
            out.extend(await self._embed_chunk(chunk))
        return out

    async def embed_one(self, text: str) -> list[float]:
        vecs = await self.embed_batch([text])
        return vecs[0]

    async def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        try:
            return await self._embed_via_sidecar(texts)
        except Exception as exc:  # sidecar 不可达 -> 本地兜底
            logger.warning("bge sidecar 不可达，回退本地推理: %s", exc)
            return await self._embed_via_local(texts)

    async def _embed_via_sidecar(self, texts: list[str]) -> list[list[float]]:
        # infinity / TEI 风格的 embeddings 接口。
        resp = await self._http.post(
            f"{self._base_url}/embeddings",
            json={"model": self._model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]

    async def _embed_via_local(self, texts: list[str]) -> list[list[float]]:
        if self._local_embedder is None:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._local_embedder = SentenceTransformer(self._model)
        # sentence-transformers 同步接口，放线程池执行避免阻塞事件循环。
        import asyncio

        loop = asyncio.get_running_loop()
        vecs = await loop.run_in_executor(None, self._local_embedder.encode, texts)
        return [list(map(float, v)) for v in vecs]

    async def close(self) -> None:
        await self._http.aclose()


class BgeReranker:
    """bge-reranker-v2-m3 cross-encoder 精排（B 用）。

    使用 ``FlagReranker(use_fp16=True)``；优先 sidecar，否则本地推理。
    """

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings
        self._reranker_model = settings.reranker_model
        self._local_reranker: Any = None

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        """对 docs 按 query 相关性精排，返回 (原索引, 分数) 列表，截断 top_k。"""
        if not docs:
            return []
        pairs = [[query, doc] for doc in docs]
        scores = await self._score(pairs)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    async def _score(self, pairs: list[list[str]]) -> list[float]:
        try:
            return await self._score_via_sidecar(pairs)
        except Exception:
            return await self._score_via_local(pairs)

    async def _score_via_sidecar(self, pairs: list[list[str]]) -> list[float]:
        # rerank 接口约定：返回每个 pair 的相关性分数。
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(
                f"{self._settings.base_url}/rerank",
                json={"model": self._reranker_model, "pairs": pairs},
            )
            resp.raise_for_status()
            return [float(s) for s in resp.json()["scores"]]

    async def _score_via_local(self, pairs: list[list[str]]) -> list[float]:
        if self._local_reranker is None:
            from FlagEmbedding import FlagReranker  # type: ignore

            self._local_reranker = FlagReranker(self._reranker_model, use_fp16=True)
        import asyncio

        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(None, self._local_reranker.compute_score, pairs)
        if isinstance(scores, float):
            scores = [scores]
        return [float(s) for s in scores]
