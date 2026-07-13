"""B MySQL 侧文档元数据仓库（rag_doc schema）。

chunk 向量在 ChromaDB（非 MySQL）；幂等/位点/审计在 rag_shared（shared 模型）；
治理/审计聚合导出表 + 文档元数据在 rag_doc（MySQL）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.routes.document.domain.document import (
    DocumentBinding,
    DocumentCategory,
    DocumentSource,
    DocumentVersion,
    DocType,
    KnowledgeDocument,
    VersionState,
)
from app.shared.persistence.base import Base

logger = logging.getLogger(__name__)


class KnowledgeDocumentModel(Base):
    """文档聚合根表（rag_doc）。"""

    __tablename__ = "knowledge_document"
    __table_args__ = {"schema": "rag_doc"}

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentVersionModel(Base):
    """文档版本表（rag_doc）。``file_content_hash`` 唯一 = 摄入幂等键。"""

    __tablename__ = "document_version"
    __table_args__ = {"schema": "rag_doc"}

    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_no: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="UPLOAD")
    file_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    file_content_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    bindings: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentRepo:
    """文档元数据仓库（MySQL rag_doc）。"""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def save_document(self, doc: KnowledgeDocument, version: DocumentVersion) -> None:
        async with self._session_factory() as session:
            session.add(
                KnowledgeDocumentModel(
                    document_id=doc.document_id,
                    doc_type=doc.doc_type.value,
                    title=doc.title,
                    category=doc.category.value,
                    tenant_scope=doc.tenant_scope,
                    created_at=doc.created_at,
                )
            )
            session.add(self._version_to_model(version))
            await session.commit()

    async def find_by_hash(self, content_hash: str) -> DocumentVersion | None:
        async with self._session_factory() as session:
            stmt = select(DocumentVersionModel).where(
                DocumentVersionModel.file_content_hash == content_hash
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._model_to_version(row) if row else None

    async def get_document(self, document_id: str) -> KnowledgeDocument | None:
        """取聚合根（含 doc_type/tenant_scope，重索引重建 chunk 用）。"""
        async with self._session_factory() as session:
            doc_row = (
                await session.execute(
                    select(KnowledgeDocumentModel).where(
                        KnowledgeDocumentModel.document_id == document_id
                    )
                )
            ).scalar_one_or_none()
            if doc_row is None:
                return None
            ver_rows = (
                await session.execute(
                    select(DocumentVersionModel).where(
                        DocumentVersionModel.document_id == document_id
                    )
                )
            ).scalars().all()
            doc = KnowledgeDocument(
                document_id=doc_row.document_id,
                doc_type=DocType(doc_row.doc_type),
                title=doc_row.title,
                category=DocumentCategory(doc_row.category),
                tenant_scope=doc_row.tenant_scope,
                created_at=doc_row.created_at,
                versions=[self._model_to_version(r) for r in ver_rows],
            )
            return doc

    async def find_drafts_by_route(
        self, route_id: str, route_version: str
    ) -> list[DocumentVersion]:
        """查找绑定此 route 的 PROCESS_BOUND 文档版本（决策 #3：联动 PUBLISHED）。"""
        async with self._session_factory() as session:
            stmt = select(DocumentVersionModel).where(
                DocumentVersionModel.state.in_(
                    [VersionState.DRAFT.value, VersionState.PUBLISHED.value]
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                self._model_to_version(r)
                for r in rows
                if self._matches_route(r, route_id, route_version)
            ]

    async def find_published_by_route(
        self, route_id: str, route_version: str
    ) -> list[DocumentVersion]:
        async with self._session_factory() as session:
            stmt = select(DocumentVersionModel).where(
                DocumentVersionModel.state == VersionState.PUBLISHED.value
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                self._model_to_version(r)
                for r in rows
                if self._matches_route(r, route_id, route_version)
            ]

    async def update_state(
        self,
        session: Any,
        version_id: str,
        state: VersionState,
        *,
        effective: bool = False,
        deprecate: bool = False,
    ) -> None:
        """同事务更新版本状态（与幂等/位点记录一起提交）。"""
        stmt = select(DocumentVersionModel).where(DocumentVersionModel.version_id == version_id)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.state = state.value
        now = datetime.now(timezone.utc)
        if effective:
            row.effective_at = now
        if deprecate:
            row.deprecated_at = now

    async def deprecate_old_published(
        self, session: Any, document_id: str, exclude_version_id: str
    ) -> None:
        """同类绑定的旧 PUBLISHED -> DEPRECATED（新版本 PUBLISHED 时）。"""
        stmt = select(DocumentVersionModel).where(
            DocumentVersionModel.document_id == document_id,
            DocumentVersionModel.version_id != exclude_version_id,
            DocumentVersionModel.state == VersionState.PUBLISHED.value,
        )
        rows = (await session.execute(stmt)).scalars().all()
        now = datetime.now(timezone.utc)
        for r in rows:
            r.state = VersionState.DEPRECATED.value
            r.deprecated_at = now

    # ── 映射 ──

    @staticmethod
    def _matches_route(row: DocumentVersionModel, route_id: str, route_version: str) -> bool:
        for b in row.bindings or []:
            if (
                b.get("binding_type") == "ROUTE_VERSION"
                and b.get("target_ref", {}).get("route_id") == route_id
                and b.get("target_ref", {}).get("route_version") == route_version
            ):
                return True
        return False

    def _version_to_model(self, v: DocumentVersion) -> DocumentVersionModel:
        return DocumentVersionModel(
            version_id=v.version_id,
            document_id=v.document_id,
            version_no=v.version_no,
            state=v.state.value,
            source_type=v.source.source_type,
            file_ref=v.file_ref,
            file_content_hash=v.file_content_hash,
            bindings=[b.model_dump(mode="json") for b in v.bindings],
            effective_at=v.effective_at,
            deprecated_at=v.deprecated_at,
            created_at=v.created_at,
        )

    def _model_to_version(self, r: DocumentVersionModel) -> DocumentVersion:
        bindings = [DocumentBinding.model_validate(b) for b in (r.bindings or [])]
        return DocumentVersion(
            version_id=r.version_id,
            document_id=r.document_id,
            version_no=r.version_no,
            state=VersionState(r.state),
            source=DocumentSource(source_type=r.source_type),
            file_ref=r.file_ref,
            file_content_hash=r.file_content_hash,
            bindings=bindings,
            effective_at=r.effective_at,
            deprecated_at=r.deprecated_at,
            created_at=r.created_at,
        )
