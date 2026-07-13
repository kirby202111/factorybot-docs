"""ChromaDB chunk 仓库。

chunk 不可变：upsert 按 chunk_id 覆盖（内容不变得出相同结果，幂等）；
单条软删（文档撤回）允许 upsert 改 state=DEPRECATED（单条原子，ChromaDB 可接受），
区别于"批量翻转"（不做）。
"""
from __future__ import annotations

import logging
from typing import Any

from app.routes.document.domain.chunk import DocumentChunk

logger = logging.getLogger(__name__)


class ChunkRepo:
    """ChromaDB chunk 操作。"""

    def __init__(self, collection: Any) -> None:
        self._collection = collection

    async def upsert_chunks(self, chunks: list[DocumentChunk]) -> None:
        """批量 upsert（chunk 不可变，写入后 metadata 不再修改）。"""
        if not chunks:
            return
        self._collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=[c.embedding for c in chunks],
            documents=[c.text for c in chunks],
            metadatas=[c.to_metadata_dict() for c in chunks],
        )

    async def soft_delete(self, chunk_id: str) -> None:
        """单条软删：state=DEPRECATED（单条原子，区别于批量翻转）。"""
        self._collection.update(ids=[chunk_id], metadatas=[{"state": "DEPRECATED"}])

    async def delete_by_version(self, version_id: str) -> None:
        """按 version 删除（仅 ChromaDB 重建时使用）。"""
        self._collection.delete(where={"version_id": version_id})

    async def count(self) -> int:
        return self._collection.count()
