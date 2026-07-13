"""shared/events -- 版本契约。

``route_version``/``bom_version``/``rule_version`` 三类版本锚点定义；
``ProcessRouteActivated`` 驱动的版本失效事件 -> A 重投图 / B 重索引的统一入口。

版本一致性三段传递链（核心安全契约）：
图 ``SNAPSHOT_OF_ROUTE{route_version}`` -> L1 ``evidence.route_version``
-> L2 ``Draft.route_version`` -> MES 应用服务校验 ACTIVE。
rag-service 侧负责第一段（图用快照边物理锁定版本）+ 发布 ``rag.reindex.request``
内部事件通知 B。

口径见《rag-service-整体结构设计》§3.10、《技术选型和实现方案》§2.10/§8.4。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class VersionKind(str, Enum):
    """三类版本锚点。"""

    ROUTE = "route_version"      # 工艺路线版本（核心安全契约）
    BOM = "bom_version"          # 物料清单版本
    RULE = "rule_version"        # 质量门规则版本（决策 #2，评测后切换为检验标准锚点）


class VersionAnchor(BaseModel):
    """版本锚点：物理锁定某次生产/判定时使用的具体版本。"""

    kind: VersionKind
    ref_id: str = Field(description="route_id / bom_id / rule_id")
    version: str = Field(description="具体版本号，如 v3")

    def as_edge_attr(self) -> dict[str, str]:
        """作为图边属性的字典形式（SNAPSHOT_OF_ROUTE{route_version} 等）。"""
        return {self.kind.value: self.version}


class ReindexRequest(BaseModel):
    """``rag.reindex.request`` 内部事件（A 升版 -> B 重索引）。

    A 在 ``ProcessRouteActivated`` 时发布，B 的 ``ReindexCoordinator`` 消费，
    按 ``route_id`` + ``route_version`` 重新摄入关联文档（从 MinIO 拉原始文件）。
    chunk 不可变使重索引幂等（chunk_id 不变，ChromaDB upsert 无副作用）。
    """

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str = "rag.reindex.request"
    route_id: str
    route_version: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_service: str = "rag-service.traceability"
    trace_id: str = ""

    def as_kafka_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "source_service": self.source_service,
            "trace_id": self.trace_id,
            "payload": {"route_id": self.route_id, "route_version": self.route_version},
        }


def parse_anchor(kind: VersionKind, ref_id: str, version: str) -> VersionAnchor:
    """版本锚点解析入口。"""
    if not version:
        raise ValueError(f"{kind.value} 缺失：版本锚点不可为空（安全契约）")
    return VersionAnchor(kind=kind, ref_id=ref_id, version=version)
