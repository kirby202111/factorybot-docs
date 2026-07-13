"""只读红线：ReadOnly*Gate 启动断言。

统一的 Gate 体系在 ``shared/web/lifespan`` 启动期扫描，任一失败即拒绝启动（fail-fast）。
最坏情况是"没检索出来"，不会产生写副作用。
"""
from __future__ import annotations

import inspect
from typing import Any


class StartupAssertionError(RuntimeError):
    """启动断言失败，拒绝启动（fail-fast）。"""


class ReadOnlyAclGate:
    """扫描所有 ``BaseReadonlyAclClient`` 子类，方法名禁止写动词。

    实现（§3.2）：遍历 ``dir(type(c))``，方法名命中写动词前缀或含 ``_<verb>``
    即拒绝启动。这是把"只读旁路"从约定变成结构属性。
    """

    VERBS = {"create", "update", "delete", "post", "put", "patch", "remove", "save", "insert"}

    def assert_readonly(self, clients: list[Any]) -> None:
        for c in clients:
            cls = type(c)
            for name in dir(cls):
                if name.startswith("__"):
                    continue
                if self._is_write_verb(name):
                    raise StartupAssertionError(
                        f"写动词方法名 '{name}' 出现在只读 ACL {cls.__name__}（违反只读红线）"
                    )

    # 统一启动断言入口（供 lifespan 调度）。
    def assert_on(self, clients: list[Any]) -> None:
        self.assert_readonly(clients)

    def _is_write_verb(self, name: str) -> bool:
        low = name.lower()
        if any(low.startswith(v) for v in self.VERBS):
            return True
        if any(f"_{v}" in low for v in self.VERBS):
            return True
        return False


def assert_no_write_calls_in_handler(handler: Any) -> None:
    """扫描 handler.handle 方法的源码，禁止出现对 MES 写接口的调用（B 的 ReadOnlyIngestionGate 用）。

    静态扫描 handler 依赖的 ACL client 方法名（更可靠），此处提供源码兜底。
    """
    try:
        source = inspect.getsource(handler.handle)
    except (OSError, TypeError):
        return
    for verb in ReadOnlyAclGate.VERBS:
        if f".{verb}(" in source:
            raise StartupAssertionError(
                f"handler {type(handler).__name__}.handle 含写动词调用 '{verb}'（违反只读摄入红线）"
            )
