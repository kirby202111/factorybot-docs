"""E 工具注册表 + ReadOnlyToolGate。

``ToolRegistry`` 注册 A/B 只读工具；``ReadOnlyToolGate`` 拒绝注册 ``read_only=False``
的工具（E 的只读红线）。L1/L2 不作为工具注册，由 ``SubAgentDelegator`` 直接委托。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from app.shared.acl.gates import StartupAssertionError


class ToolDescriptor(BaseModel):
    """工具描述符。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    route: str                                    # "A" | "B"
    read_only: bool                               # E 强制 True
    args_schema: type | None = None
    required_tenant_scopes: list[str] = []
    handler: Callable[..., Awaitable[Any]] | None = None


class ReadOnlyToolGate(StartupAssertionError):
    """启动断言：发现非只读工具，拒绝启动。"""


class ToolRegistry:
    """A/B 工具注册表。``ReadOnlyToolGate`` 在注册/启动期拒绝非只读工具。"""

    def __init__(self) -> None:
        self._descriptors: dict[str, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        if not descriptor.read_only:
            raise ReadOnlyToolGate(
                f"工具 '{descriptor.name}' read_only=False，拒绝注册（E 只读红线）"
            )
        self._descriptors[descriptor.name] = descriptor

    def get(self, name: str) -> ToolDescriptor | None:
        return self._descriptors.get(name)

    def all_descriptors(self) -> list[ToolDescriptor]:
        return list(self._descriptors.values())

    def validate_on_startup(self) -> None:
        """启动期断言：所有已注册工具必须 read_only=True。"""
        for name, desc in self._descriptors.items():
            if not desc.read_only:
                raise ReadOnlyToolGate(f"工具 '{name}' read_only=False，拒绝启动")

    def assert_on(self, gateway: Any) -> None:
        """供 lifespan 统一调度：扫描 gateway.tool_registry。"""
        registry = getattr(gateway, "tool_registry", None)
        if registry is None:
            return
        registry.validate_on_startup()

    def build_default(self, *, trace_rag_port: Any, doc_rag_port: Any) -> None:
        """注册 E 默认工具：A ``query_traceability_graph`` + B ``search_docs``。

        工具 handler 只传原语给 Port（路线间禁止直 import 对方 domain）：
        - seed kind 用枚举值字符串 ``"WipUnit"`` 等（TraceRagPort 契约）；
        - doc types 用枚举值字符串列表 ``["SOP"]`` 等（DocRagPort 契约）。
        """
        from datetime import datetime

        async def _query_traceability_graph(
            *, seed_value: str, seed_kind: str, tenant: Any, as_of: datetime | None = None
        ) -> Any:
            # 经 TraceRagPort InProcess 调 A（决策 #4，不走本机 REST）
            return await trace_rag_port.expand(
                seed_kind, seed_value, tenant, as_of=as_of
            )

        async def _search_docs(
            *, query: str, route_version: str, tenant: Any, doc_types: list[str] | None = None
        ) -> Any:
            # 经 DocRagPort InProcess 调 B（决策 #4）
            return await doc_rag_port.search(
                query, tenant, route_version=route_version or None, doc_types=doc_types
            )

        self.register(
            ToolDescriptor(
                name="query_traceability_graph",
                description="查询追溯子图：给定 SN/批次/工单，返回全链路节点与边",
                route="A",
                read_only=True,
                required_tenant_scopes=["trace:read"],
                handler=_query_traceability_graph,
            )
        )
        self.register(
            ToolDescriptor(
                name="search_docs",
                description="检索 SOP/手册/标准文档：按关键词返回匹配片段",
                route="B",
                read_only=True,
                required_tenant_scopes=["doc:read"],
                handler=_search_docs,
            )
        )
