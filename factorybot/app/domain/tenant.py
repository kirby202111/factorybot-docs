"""租户上下文：随会话流动，工具调用前按 scope 过滤。

对齐 14 个限界上下文暴露的 toolset 边界--Agent 能调的工具 = 上下文暴露的 toolset，
权限在调用前按 TenantContext 过滤（引入路线 §4 / 诊断 §4）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TenantContext(BaseModel):
    """不可变租户上下文。所有 ACL 调用都会带上其 headers。"""

    tenant_id: str
    workshop: str
    line: str = ""
    role: str = "ENGINEER"
    user_id: str = "system"
    scopes: list[str] = Field(default_factory=list)

    def can_access(self, required_scopes: list[str]) -> bool:
        """所需 scope 必须全部满足。空 required_scopes 视为公开。"""
        if not required_scopes:
            return True
        return all(s in self.scopes for s in required_scopes)

    def headers(self) -> dict[str, str]:
        """出站 REST 请求头，ACL 客户端统一注入。"""
        return {
            "X-Tenant-Id": self.tenant_id,
            "X-Tenant-Workshop": self.workshop,
            "X-Tenant-Line": self.line,
            "X-Tenant-Role": self.role,
            "X-Tenant-User-Id": self.user_id,
        }

    @classmethod
    def default(cls, tenant_id: str = "WS-A", workshop: str = "SMT-1", line: str = "L-01") -> "TenantContext":
        """mock 场景的示例租户，拥有全部只读 scope。

        WS = WorkShop（车间）；WS-A/WS-B 为示例车间代号（多租户隔离边界=车间），
        生产环境换真实车间 ID。
        """
        return cls(
            tenant_id=tenant_id,
            workshop=workshop,
            line=line,
            role="ENGINEER",
            user_id="u_zhang",
            scopes=[
                "pass:read", "workorder:read", "process:read", "material:read",
                "wip:read", "device:read", "equipment:read", "rework:read",
                "quality:read", "tooling:read", "rag:read", "doc:read",
                "rework:write", "process:write", "pass:write",
            ],
        )
