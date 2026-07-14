"""B 检索请求/应答 DTO（端点 schema + Port 契约）。

版本锚点通用化：``version``+``version_kind``+``version_ref_id`` 替代 route-specific 的
``route_version``+``asset_id``。工艺绑定型（PROCESS_BOUND）需 ROUTE 锚点（version 必填），
设备绑定型（ASSET_BOUND）需 ASSET 锚点（ref_id 必填，version 可选），通用知识型可选。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.routes.document.domain.document import DocumentCategory, DocType, DocumentBinding
from app.shared.events.version_contract import VersionAnchor, VersionKind


class DocQuery(BaseModel):
    """``POST /rag/docs/query`` 请求（检索 + LLM 综合）。

    工艺绑定型（PROCESS_BOUND）需 ROUTE 锚点（``version``+``version_kind="route"`` 必填）：
    入口校验拒绝缺失，不退回"查最新 ACTIVE"（避开在制品不切换工艺语义陷阱）。
    """

    question: str
    doc_category: DocumentCategory = DocumentCategory.GENERAL
    version: str | None = None              # 锁定的版本号（PROCESS_BOUND 必填）
    version_kind: str | None = None         # route|bom|rule|asset|standard
    version_ref_id: str | None = None       # route_id / asset_id / standard_id（ASSET_BOUND 必填）
    doc_types: list[DocType] | None = None
    top_k: int = 20
    top_n: int = 5

    def version_anchor(self) -> VersionAnchor | None:
        """构造版本锚点；无 version_kind/version 返回 None。"""
        if not self.version_kind or not self.version:
            return None
        try:
            return VersionAnchor(
                kind=VersionKind(self.version_kind),
                ref_id=self.version_ref_id or "",
                version=self.version,
            )
        except ValueError:
            return None


class DocSearch(BaseModel):
    """``POST /rag/docs/search`` 请求（只检索 chunks，不综合）。"""

    question: str
    version: str | None = None
    version_kind: str | None = None
    version_ref_id: str | None = None
    doc_types: list[DocType] | None = None
    top_k: int = 20

    def version_anchor(self) -> VersionAnchor | None:
        if not self.version_kind or not self.version:
            return None
        try:
            return VersionAnchor(
                kind=VersionKind(self.version_kind),
                ref_id=self.version_ref_id or "",
                version=self.version,
            )
        except ValueError:
            return None


class ChunkHit(BaseModel):
    """检索命中的 chunk（``/search`` 返回，供 L1 Agent / UI 直接消费）。"""

    chunk_id: str
    doc_id: str
    version_id: str
    text: str
    locator: dict
    section_type: str
    version_kind: str = ""
    version_ref_id: str = ""
    version: str = ""
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
    version_filter: str | None = None         # 实际过滤的版本号
    version_kind_filter: str | None = None    # 实际过滤的版本类型
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
