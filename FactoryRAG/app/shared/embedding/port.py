"""Embedding 抽象接口（DIP）。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingPort(Protocol):
    """向量化抽象。

    A（DefectCatalog 缺陷描述语义入口）/ B（文档 chunk 主体）共用。
    provider 无关、可插拔：默认百炼 ``text-embedding-v4`` 1024 维，
    也可切 bge-m3 本地 sidecar（车间网隔离场景）。cosine 相似度。
    """

    DIM: int

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回 1024 维向量列表。"""
        ...

    async def embed_one(self, text: str) -> list[float]:
        """单条向量化（embed_batch 的便捷封装）。"""
        ...


@runtime_checkable
class RerankerPort(Protocol):
    """精排抽象（B 用）。

    provider 无关、可插拔：默认百炼 ``gte-rerank-v2``，也可切 bge-reranker-v2-m3
    本地 sidecar。``rerank`` 返回 (原索引, 分数) 列表，下游按索引取回 chunk。
    """

    async def rerank(
        self, query: str, docs: list[str], top_k: int
    ) -> list[tuple[int, float]]:
        """对 docs 按 query 相关性精排，返回 (原索引, 分数) 列表，截断 top_k。"""
        ...
