"""路线间调用的 Port（Protocol）。

E -> A/B、A -> B 的跨路线调用一律依赖此处的 Port，禁止直 import 对方
application/domain。**Port 方法只收原语**（str / datetime / list[str]），调用方
（A/E）零跨路线 import；各路线 DTO 由 InProcess Adapter 内部构造（见 adapters.py），
Http Adapter 把原语组装成端点 JSON。返回类型仍为各路线领域模型（调用方消费只读视图）。

跨路线契约的枚举值一律用 **枚举值字符串** 传递：
- seed kind：``"WipUnit" | "WorkOrder" | "InventoryBatch" | "Defect" | "Asset"``
- doc category：``"PROCESS_BOUND" | "ASSET_BOUND" | "GENERAL"``
- doc type：``"SOP" | "MANUAL" | "STANDARD"``

``tenant`` 显式传递（跨路线调用的租户上下文，过滤靠它）。
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # 仅类型检查用，避免 shared -> routes 运行时依赖
    from app.routes.document.domain.answer import ChunkHit, DocAnswer
    from app.routes.traceability.domain.answer import TraceAnswer
    from app.routes.traceability.domain.subgraph import TraceSubgraph
    from app.shared.tenant.context import TenantContext


@runtime_checkable
class TraceRagPort(Protocol):
    """A 追溯型的对外 Port。调用方：E（统一入口）、agent-service L1/L2。"""

    async def query(
        self,
        question: str,
        tenant: "TenantContext",
        *,
        seed_kind: str | None = None,
        seed_value: str | None = None,
        as_of: datetime | None = None,
        route_version: str | None = None,
    ) -> "TraceAnswer":
        """子图检索 + LLM 综合，返回 TraceAnswer（含 subgraph_ref）。"""
        ...

    async def expand(
        self,
        kind: str,
        value: str,
        tenant: "TenantContext",
        *,
        as_of: datetime | None = None,
        route_version: str | None = None,
    ) -> "TraceSubgraph":
        """只取子图不综合，返回 TraceSubgraph。L2 不重查图，用此回查。"""
        ...


@runtime_checkable
class DocRagPort(Protocol):
    """B 文档型的对外 Port。调用方：A（suggested_action 拉 SOP）、E、agent-service L1/L2。"""

    async def query(
        self,
        question: str,
        tenant: "TenantContext",
        *,
        route_version: str | None = None,
        doc_category: str | None = None,
        asset_id: str | None = None,
        doc_types: list[str] | None = None,
    ) -> "DocAnswer":
        """检索 + LLM 综合，返回 DocAnswer。"""
        ...

    async def search(
        self,
        query: str,
        tenant: "TenantContext",
        *,
        route_version: str | None = None,
        asset_id: str | None = None,
        doc_types: list[str] | None = None,
        top_k: int = 20,
    ) -> list["ChunkHit"]:
        """只检索 chunks，返回 list[ChunkHit]。"""
        ...
