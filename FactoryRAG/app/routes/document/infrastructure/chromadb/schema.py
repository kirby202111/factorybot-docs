"""ChromaDB collection 初始化（非 Alembic，chunk 不可变无 schema 演进）。

启动时幂等创建/获取 collection；metadata 字段：
route_version/state/tenant_scope/doc_id/doc_type/chunk_seq/locator。
"""
from __future__ import annotations

from typing import Any


class ChromaCollectionInitializer:
    """启动时幂等创建/获取 collection。

    distance = cosine（embedding 1024 维，默认百炼 text-embedding-v4）。chunk 不可变：collection 无 schema 演进，
    版本隔离靠查询 ``where`` pre-filter，不靠 collection 重建。
    """

    def __init__(self, collection_name: str = "document_chunks") -> None:
        self._name = collection_name

    def ensure(self, client: Any) -> Any:
        return client.get_or_create_collection(
            name=self._name,
            metadata={"hnsw:space": "cosine"},
        )
