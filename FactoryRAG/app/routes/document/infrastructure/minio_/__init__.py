"""B MinIO 对象存储包（``minio_`` 下划线后缀避开 ``minio`` 顶层包名冲突）。"""
from app.routes.document.infrastructure.minio_.object_store import ObjectStore

__all__ = ["ObjectStore"]
