"""版本锚点值对象（agent-service 自持契约副本）。

通用 ``VersionAnchor(kind, ref_id, version)`` 是贯穿 L1/L2 版本一致性三段链的统一版本锚点：
物理锁定某次诊断/草拟时使用的具体版本。``route_version``/``bom_version``/``rule_version``/
``asset_version``/``standard_version`` 都是它的具化（``VersionKind``）。

factorybot 是独立微服务，不导入 FactoryRAG；此模块镜像 RAG 侧
``shared/events/version_contract.py`` 的 ``VersionAnchor``（不含 ChromaDB/图专属的
``to_metadata``/``as_edge_attr``）。领域链实体（DiagnosisReport/Draft/DiagnosisSession/
TraceGraphView/DocSearchHit）持扁平 ``version``/``version_kind``/``version_ref_id`` 三字段 +
``version_anchor()`` 属性构造本对象；ACL 方法收 ``VersionAnchor|None``。

版本一致性三段传递链（核心安全契约）：
图 ``SNAPSHOT_OF_{kind}{version}`` (RAG) -> L1 ``DiagnosisReport.version`` ->
L2 ``Draft.version`` -> MES 应用服务校验 ACTIVE (``process_management.py``，route-specific)。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class VersionKind(str, Enum):
    """版本锚点类型。值与 RAG 侧 ``VersionKind`` 一致。"""

    ROUTE = "route"        # 工艺路线版本（PROCESS_BOUND SOP/检验标准，核心安全契约）
    BOM = "bom"            # 物料清单版本
    RULE = "rule"          # 质量门规则版本
    ASSET = "asset"        # 设备资产版本（ASSET_BOUND 维修手册）
    STANDARD = "standard"  # 通用标准版本（GENERAL IPC/ESD）


class VersionAnchor(BaseModel):
    """版本锚点：物理锁定某次诊断/草拟时使用的具体版本。

    - ``kind``：版本类型（route/bom/rule/asset/standard）。
    - ``ref_id``：被绑定的目标 ID（route_id / bom_id / rule_id / asset_id / standard_id）。
    - ``version``：具体版本号（v3 / RevH）。
    """

    kind: VersionKind
    ref_id: str = ""
    version: str

    @property
    def is_bound(self) -> bool:
        """是否携带有效版本号（version 非空才算锁定）。"""
        return bool(self.version)

    def to_flat(self) -> dict[str, str | None]:
        """扁平三字段（领域实体 / RAG API 请求 DTO 形状）。"""
        return {
            "version": self.version,
            "version_kind": self.kind.value,
            "version_ref_id": self.ref_id,
        }

    @classmethod
    def from_flat(
        cls, version: str | None, version_kind: str | None,
        version_ref_id: str | None = None,
    ) -> "VersionAnchor | None":
        """从扁平三字段构造锚点；无 kind/version 返回 None。"""
        kind_v = version_kind or ""
        ver = version or ""
        if not kind_v or not ver:
            return None
        try:
            return cls(
                kind=VersionKind(kind_v),
                ref_id=version_ref_id or "",
                version=ver,
            )
        except ValueError:
            return None
