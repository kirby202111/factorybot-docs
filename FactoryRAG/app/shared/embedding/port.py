"""Embedding 抽象接口（DIP）。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingPort(Protocol):
    """向量化抽象。

    A（DefectCatalog 缺陷描述语义入口）/ B（文档 chunk 主体）共用。
    bge-m3，1024 维，cosine 相似度。
    """

    DIM: int

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回 1024 维向量列表。"""
        ...

    async def embed_one(self, text: str) -> list[float]:
        """单条向量化（embed_batch 的便捷封装）。"""
        ...
