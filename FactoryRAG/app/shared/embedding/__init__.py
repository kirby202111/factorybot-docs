"""shared/embedding -- Embedding 抽象。

A（缺陷语义入口）/ B（文档主体）共用 bge-m3 1024 维；B 用 bge-reranker-v2-m3 精排。
口径见《rag-service-整体结构设计》§3.2、《技术选型和实现方案》§2.2。
"""
from app.shared.embedding.bge_client import BgeClient, BgeReranker
from app.shared.embedding.port import EmbeddingPort

__all__ = ["EmbeddingPort", "BgeClient", "BgeReranker"]
