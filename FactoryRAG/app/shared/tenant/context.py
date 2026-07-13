"""租户上下文（tenant_id + scopes: workshop/line 列表）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TenantContext(BaseModel):
    """租户上下文。A/B/E 共用，过滤逻辑各自实现（A Cypher WHERE / B ChromaDB where）。"""

    tenant_id: str = Field(description="租户 ID")
    tenant_scopes: list[str] = Field(
        default_factory=list,
        description="作用域列表，如 ['workshop:PCBA', 'line:SMT-1']",
    )

    def can_access(self, required_scopes: list[str]) -> bool:
        """作用域校验：required_scopes 任一命中即放行；空 required 放行。"""
        if not required_scopes:
            return True
        return any(s in self.tenant_scopes for s in required_scopes)

    def chroma_scopes(self) -> list[str]:
        """B ChromaDB where 过滤用的 scopes 列表。"""
        return self.tenant_scopes

    def headers(self) -> dict[str, str]:
        """出站 httpx 透传的租户 header。"""
        return {
            "X-Tenant-Id": self.tenant_id,
            "X-Tenant-Scope": ",".join(self.tenant_scopes),
        }
