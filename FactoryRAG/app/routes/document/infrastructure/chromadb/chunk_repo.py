"""ChromaDB chunk 仓库。

chunk 不可变：upsert 按 chunk_id 覆盖（内容不变得出相同结果，幂等）；
单条软删（文档撤回）允许 upsert 改 state=DEPRECATED（单条原子，ChromaDB 可接受），
区别于"批量翻转"（不做）。

BM25 投影：可选注入 ``bm25_index``（``Bm25Index | None``，混合召回启用时注入）。
ChromaDB 是唯一真相源，本仓库是写入 chokepoint -> 同步 add/remove 投影，保证两套存储不分叉。
投影同步失败仅告警不抛（降级为索引暂时 stale，下次启动全量重建修复），不影响 ChromaDB 写入。
"""
from __future__ import annotations

import logging
from typing import Any

from app.routes.document.domain.chunk import DocumentChunk

logger = logging.getLogger(__name__)


class ChunkRepo:
    """ChromaDB chunk 操作。"""

    def __init__(self, collection: Any, bm25_index: Any = None) -> None:
        self._collection = collection
        self._bm25_index = bm25_index  # Bm25Index | None

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
        await self._sync_index_add(chunks)

    async def soft_delete(self, chunk_id: str) -> None:
        """单条软删：state=DEPRECATED（单条原子，区别于批量翻转）。"""
        self._collection.update(ids=[chunk_id], metadatas=[{"state": "DEPRECATED"}])
        await self._sync_index_remove([chunk_id])

    async def delete_by_version(self, version_id: str) -> None:
        """按 version 删除（仅 ChromaDB 重建时使用）。"""
        self._collection.delete(where={"version_id": version_id})
        if self._bm25_index is not None:
            try:
                await self._bm25_index.remove_by_version(version_id)
            except Exception as exc:
                logger.warning("BM25 投影 remove_by_version 失败(version=%s): %s", version_id, exc)

    async def count(self) -> int:
        return self._collection.count()

    # ── BM25 投影同步（chokepoint 内派生，失败仅告警）──
    async def _sync_index_add(self, chunks: list[DocumentChunk]) -> None:
        if self._bm25_index is None:
            return
        try:
            await self._bm25_index.add(chunks)
        except Exception as exc:
            logger.warning("BM25 投影 add 失败(%d 条): %s", len(chunks), exc)

    async def _sync_index_remove(self, chunk_ids: list[str]) -> None:
        if self._bm25_index is None:
            return
        try:
            await self._bm25_index.remove(chunk_ids)
        except Exception as exc:
            logger.warning("BM25 投影 remove 失败(%s): %s", chunk_ids, exc)
