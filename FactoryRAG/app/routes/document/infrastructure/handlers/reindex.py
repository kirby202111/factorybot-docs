"""``rag.reindex.request`` handler（A 升版 -> B 重索引）。

从 MinIO 拉原始文件 -> 重新切分 + 向量化 -> ChromaDB upsert（幂等）。
chunk 不可变使重索引幂等：chunk_id = ``{doc_id}:{version_id}:{chunk_seq}`` 不变，
ChromaDB upsert 按 id 覆盖，内容相同则无副作用，可安全重跑。
"""
from __future__ import annotations

import logging
from typing import Any

from app.routes.document.infrastructure.handlers.process_route import _RouteHandlerBase
from app.shared.kafka.domain_event import DomainEvent

logger = logging.getLogger(__name__)


class RagReindexRequestHandler(_RouteHandlerBase):
    """A 发布的 ``rag.reindex.request`` 内部事件 -> B 重索引。"""

    event_type = "rag.reindex.request"

    async def handle(self, event: DomainEvent, session: Any) -> None:
        route_id, route_version = self._route(event)
        if not route_id or not route_version:
            return
        versions = await self._doc_repo.find_published_by_route(route_id, route_version)
        if not versions:
            logger.debug("rag.reindex.request: 无关联文档 route=%s@%s", route_id, route_version)
            return
        for v in versions:
            doc = await self._doc_repo.get_document(v.document_id)
            if doc is None:
                continue
            await self._reingest(v, doc.tenant_scope, doc.doc_type.value)

    async def _reingest(self, version: Any, tenant_scope: str, doc_type: str) -> None:
        """从 MinIO 拉原始文件重建 chunk（幂等）。"""
        from app.routes.document.domain.document import DocType

        content = await self._object_store.get(version.file_ref)
        text = await self._parser.parse(content, DocType(doc_type))
        chunks = self._chunk_selector.split(
            text=text,
            doc_type=DocType(doc_type),
            doc_id=version.document_id,
            version_id=version.version_id,
            tenant_scope=tenant_scope,
            version_anchor=version.get_version_anchor(),
            file_content_hash=version.file_content_hash,
        )
        embeddings = await self._embedder.embed_batch([c.text for c in chunks])
        for c, emb in zip(chunks, embeddings):
            c.embedding = emb
        # chunk 不可变：upsert 幂等，重建安全
        await self._chunk_repo.upsert_chunks(chunks)
        logger.info("重索引完成 doc=%s version=%s chunks=%d", version.document_id, version.version_id, len(chunks))
