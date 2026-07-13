"""按 config 构造 Embedding/Reranker，provider 无关（bge 本地 | 百炼）。

对齐 ``llm_factory`` 风格：provider 可插拔，默认百炼（text-embedding-v4 + gte-rerank-v2），
车间网隔离不能出网时回退 provider=bge 走本地 sidecar。Bge 实现保留向后兼容。
"""
from __future__ import annotations

from app.shared.config.base import EmbeddingSettings
from app.shared.embedding.bge_client import BgeClient, BgeReranker
from app.shared.embedding.bailian_client import BailianEmbeddingClient, BailianReranker
from app.shared.embedding.port import EmbeddingPort, RerankerPort


def build_embedding(settings: EmbeddingSettings) -> EmbeddingPort:
    """按 ``settings.provider`` 构造 EmbeddingPort。provider: bge | bailian。"""
    provider = settings.provider.lower()
    if provider == "bailian":
        return BailianEmbeddingClient(settings)
    if provider == "bge":
        return BgeClient(settings)
    raise ValueError(f"不支持的 embedding provider: {settings.provider}")


def build_reranker(settings: EmbeddingSettings) -> RerankerPort:
    """按 ``settings.provider`` 构造 RerankerPort。provider: bge | bailian。"""
    provider = settings.provider.lower()
    if provider == "bailian":
        return BailianReranker(settings)
    if provider == "bge":
        return BgeReranker(settings)
    raise ValueError(f"不支持的 reranker provider: {settings.provider}")
