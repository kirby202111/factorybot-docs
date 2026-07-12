"""ToolResultCache：版本化工具结果缓存（Redis）。re-export 自 redis_。

cache key 含 route_version/tenant。语义缓存默认关闭（MES 追溯时变性），
本实现是按 key 的精确缓存，仅对工艺参数/8D 模板等稳定场景灰度。
"""
from __future__ import annotations

from app.infrastructure.redis_.tool_cache import ToolResultCache as _Impl


class ToolResultCache(_Impl):
    """与 redis_.ToolResultCache 同实现，cost 层语义入口。"""
    pass
