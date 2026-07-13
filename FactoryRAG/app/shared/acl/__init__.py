"""shared/acl -- ACL 基类 + 路线间 Port/Adapter + MES 只读客户端。

这是可拆性的核心：
- 对 MES 只读 REST 的 ACL（出站）-- ``BaseReadonlyAclClient`` + ``MesClients``；
- 路线间调用的 Port/Adapter -- ``TraceRagPort``/``DocRagPort`` + InProcess/Http Adapter。

规则：路线间**禁止直接 import 对方的 application/domain**，一律依赖本包的 Port。
单服务模式下 DI 容器注入 InProcess Adapter；拆服务只需把绑定换成 Http Adapter。

口径见《rag-service-整体结构设计》§3.6、《技术选型和实现方案》§2.6。
"""
from app.shared.acl.adapters import (
    HttpDocRagAdapter,
    HttpTraceRagAdapter,
    InProcessDocRagAdapter,
    InProcessTraceRagAdapter,
)
from app.shared.acl.base_client import BaseReadonlyAclClient
from app.shared.acl.gates import ReadOnlyAclGate, StartupAssertionError
from app.shared.acl.mes_clients import (
    CheckpointAclClient,
    MesClients,
    ProcessManagementAclClient,
)
from app.shared.acl.ports import DocRagPort, TraceRagPort

__all__ = [
    "BaseReadonlyAclClient",
    "ReadOnlyAclGate",
    "StartupAssertionError",
    "TraceRagPort",
    "DocRagPort",
    "InProcessTraceRagAdapter",
    "HttpTraceRagAdapter",
    "InProcessDocRagAdapter",
    "HttpDocRagAdapter",
    "MesClients",
    "ProcessManagementAclClient",
    "CheckpointAclClient",
]
