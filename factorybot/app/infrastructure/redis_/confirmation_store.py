"""ConfirmationStore：confirmation token 签发 / 校验 / 防重复确认。

token = secrets.token_hex(16)（32 字符随机 hex），TTL 30min。
校验：存在性 + action 匹配（防篡改）。防重复确认：session+step -> token_id。
承载 编排 写动作的"人确认"闸门。
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from app.infrastructure.redis_.fake_redis import RedisLike, get_redis


@dataclass(frozen=True)
class ConfirmationToken:
    id: str                 # 32 字符 hex, token_hex(16)
    session_id: str
    step: str
    action: str             # = f"{session_id}:{step}" 或写动作 f"{verb}:{session_id}"
    approved: bool          # 人点了确认还是拒绝
    user_id: str
    issued_at: int          # Unix 时间戳

    def valid_for(self, expected_action: str) -> bool:
        return self.action == expected_action


class ConfirmationStore:
    def __init__(self, redis: Optional[RedisLike] = None, token_ttl: int = 1800) -> None:
        self._redis = redis or get_redis()
        self._ttl = token_ttl

    async def issue(
        self, session_id: str, step: str, approved: bool, user_id: str,
        action: Optional[str] = None,
    ) -> ConfirmationToken:
        """签发 token。action 默认 f"{session_id}:{step}"。"""
        token_id = secrets.token_hex(16)
        action = action or f"{session_id}:{step}"
        payload = {
            "token_id": token_id,
            "session_id": session_id,
            "step": step,
            "action": action,
            "approved": approved,
            "user_id": user_id,
            "issued_at": int(time.time()),
        }
        # 防重复确认：同 session+step 已有 token 则拒绝
        existing = await self._redis.get(f"confirm:session:{session_id}:{step}")
        if existing:
            # 已确认过，返回既有 token（幂等）
            return ConfirmationToken(
                id=existing, session_id=session_id, step=step,
                action=action, approved=approved, user_id=user_id,
                issued_at=int(time.time()),
            )
        await self._redis.setex(f"confirm:{token_id}", self._ttl, json.dumps(payload))
        await self._redis.setex(f"confirm:session:{session_id}:{step}", self._ttl, token_id)
        return ConfirmationToken(
            id=token_id, session_id=session_id, step=step,
            action=action, approved=approved, user_id=user_id,
            issued_at=payload["issued_at"],
        )

    async def validate(self, token_id: str, expected_action: str) -> bool:
        """校验：token 存在 + action 匹配。"""
        raw = await self._redis.get(f"confirm:{token_id}")
        if raw is None:
            return False
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if payload.get("action") != expected_action:
            return False
        return True

    async def get(self, token_id: str) -> Optional[ConfirmationToken]:
        raw = await self._redis.get(f"confirm:{token_id}")
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        p = json.loads(raw) if isinstance(raw, str) else raw
        return ConfirmationToken(
            id=p["token_id"], session_id=p["session_id"], step=p["step"],
            action=p["action"], approved=p["approved"], user_id=p["user_id"],
            issued_at=p["issued_at"],
        )

    async def is_already_confirmed(self, session_id: str, step: str) -> bool:
        return await self._redis.get(f"confirm:session:{session_id}:{step}") is not None
