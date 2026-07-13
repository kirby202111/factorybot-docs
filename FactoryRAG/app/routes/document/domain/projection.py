"""B 重索引 handler 协议 + ReadOnlyIngestionGate 启动断言。

摄入/重索引 handler 禁止任何写 MES 调用（B 的只读红线）。
"""
from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable

from app.shared.acl.gates import StartupAssertionError
from app.shared.kafka.domain_event import DomainEvent


@runtime_checkable
class ReindexHandler(Protocol):
    """事件 -> 重索引/状态翻转 handler 协议（ISP）。"""

    event_type: str

    async def handle(self, event: DomainEvent, tx: Any) -> None:
        """处理事件（tx 为 MySQL 会话）。禁止写 MES。"""
        ...


class ReadOnlyIngestionGate:
    """启动断言：摄入/重索引 handler 禁止任何写 MES 调用。

    扫描 handler.handle 源码与依赖的 ACL client 方法名，发现写动词即拒绝启动。
    """

    WRITE_VERBS = ("create", "update", "delete", "post", "put", "patch", "remove", "save", "insert")

    def assert_on(self, coordinator: Any) -> None:
        handlers = getattr(coordinator, "handlers", {}) or {}
        for event_type, handler in handlers.items():
            self._assert_handler(handler)

    def _assert_handler(self, handler: Any) -> None:
        handle = getattr(handler, "handle", None)
        if handle is None:
            return
        try:
            source = inspect.getsource(handle)
        except (OSError, TypeError):
            source = ""
        for verb in self.WRITE_VERBS:
            # 命中对 MES 写接口的调用（.verb( 形式，排除 _build_ 等内部方法）。
            if f".{verb}(" in source and "mes" in source.lower():
                raise StartupAssertionError(
                    f"摄入 handler {type(handler).__name__}.handle 含写 MES 调用 '{verb}'"
                    "（违反 B 只读摄入红线）"
                )
