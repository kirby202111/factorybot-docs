"""B MinIO 原始文档对象存储（S3 兼容，path-style-access）。

原始文档留 MinIO，向量库可从 MinIO 重建（备份兜底，§1.2）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ObjectStore:
    """MinIO 对象存储。bucket=rag-docs，路径 ``{doc_id}/{version_id}/raw/{filename}``。"""

    BUCKET = "rag-docs"

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str = BUCKET,
        secure: bool = False,
    ) -> None:
        self._endpoint = endpoint
        self._bucket = bucket
        self._client: Any = None
        self._connect(endpoint, access_key, secret_key, secure)

    def _connect(self, endpoint: str, access_key: str, secret_key: str, secure: bool) -> None:
        try:
            from minio import Minio  # type: ignore

            self._client = Minio(
                endpoint, access_key=access_key, secret_key=secret_key, secure=secure
            )
        except Exception as exc:  # minio 未安装时降级（测试/本地）
            logger.warning("MinIO 客户端不可用，ObjectStore 将走内存兜底: %s", exc)
            self._client = None
            self._mem: dict[str, bytes] = {}

    async def put(self, doc_id: str, version_id: str, filename: str, content: bytes) -> str:
        """上传原始文件，返回 file_ref。"""
        key = f"{doc_id}/{version_id}/raw/{filename}"
        if self._client is not None:
            import io

            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
            self._client.put_object(
                self._bucket, key, io.BytesIO(content), length=len(content)
            )
        else:
            self._mem[key] = content  # type: ignore[attr-defined]
        return f"minio://{self._bucket}/{key}"

    async def get(self, file_ref: str) -> bytes:
        key = file_ref.split(f"{self._bucket}/")[-1]
        if self._client is not None:
            resp = self._client.get_object(self._bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        return self._mem.get(key, b"")  # type: ignore[attr-defined]

    async def delete(self, file_ref: str) -> None:
        key = file_ref.split(f"{self._bucket}/")[-1]
        if self._client is not None:
            self._client.remove_object(self._bucket, key)
        else:
            self._mem.pop(key, None)  # type: ignore[attr-defined]
