"""ToolResultCache：版本化工具结果缓存（Redis）。cache key 含 version_anchor/tenant。

降本五层杠杆的缓存层（最后一步）。MES 追溯时变性，语义缓存默认关闭；
仅对工艺参数/8D 模板等稳定场景灰度。本实现是按 key 的精确缓存，非语义缓存。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.domain.version import VersionAnchor
from app.infrastructure.redis_.fake_redis import get_redis


class ToolResultCache:
    def __init__(self, redis=None, default_ttl: int = 300) -> None:
        self._redis = redis or get_redis()
        self._default_ttl = default_ttl

    def _key(self, tenant_id: str, tool_name: str, args: dict,
             version_anchor: Optional[VersionAnchor]) -> str:
        anchor_str = version_anchor.model_dump_json() if version_anchor else ""
        payload = json.dumps({"t": tenant_id, "a": args, "v": anchor_str}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return f"tc:{tenant_id}:{tool_name}:{digest}"

    async def get(self, tenant_id: str, tool_name: str, args: dict,
                  version_anchor: Optional[VersionAnchor] = None) -> Optional[Any]:
        raw = await self._redis.get(self._key(tenant_id, tool_name, args, version_anchor))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw) if isinstance(raw, str) else raw

    async def set(self, tenant_id: str, tool_name: str, args: dict, value: Any,
                  version_anchor: Optional[VersionAnchor] = None, ttl: Optional[int] = None) -> None:
        await self._redis.setex(
            self._key(tenant_id, tool_name, args, version_anchor),
            ttl or self._default_ttl,
            json.dumps(value, default=str),
        )
