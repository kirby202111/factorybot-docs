"""B 检索请求/应答 DTO（端点 schema + Port 契约）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.routes.document.domain.document import DocumentCategory, DocType, DocumentBinding


class DocQuery(BaseModel):
    """``POST /rag/docs/query`` 请求（检索 + LLM 综合）。

    工艺绑定型（PROCESS_BOUND）``route_version`` 必填：入口校验拒绝缺失，
    不退回"查最新 ACTIVE"（避开在制品不切换工艺语义陷阱）。
    """

    question: str
    doc_category: DocumentCategory = DocumentCategory.GENERAL
    route_version: str | None = None       # PROCESS_BOUND 时必填
    asset_id: str | None = None            # ASSET_BOUND 时必填
    doc_types: list[DocType] | None = None
    top_k: int = 20
    top_n: int = 5


class DocSearch(BaseModel):
    """``POST /rag/docs/search`` 请求（只检索 chunks，不综合）。"""

    question: str
    route_version: str | None = None
    asset_id: str | None = None
    doc_types: list[DocType] | None = None
    top_k: int = 20


class ChunkHit(BaseModel):
    """检索命中的 chunk（``/search`` 返回，供 L1 Agent / UI 直接消费）。"""

    chunk_id: str
    doc_id: str
    version_id: str
    text: str
    locator: dict
    section_type: str
    route_version: str | None = None
    state: str = "PUBLISHED"
    score: float = 0.0                     # cosine 相似度


class DocCitation(BaseModel):
    """答案引用（指向原文档具体位置）。"""

    chunk_id: str
    document_id: str
    version_no: str
    title: str
    locator: dict
    quoted_text: str


class DocAnswer(BaseModel):
    """``POST /rag/docs/query`` 应答。"""

    answer: str
    citations: list[DocCitation] = Field(default_factory=list)
    confidence: float = 0.0
    route_version_filter: str | None = None
    disclaimer: str = "本答案来自文档型 RAG，处置需按现行 SOP 确认"
    needs_human_review: bool = False


class IngestCommand(BaseModel):
    """``POST /rag/docs/ingest`` 请求。"""

    filename: str
    content_b64: str                       # base64 编码的原始文件内容
    doc_type: DocType
    title: str
    category: DocumentCategory
    tenant_scope: str
    bindings: list[DocumentBinding] = Field(default_factory=list)


class IngestResponse(BaseModel):
    """``POST /rag/docs/ingest`` 应答。"""

    version_id: str
    status: str                            # created | already_ingested
