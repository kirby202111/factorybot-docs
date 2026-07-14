"""B 检索器抽象（DIP）。

第一阶段召回（粗排）的统一契约：稠密（VectorRetriever）/ 稀疏（Bm25Retriever）/
混合（HybridRetriever）均满足本协议，``DocumentRetrievalService`` 依赖抽象而非实现，
组合根按 ``DocSettings.hybrid_recall_enabled`` 注入具体实现。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.routes.document.domain.answer import ChunkHit
from app.shared.events.version_contract import VersionAnchor
from app.shared.tenant.context import TenantContext


@runtime_checkable
class RetrieverPort(Protocol):
    """第一阶段召回抽象。

    返回 ``list[ChunkHit]``，``score`` 语义由实现自定（稠密=cosine 相似度、
    稀疏=BM25 分、混合=RRF 融合分）；下游 rerank 阶段以 cross-encoder 重排，
    不依赖此处 score 的绝对量纲，仅依赖相对顺序。

    过滤语义（state=PUBLISHED + 版本锚点等值 + tenant_scope + doc_type）由各实现自行落实：
    稠密走 ChromaDB ``where`` pre-filter，稀疏走内存谓词，二者必须等价（见
    ``infrastructure.chunk_filter``）。版本锚点统一为 ``VersionAnchor``（route/bom/rule/asset/standard）。
    """

    async def retrieve(
        self,
        *,
        query: str,
        tenant: TenantContext,
        version_anchor: VersionAnchor | None = None,
        doc_types: list[str] | None = None,
        top_k: int = 20,
    ) -> list[ChunkHit]:
        """按过滤条件召回 top_k 个 chunk 命中。"""
        ...
