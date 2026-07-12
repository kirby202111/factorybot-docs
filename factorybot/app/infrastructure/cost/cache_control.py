"""CacheControl：为 system prompt + 工具定义标记 cache_control（ephemeral, TTL 5min/1h）。

prompt caching 让稳定的 system prompt / 工具定义命中缓存，降低重复 token 成本。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class CacheControl:
    type: Literal["ephemeral"] = "ephemeral"
    ttl: Literal["5m", "1h"] = "5m"

    def to_dict(self) -> dict:
        return {"cache_control": {"type": self.type, "ttl": self.ttl}}


def mark_system_prompt_cache(prompt: str, ttl: str = "5m") -> dict:
    """把 system prompt 标记为可缓存（返回 provider 期望的结构）。"""
    cc = CacheControl(ttl=ttl)  # type: ignore[arg-type]
    return {"role": "system", "content": prompt, **cc.to_dict()}


def mark_tools_cache(tools: list[dict], ttl: str = "1h") -> list[dict]:
    """工具定义整体标记可缓存（稳定，TTL 长）。"""
    cc = CacheControl(ttl=ttl)  # type: ignore[arg-type]
    for t in tools:
        t.update(cc.to_dict())
    return tools
