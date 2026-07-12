"""Redis 适配：ConfirmationStore / ToolResultCache / 限流。

mock 模式用进程内 FakeRedis；real 模式用 redis.asyncio.Redis。两者满足同一 RedisLike 接口。
"""
from app.infrastructure.redis_.confirmation_store import ConfirmationToken, ConfirmationStore
from app.infrastructure.redis_.fake_redis import FakeRedis, get_redis
from app.infrastructure.redis_.tool_cache import ToolResultCache

__all__ = ["ConfirmationToken", "ConfirmationStore", "FakeRedis", "get_redis", "ToolResultCache"]
