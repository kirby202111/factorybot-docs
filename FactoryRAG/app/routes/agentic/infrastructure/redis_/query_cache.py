"""E 查询缓存（Redis）。

key: ``rag:agentic:cache:{tenant_id}:{sha256(question)}``；TTL 默认 300s。
租户隔离：同问题不同租户命中不同 key。无主动失效，仅 TTL 过期。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


class QueryCache:
    """E 查询缓存。"""

    def __init__(self, *, redis: Any, ttl: int = 300) -> None:
        self._redis = redis
        self._ttl = ttl

    def _key(self, question: str, tenant_id: str) -> str:
        normalized = question.strip().lower()
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        return f"rag:agentic:cache:{tenant_id}:{digest}"

    async def get(self, request: Any, tenant: Any) -> Any | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(request.question, tenant.tenant_id))
            if raw:
                from app.routes.agentic.domain.answer import AgentAnswer

                return AgentAnswer.model_validate_json(raw)
        except Exception:
            pass
        return None

    async def set(self, request: Any, tenant: Any, answer: Any) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                self._key(request.question, tenant.tenant_id), self._ttl, answer.model_dump_json()
            )
        except Exception:
            pass
