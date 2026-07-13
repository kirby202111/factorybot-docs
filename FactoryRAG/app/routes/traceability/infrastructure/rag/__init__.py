"""A LlamaIndex 检索编排封装（可选上层，MVP 走裸 Cypher）。"""
from app.routes.traceability.infrastructure.rag.graph_index import GraphIndex

__all__ = ["GraphIndex"]
