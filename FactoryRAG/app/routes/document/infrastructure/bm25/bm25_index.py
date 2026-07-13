"""BM25 倒排索引（rank_bm25 包装）。

作为 ChromaDB chunk 的**只读投影**：ChromaDB 是唯一真相源，本索引是内存缓存，
启动期全量构建（仅 PUBLISHED），运行期由 ``ChunkRepo`` 写入 chokepoint 同步 add/remove，
保证两套存储永不分叉。chunk 不可变 -> 同 chunk_id 视为幂等覆盖（内容一致）。

并发：``BM25Okapi`` 非线程安全，且 add/remove 需重建底层结构，读写互斥用 ``asyncio.Lock``。
规模：MVP 车间文档语料万级 chunk，add/remove 全量重建 O(N) 可接受；后续可换增量 Okapi。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from app.routes.document.domain.chunk import DocumentChunk
from app.routes.document.infrastructure.bm25.tokenizer import Tokenizer

logger = logging.getLogger(__name__)


class Bm25Index:
    """BM25 稀疏检索索引（内存投影）。"""

    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tokenizer = tokenizer
        self._lock = asyncio.Lock()
        self._chunk_ids: list[str] = []
        self._tokens: list[list[str]] = []
        self._chunks: list[DocumentChunk] = []
        self._id_to_pos: dict[str, int] = {}
        self._bm25: Any = None  # rank_bm25.BM25Okapi

    @property
    def size(self) -> int:
        """当前索引 chunk 数（仅 PUBLISHED）。"""
        return len(self._chunk_ids)

    # ── 构建 ──
    async def build_from_chunks(self, chunks: list[DocumentChunk]) -> None:
        """全量构建（仅纳入 PUBLISHED）。先 reset 再追加。"""
        async with self._lock:
            self._reset_unlocked()
            for c in chunks:
                if c.state != "PUBLISHED":
                    continue
                if c.chunk_id in self._id_to_pos:
                    continue
                self._append_unlocked(c)
            self._rebuild_unlocked()
            logger.info("BM25 索引构建完成，size=%d", self.size)

    async def build_from_collection(self, collection: Any) -> None:
        """从 ChromaDB collection 全量拉取 PUBLISHED chunk 构建。

        ChromaDB 是真相源；embedding 不需要（BM25 只用文本），传 ``[]`` 占位。
        """
        result = collection.get(
            where={"state": "PUBLISHED"},
            include=["documents", "metadatas"],
        )
        ids = result.get("ids", []) or []
        docs = result.get("documents", []) or []
        metas = result.get("metadatas", []) or []
        chunks = [
            DocumentChunk.from_chroma(
                chunk_id=cid, document=doc, embedding=[], metadata=meta
            )
            for cid, doc, meta in zip(ids, docs, metas)
        ]
        await self.build_from_chunks(chunks)

    # ── 增量维护（ChunkRepo 写入 chokepoint 调用）──
    async def add(self, chunks: list[DocumentChunk]) -> None:
        """追加新 chunk（仅 PUBLISHED；已存在 chunk_id 幂等跳过）。"""
        if not chunks:
            return
        async with self._lock:
            added = 0
            for c in chunks:
                if c.state != "PUBLISHED":
                    continue
                if c.chunk_id in self._id_to_pos:
                    continue
                self._append_unlocked(c)
                added += 1
            if added:
                self._rebuild_unlocked()
                logger.debug("BM25 索引追加 %d 条，size=%d", added, self.size)

    async def remove(self, chunk_ids: list[str]) -> None:
        """按 chunk_id 移除（软删/重索引删调用）。"""
        if not chunk_ids:
            return
        async with self._lock:
            self._remove_ids_unlocked({cid for cid in chunk_ids if cid in self._id_to_pos})

    async def remove_by_version(self, version_id: str) -> None:
        """按 version_id 移除（ChromaDB ``delete_by_version`` 调用）。"""
        async with self._lock:
            ids = {
                self._chunk_ids[i]
                for i, c in enumerate(self._chunks)
                if c.version_id == version_id
            }
            self._remove_ids_unlocked(ids)

    # ── 检索 ──
    async def search(
        self,
        query: str,
        *,
        predicate: Callable[[DocumentChunk], bool],
        top_k: int,
    ) -> list[tuple[DocumentChunk, float]]:
        """BM25 打分检索。

        先对全部 chunk 应用 ``predicate``（与稠密路 ``ChunkFilter`` 同语义）过滤候选，
        再用 BM25 打分截断 top_k。返回 ``(DocumentChunk, bm25_score)``，分数越高越相关。
        """
        async with self._lock:
            if self._bm25 is None or not self._chunk_ids:
                return []
            q_tokens = self._tokenizer.tokenize(query)
            if not q_tokens:
                return []
            scores = self._bm25.get_scores(q_tokens)  # len = corpus
            cand: list[tuple[DocumentChunk, float]] = [
                (self._chunks[i], float(scores[i]))
                for i in range(len(self._chunks))
                if predicate(self._chunks[i])
            ]
            cand.sort(key=lambda x: x[1], reverse=True)
            return cand[:top_k]

    # ── 内部（调用方已持锁）──
    def _append_unlocked(self, chunk: DocumentChunk) -> None:
        tokens = self._tokenizer.tokenize(chunk.text)
        if not tokens:
            # 无可索引词项（纯停用词/空文本）：BM25 无法召回，跳过；
            # 亦避免 BM25Okapi 全空语料 avgdl=0 除零。
            return
        pos = len(self._chunk_ids)
        self._chunk_ids.append(chunk.chunk_id)
        self._tokens.append(tokens)
        self._chunks.append(chunk)
        self._id_to_pos[chunk.chunk_id] = pos

    def _remove_ids_unlocked(self, ids_set: set[str]) -> None:
        if not ids_set:
            return
        kept = [
            (cid, tok, c)
            for cid, tok, c in zip(self._chunk_ids, self._tokens, self._chunks)
            if cid not in ids_set
        ]
        self._reset_unlocked()
        for cid, tok, c in kept:
            self._id_to_pos[cid] = len(self._chunk_ids)
            self._chunk_ids.append(cid)
            self._tokens.append(tok)
            self._chunks.append(c)
        self._rebuild_unlocked()
        logger.debug("BM25 索引移除 %d 条，size=%d", len(ids_set), self.size)

    def _rebuild_unlocked(self) -> None:
        if not self._tokens:
            self._bm25 = None
            return
        from rank_bm25 import BM25Okapi

        self._bm25 = BM25Okapi(self._tokens)

    def _reset_unlocked(self) -> None:
        self._chunk_ids.clear()
        self._tokens.clear()
        self._chunks.clear()
        self._id_to_pos.clear()
        self._bm25 = None
