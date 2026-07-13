"""shared/embedding -- 向量化 + 精排抽象。

provider 无关、可插拔：默认百炼 ``text-embedding-v4``（1024 维）+ ``gte-rerank-v2``；
可切 bge-m3 + bge-reranker-v2-m3 本地 sidecar（车间网隔离场景）。
经 ``embedding_factory`` 按 ``settings.embedding.provider`` 选择。
口径见《rag-service-整体结构设计》§3.2、《技术选型和实现方案》§2.2。
"""
from app.shared.embedding.bge_client import BgeClient, BgeReranker
from app.shared.embedding.bailian_client import BailianEmbeddingClient, BailianReranker
from app.shared.embedding.embedding_factory import build_embedding, build_reranker
from app.shared.embedding.port import EmbeddingPort, RerankerPort

__all__ = [
    "EmbeddingPort",
    "RerankerPort",
    "build_embedding",
    "build_reranker",
    "BgeClient",
    "BgeReranker",
    "BailianEmbeddingClient",
    "BailianReranker",
]
