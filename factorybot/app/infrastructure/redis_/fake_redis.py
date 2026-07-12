"""进程内 FakeRedis：mock 模式下替代 redis.asyncio.Redis。

实现 ConfirmationStore / ToolResultCache 用到的子集：get/set/setex/delete/exists/expire。
接口与 redis.asyncio.Redis 兼容，real 模式下直接换成真 Redis。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional, Protocol


class RedisLike(Protocol):
    async def get(self, key: str) -> Optional[Any]: ...
    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> None: ...
    async def setex(self, key: str, ttl: int, value: Any) -> None: ...
    async def delete(self, key: str) -> int: ...
    async def exists(self, key: str) -> int: ...


class FakeRedis:
    """进程内实现，支持 TTL（过期按时间戳判定）。"""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, Optional[float]]] = {}  # key -> (value, expire_at)

    async def get(self, key: str) -> Optional[Any]:
        self._evict(key)
        entry = self._store.get(key)
        return entry[0] if entry else None

    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> None:
        expire_at = (time.time() + ex) if ex else None
        self._store[key] = (value, expire_at)

    async def setex(self, key: str, ttl: int, value: Any) -> None:
        await self.set(key, value, ex=ttl)

    async def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        self._evict(key)
        return 1 if key in self._store else 0

    async def expire(self, key: str, ttl: int) -> int:
        if key in self._store:
            self._store[key] = (self._store[key][0], time.time() + ttl)
            return 1
        return 0

    def _evict(self, key: str) -> None:
        entry = self._store.get(key)
        if entry and entry[1] is not None and entry[1] <= time.time():
            self._store.pop(key, None)


_redis: FakeRedis | Any | None = None


def get_redis() -> Any:
    """单例 Redis。real 模式（REDIS_URL 配置）返回真 redis.asyncio.Redis，否则 FakeRedis。"""
    global _redis
    if _redis is not None:
        return _redis
    from app.config import get_settings
    s = get_settings()
    if s.redis_url and not s.is_mock:
        import redis.asyncio as aioredis  # type: ignore
        _redis = aioredis.from_url(s.redis_url, decode_responses=True)
    else:
        _redis = FakeRedis()
    return _redis
