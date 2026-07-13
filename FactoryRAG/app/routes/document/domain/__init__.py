"""路线 B 领域层。"""
from app.routes.document.domain.answer import (
    ChunkHit,
    DocAnswer,
    DocCitation,
    DocQuery,
    DocSearch,
)
from app.routes.document.domain.chunk import ChunkLocator, DocumentChunk
from app.routes.document.domain.document import (
    BindingType,
    DocumentBinding,
    DocumentCategory,
    DocumentSource,
    DocumentVersion,
    DocType,
    KnowledgeDocument,
    VersionState,
)
from app.routes.document.domain.projection import (
    ReadOnlyIngestionGate,
    ReindexHandler,
)

__all__ = [
    "BindingType",
    "DocumentCategory",
    "DocType",
    "VersionState",
    "DocumentSource",
    "DocumentBinding",
    "DocumentVersion",
    "KnowledgeDocument",
    "ChunkLocator",
    "DocumentChunk",
    "DocQuery",
    "DocSearch",
    "DocAnswer",
    "DocCitation",
    "ChunkHit",
    "ReindexHandler",
    "ReadOnlyIngestionGate",
]
