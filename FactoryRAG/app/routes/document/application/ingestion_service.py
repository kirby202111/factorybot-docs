"""B 文档摄入服务。

1. 上传 MinIO + 计算 SHA-256
2. file_content_hash 查重 -> 命中返回 already_ingested
3. 解析 -> 切分 -> bge-m3 批量 embed
4. MySQL 事务持久化 knowledge_document + document_version
5. ChromaDB 批量 upsert chunks（state=PUBLISHED 固定，chunk 不可变）
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from app.routes.document.application.chunking import ChunkStrategySelector
from app.routes.document.domain.chunk import DocumentChunk
from app.routes.document.domain.document import (
    DocumentCategory,
    DocumentVersion,
    DocType,
    KnowledgeDocument,
    VersionState,
)
from app.routes.document.domain.answer import IngestCommand, IngestResponse
from app.shared.obs.port import ObservabilityPort
from app.shared.tenant.context import TenantContext

logger = logging.getLogger(__name__)


class DocumentIngestionService:
    """文档摄入 application service。

    SRP：只管"摄入流水线"；检索/重索引分别在 RetrievalService/ReindexCoordinator。
    """

    def __init__(
        self,
        *,
        object_store: Any,
        parser: Any,
        chunk_selector: ChunkStrategySelector,
        embedder: Any,
        doc_repo: Any,
        chunk_repo: Any,
        obs: ObservabilityPort,
    ) -> None:
        self._store = object_store
        self._parser = parser
        self._chunk_selector = chunk_selector
        self._embedder = embedder
        self._doc_repo = doc_repo
        self._chunk_repo = chunk_repo
        self._obs = obs

    async def ingest(self, cmd: IngestCommand, tenant: TenantContext) -> IngestResponse:
        content = base64.b64decode(cmd.content_b64)
        content_hash = hashlib.sha256(content).hexdigest()

        # 2. 查重（file_content_hash 幂等键）
        existing = await self._doc_repo.find_by_hash(content_hash)
        if existing is not None:
            logger.info("文档已摄入（hash 命中）: %s", content_hash[:12])
            return IngestResponse(version_id=existing.version_id, status="already_ingested")

        # 1. 上传 MinIO
        doc_id = self._new_id()
        version_id = self._new_id()
        file_ref = await self._store.put(doc_id, version_id, cmd.filename, content)

        # 摄入 scope 授权告警：chunk 的 tenant_scope 以 cmd.tenant_scope 为准（与文档记录一致），
        # 不再静默取 tenant.tenant_scopes[0]；若声明 scope 不在租户授权 scopes 内，记 warning 供审计。
        self._warn_scope_mismatch(tenant, cmd)

        # 3. 解析 + 切分
        text = await self._parser.parse(content, cmd.doc_type)
        chunks = self._chunk_selector.split(
            text=text,
            doc_type=cmd.doc_type,
            doc_id=doc_id,
            version_id=version_id,
            tenant_scope=cmd.tenant_scope,
            version_anchor=self._first_version_anchor(cmd.bindings),
            file_content_hash=content_hash,
        )

        # 3b. bge-m3 批量 embed
        embeddings = await self._embedder.embed_batch([c.text for c in chunks])
        for c, emb in zip(chunks, embeddings):
            c.embedding = emb

        # 4. MySQL 持久化（聚合根 + 版本）
        version = DocumentVersion(
            version_id=version_id,
            document_id=doc_id,
            version_no="v1",
            state=self._initial_state(cmd.category),
            file_ref=file_ref,
            file_content_hash=content_hash,
            bindings=cmd.bindings,
        )
        doc = KnowledgeDocument(
            document_id=doc_id,
            doc_type=cmd.doc_type,
            title=cmd.title,
            category=cmd.category,
            tenant_scope=cmd.tenant_scope,
            versions=[version],
        )
        doc.enforce_category_invariant()
        await self._doc_repo.save_document(doc, version)

        # 5. ChromaDB upsert chunks（chunk 不可变，state 固定 PUBLISHED）
        await self._chunk_repo.upsert_chunks(chunks)
        try:
            self._obs.metrics.doc_ingest_chunks.labels(category=cmd.category.value).inc(len(chunks))
        except Exception:
            pass

        logger.info("文档摄入完成 doc_id=%s chunks=%d", doc_id, len(chunks))
        return IngestResponse(version_id=version_id, status="created")

    @staticmethod
    def _initial_state(category: DocumentCategory) -> VersionState:
        # 工艺绑定型初始 DRAFT，等 ProcessRouteActivated 联动 PUBLISHED（决策 #3）；
        # 通用知识型/设备绑定型也 DRAFT，走管理接口手动 PUBLISHED。
        return VersionState.DRAFT

    @staticmethod
    def _first_version_anchor(bindings: list) -> "VersionAnchor | None":
        """从 bindings 取第一个有效版本锚点（chunk metadata 同步用）。"""
        for b in bindings:
            anchor = b.version_anchor()
            if anchor is not None:
                return anchor
        return None

    @staticmethod
    def _warn_scope_mismatch(tenant: TenantContext, cmd: IngestCommand) -> None:
        """摄入 scope 与租户授权 scopes 不一致时告警（不阻断、不静默改写）。

        chunk 的 ``tenant_scope`` 以 ``cmd.tenant_scope`` 为准，与文档聚合根记录一致
        （此前取 ``tenant.tenant_scopes[0]`` 会与文档记录分裂，且多 scope 静默丢弃其余）。
        若租户带授权 scopes 而声明 scope 不在其中，记 warning 供审计 -- 授权拦截由上层
        （中间件 / Port）负责，此处仅暴露异常，保持摄入流水线单一职责。
        """
        if tenant.tenant_scopes and cmd.tenant_scope not in tenant.tenant_scopes:
            logger.warning(
                "摄入 tenant_scope=%s 不在租户授权 scopes=%s（tenant_id=%s）",
                cmd.tenant_scope,
                tenant.tenant_scopes,
                tenant.tenant_id,
            )

    @staticmethod
    def _new_id() -> str:
        from uuid import uuid4

        return str(uuid4())
