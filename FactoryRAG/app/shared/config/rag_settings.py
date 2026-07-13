"""RagSettings 聚合各路线子配置 + 路线级开关。

路线级开关控制 router 注册与 consumer 启停（§4.2），支持灰度引入：先 B 再 A，E 收口。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.shared.config.base import BaseSettings


class TraceSettings(BaseModel):
    """A 追溯型子配置。"""

    enabled: bool = False
    subgraph_cache_ttl_seconds: int = 60


class DocSettings(BaseModel):
    """B 文档型子配置。"""

    enabled: bool = True
    retrieval_top_k: int = 20
    rerank_top_n: int = 5
    cache_ttl_seconds: int = 300


class AgenticSettings(BaseModel):
    """E Agentic 子配置。"""

    enabled: bool = False
    cache_ttl_seconds: int = 300


class RagSettings(BaseSettings):
    """聚合各路线子配置 + 路线级开关。

    路线开关 ``rag.<route>.enabled`` 控制 router 注册与 consumer 启停。
    灰度引入顺序：``document.enabled=true`` 先行，``traceability``/``agentic`` 灰度打开。
    """

    traceability: TraceSettings = Field(default_factory=TraceSettings)
    document: DocSettings = Field(default_factory=DocSettings)
    agentic: AgenticSettings = Field(default_factory=AgenticSettings)
