"""路线间调用的 Port（Protocol）。

E -> A/B、A -> B 的跨路线调用一律依赖此处的 Port，禁止直 import 对方
application/domain。Port 方法的参数/返回类型用字符串注解引用各路线领域模型
（TYPE_CHECKING），运行时不触发 import，保持 shared 不依赖 routes。

``tenant`` 显式传递（跨路线调用的租户上下文，过滤靠它）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # 仅类型检查用，避免 shared -> routes 运行时依赖
    from app.routes.document.domain.answer import ChunkHit, DocAnswer, DocQuery, DocSearch
    from app.routes.traceability.domain.answer import TraceAnswer
    from app.routes.traceability.domain.seed import ExpandRequest, TraceQuery
    from app.routes.traceability.domain.subgraph import TraceSubgraph
    from app.shared.tenant.context import TenantContext


@runtime_checkable
class TraceRagPort(Protocol):
    """A 追溯型的对外 Port。调用方：E（统一入口）、agent-service L1/L2。"""

    async def query(self, req: "TraceQuery", tenant: "TenantContext") -> "TraceAnswer":
        """子图检索 + LLM 综合，返回 TraceAnswer（含 subgraph_ref）。"""
        ...

    async def expand(self, req: "ExpandRequest", tenant: "TenantContext") -> "TraceSubgraph":
        """只取子图不综合，返回 TraceSubgraph。L2 不重查图，用此回查。"""
        ...


@runtime_checkable
class DocRagPort(Protocol):
    """B 文档型的对外 Port。调用方：A（suggested_action 拉 SOP）、E、agent-service L1/L2。"""

    async def query(self, req: "DocQuery", tenant: "TenantContext") -> "DocAnswer":
        """检索 + LLM 综合，返回 DocAnswer。"""
        ...

    async def search(self, req: "DocSearch", tenant: "TenantContext") -> list["ChunkHit"]:
        """只检索 chunks，返回 list[ChunkHit]。"""
        ...
