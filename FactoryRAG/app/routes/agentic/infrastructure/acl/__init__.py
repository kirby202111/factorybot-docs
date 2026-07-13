"""E 路线 ACL：L1/L2 委托客户端（httpx REST，透传 traceparent）。

A/B 调用经 shared ``TraceRagPort``/``DocRagPort`` InProcess Adapter（决策 #4，不走本机 REST），
不在此处；L1/L2 是 agent-service（独立服务），用 httpx 委托。
"""
from app.routes.agentic.infrastructure.acl.l1_delegation import L1DelegationClient
from app.routes.agentic.infrastructure.acl.l2_delegation import L2DelegationClient

__all__ = ["L1DelegationClient", "L2DelegationClient"]
