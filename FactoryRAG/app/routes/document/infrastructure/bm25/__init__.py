"""BM25 稀疏检索基础设施包（B 路线粗排召回的稀疏路）。

- ``Tokenizer``：中文 jieba 分词，缺失降级正则。
- ``Bm25Index``：rank_bm25 内存索引，ChromaDB chunk 的只读投影。
- ``Bm25Retriever``：满足 ``RetrieverPort``，关键词精确匹配。
"""
from app.routes.document.infrastructure.bm25.bm25_index import Bm25Index
from app.routes.document.infrastructure.bm25.bm25_retriever import Bm25Retriever
from app.routes.document.infrastructure.bm25.tokenizer import Tokenizer

__all__ = ["Tokenizer", "Bm25Index", "Bm25Retriever"]
