"""草稿 NoWriteClientGate 启动断言：扫描 ACL client 方法名禁写动词。

草稿 不持有任何写 client：启动时遍历所有 ACL client 的公开方法，
命中写动词（create/update/delete/submit/release/issue/save/activate/publish...）
即拒绝启动。草稿 的 ACL client 方法只能以只读动词开头。
"""
from __future__ import annotations

from typing import Any


# 写动词前缀（小写匹配）。注意：fetch/query/search/get/list 等只读动词不在此列。
WRITE_VERBS: tuple[str, ...] = (
    "create", "update", "delete", "submit", "release", "issue",
    "save", "activate", "publish", "close", "cancel", "rework",
    "scrap", "block", "force", "write", "post", "put",
)


class NoWriteClientGate(RuntimeError):
    """草稿 禁止持有写 client 方法。"""


def assert_no_write_clients(acl_clients: list[Any]) -> None:
    """扫描所有 ACL client，命中写动词方法名即抛 NoWriteClientGate。"""
    violations: list[str] = []
    for client in acl_clients:
        for attr in dir(client):
            if attr.startswith("_"):
                continue
            member = getattr(client, attr, None)
            if not callable(member):
                continue
            lower = attr.lower()
            if any(lower.startswith(v) for v in WRITE_VERBS):
                violations.append(f"{client.__class__.__name__}.{attr}")
    if violations:
        raise NoWriteClientGate(
            "草稿 禁止持有写 client 方法: " + ", ".join(violations)
        )
