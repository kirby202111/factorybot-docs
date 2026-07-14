"""shared/events -- 版本契约。

通用 ``VersionAnchor(kind, ref_id, version)`` 是贯穿 B/A/共享内核的统一版本锚点：
物理锁定某次生产/判定时使用的具体版本。``route_version``/``bom_version``/``rule_version``/
``asset_version``/``standard_version`` 都是它的具化（``VersionKind``）。
``ProcessRouteActivated`` 等升版事件驱动的版本失效 -> A 重投图 / B 重索引的统一入口。

版本一致性三段传递链（核心安全契约）：
图 ``SNAPSHOT_OF_{kind}{version}`` -> L1 ``evidence.version_anchor``
-> L2 ``Draft.version_anchor`` -> MES 应用服务校验 ACTIVE。
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
    """版本锚点类型。值即 ChromaDB metadata ``version_kind`` 字段与图边属性前缀。"""

    ROUTE = "route"        # 工艺路线版本（PROCESS_BOUND SOP/检验标准，核心安全契约）
    BOM = "bom"            # 物料清单版本
    RULE = "rule"          # 质量门规则版本（决策 #2，评测后切换为检验标准锚点）
    ASSET = "asset"        # 设备资产版本（ASSET_BOUND 维修手册）
    STANDARD = "standard"  # 通用标准版本（GENERAL IPC/ESD）


class VersionAnchor(BaseModel):
    """版本锚点：物理锁定某次生产/判定时使用的具体版本。

    - ``kind``：版本类型（route/bom/rule/asset/standard）。
    - ``ref_id``：被绑定的目标 ID（route_id / bom_id / rule_id / asset_id / standard_id）。
    - ``version``：具体版本号（v3 / RevH）。

    chunk metadata 扁平存 ``version_kind``/``version_ref_id``/``version`` 三字段（见
    ``to_metadata``）；图快照边属性用 ``{kind}_version``（见 ``as_edge_attr``，保持图 schema 兼容）。
    """

    kind: VersionKind
    ref_id: str = Field(description="route_id / bom_id / rule_id / asset_id / standard_id")
    version: str = Field(description="具体版本号，如 v3 / RevH")

    @property
    def is_bound(self) -> bool:
        """是否携带有效版本号（ref_id 可空，但 version 非空才算锁定）。"""
        return bool(self.version)

    def as_edge_attr(self) -> dict[str, str]:
        """作为图快照边属性的字典形式。

        ``SNAPSHOT_OF_ROUTE`` 边带 ``{route_version: v3}``、``SNAPSHOT_OF_BOM`` 带
        ``{bom_version: v2}`` —— 属性名随边类型走，保持 Neo4j 图 schema 不变。
        """
        return {f"{self.kind.value}_version": self.version}

    def to_metadata(self) -> dict[str, str]:
        """ChromaDB chunk metadata 扁平三字段（写入后不可变）。"""
        return {
            "version_kind": self.kind.value,
            "version_ref_id": self.ref_id,
            "version": self.version,
        }

    @classmethod
    def from_metadata(cls, meta: dict[str, Any]) -> "VersionAnchor | None":
        """从 ChromaDB metadata 重建锚点；无 kind/version 返回 None。"""
        kind_v = meta.get("version_kind") or ""
        ver = meta.get("version") or ""
        ref = meta.get("version_ref_id") or ""
        if not kind_v or not ver:
            return None
        try:
            return cls(kind=VersionKind(kind_v), ref_id=ref, version=ver)
        except ValueError:
            return None


class ReindexRequest(BaseModel):
    """``rag.reindex.request`` 内部事件（A 升版 -> B 重索引）。

    A 在 ``ProcessRouteActivated``（等升版事件）时发布，B 的 ``ReindexCoordinator`` 消费，
    按 ``anchor`` 重新摄入关联文档（从 MinIO 拉原始文件）。chunk 不可变使重索引幂等
    （chunk_id 不变，ChromaDB upsert 无副作用）。
    """

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str = "rag.reindex.request"
    anchor: VersionAnchor
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
            "payload": {"anchor": self.anchor.model_dump(mode="json")},
        }


def parse_anchor(kind: VersionKind, ref_id: str, version: str) -> VersionAnchor:
    """版本锚点解析入口。"""
    if not version:
        raise ValueError(f"{kind.value} 版本锚点缺失：version 不可为空（安全契约）")
    return VersionAnchor(kind=kind, ref_id=ref_id, version=version)
