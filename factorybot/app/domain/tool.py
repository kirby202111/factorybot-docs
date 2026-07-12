"""工具注册与三层写防线 (ReadOnlyToolGate / WriteToolGate)。

工具边界即限界上下文边界：Agent 能调的工具 = 14 个上下文暴露的 toolset。
三层防线：
- L1 ReadOnlyToolGate：注册期 + 启动期双重断言，非只读工具拒绝注册。
- L2 NoWriteClientGate：见 domain/gate.py（扫描 ACL client 方法名禁写动词）。
- L3 WriteToolGate：写工具必须声明 requires_confirmation + writes_via；supervisor 不持工具。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from app.domain.tenant import TenantContext


class ToolGateError(RuntimeError):
    """工具注册/启动断言失败。"""


class ReadOnlyToolGate(ToolGateError):
    """L1 禁止注册非只读工具。"""


class WriteToolGate(ToolGateError):
    """L3 写工具未声明 requires_confirmation / writes_via。"""


# 工具处理器签名：(args, tenant) -> view(dict)
ToolHandler = Callable[..., Awaitable[Any]]


class ToolDescriptor(BaseModel):
    """工具描述符。注册到 ToolRegistry，对齐限界上下文边界。"""

    name: str
    description: str
    bounded_context: str            # 如 "过点执行上下文" / "工艺管理上下文"
    capability: str = "supervisor"  # supervisor | root_cause | fault_impact | traceability | draft_* | l1
    read_only: bool = True
    requires_confirmation: bool = False   # 写工具必须 True
    writes_via: Optional[str] = None      # 写落库走哪个上下文应用服务，如 "工艺管理上下文.application.activate_route"
    required_tenant_scopes: list[str] = Field(default_factory=list)
    # args_schema 是 Pydantic 类型，运行时校验工具入参
    args_schema: Any = None
    handler: Any = None  # ToolHandler，不参与序列化

    model_config = {"arbitrary_types_allowed": True}


class ToolRegistry:
    """工具注册表。承载三层写防线的注册期断言。"""

    def __init__(self, *, level: str = "L1") -> None:
        # level: "L1" 注册期拒绝写工具；"L3" 允许写工具但强制声明
        self._level = level
        self._descriptors: dict[str, ToolDescriptor] = {}

    # ---- 注册 ----
    def register(self, d: ToolDescriptor) -> None:
        if self._level == "L1":
            # L1 注册期断言：非只读工具直接拒绝
            if not d.read_only:
                raise ReadOnlyToolGate(f"L1 Agent 禁止注册非只读工具: {d.name}")
        else:
            # L3：写工具必须声明 requires_confirmation + writes_via
            if not d.read_only:
                if not d.requires_confirmation:
                    raise WriteToolGate(f"写工具必须 requires_confirmation=True: {d.name}")
                if not d.writes_via:
                    raise WriteToolGate(f"写工具必须声明 writes_via: {d.name}")
        self._descriptors[d.name] = d

    # ---- 查询 ----
    def get(self, name: str) -> Optional[ToolDescriptor]:
        return self._descriptors.get(name)

    def all(self) -> list[ToolDescriptor]:
        return list(self._descriptors.values())

    def capabilities(self) -> list[str]:
        return sorted({d.capability for d in self._descriptors.values()})

    def tools_for(self, capability: str, tenant: TenantContext) -> list[ToolDescriptor]:
        """按 capability + 租户 scope 过滤工具集。"""
        return [
            d for d in self._descriptors.values()
            if d.capability == capability and tenant.can_access(d.required_tenant_scopes)
        ]

    # ---- 启动断言 ----
    def validate_on_startup(self, *, supervisor_capability: str = "supervisor") -> None:
        """启动期断言（双重保险）。

        - L1：再次确认全部只读。
        - L3：写工具声明完整；supervisor 不持工具；禁止放行/拦截类工具；capability 互斥。
        """
        forbidden_prefixes = ("pass_judge", "force_release", "release_", "block_", "intercept_")
        for d in self._descriptors.values():
            if self._level == "L1" and not d.read_only:
                raise ReadOnlyToolGate(f"启动断言失败：非只读工具混入: {d.name}")
            if not d.read_only:
                assert d.requires_confirmation, f"写工具 {d.name} 必须 requires_confirmation=True"
                assert d.writes_via, f"写工具 {d.name} 必须声明 writes_via"
            for p in forbidden_prefixes:
                assert not d.name.startswith(p), (
                    f"禁止注册放行/拦截类工具: {d.name}"
                )
        if self._level == "L3":
            # supervisor 编排器本身不持任何工具（纯代码）
            sup_tools = [d for d in self._descriptors.values() if d.capability == supervisor_capability]
            assert not sup_tools, f"supervisor 不应持有任何工具: {[t.name for t in sup_tools]}"
            self._assert_capability_partition()

    def _assert_capability_partition(self) -> None:
        """各 capability 工具集互斥：一个工具只能归属一个 capability。"""
        seen: dict[str, str] = {}
        for d in self._descriptors.values():
            prev = seen.get(d.name)
            if prev and prev != d.capability:
                raise WriteToolGate(f"工具 {d.name} 同时归属 {prev} 与 {d.capability}")
            seen[d.name] = d.capability
