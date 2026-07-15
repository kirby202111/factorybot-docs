"""领域异常：跨租户资源访问统一抛 ResourceAccessError，路由层映射为 404（隐藏存在性）。

多租户隔离约定：资源不存在与不属于当前租户对外不可区分，均返回 404，
避免向跨租户调用方泄露资源存在性。
"""
from __future__ import annotations


class ResourceAccessError(LookupError):
    """资源不存在或不属于当前租户（多租户隔离：两者对外不可区分）。"""
