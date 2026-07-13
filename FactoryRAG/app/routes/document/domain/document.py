"""B 聚合根：KnowledgeDocument / DocumentVersion / DocumentBinding + 枚举。

审核流（决策 #3）：工艺绑定型文档随 ``ProcessRouteActivated`` **联动 PUBLISHED**，
去掉 SUBMITTED/PENDING_REBIND 中间态；通用知识型/设备绑定型仍走独立 DRAFT->PUBLISHED。
版本绑定（决策 #2）：MVP 按 route_version，``DocumentBinding`` 预留 rule_id+rule_version 双轨字段。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class BindingType(str, Enum):
    """文档绑定类型。"""

    ROUTE_VERSION = "ROUTE_VERSION"     # 工艺绑定型：SOP/检验标准
    RULE_VERSION = "RULE_VERSION"       # 质量门规则绑定（决策 #2 双轨，评测后切换）
    ASSET = "ASSET"                     # 设备绑定型：维修手册
    ASSET_MODEL = "ASSET_MODEL"


class DocumentCategory(str, Enum):
    """文档大类。"""

    PROCESS_BOUND = "PROCESS_BOUND"     # 工艺绑定型：SOP/检验标准（决策 #3 联动 PUBLISHED）
    ASSET_BOUND = "ASSET_BOUND"         # 设备绑定型：维修手册
    GENERAL = "GENERAL"                 # 通用知识型：IPC 标准/培训资料


class DocType(str, Enum):
    """文档类型（决定切分策略）。"""

    SOP = "SOP"
    MANUAL = "MANUAL"
    STANDARD = "STANDARD"


class VersionState(str, Enum):
    """文档版本状态机。

    决策 #3 后无 SUBMITTED/PENDING_REBIND：工艺绑定型 DRAFT->PUBLISHED(auto)。
    """

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"


class DocumentSource(BaseModel):
    """文档来源（值对象）。"""

    source_type: str = "UPLOAD"          # UPLOAD | SYNC
    origin_system: str | None = None


class DocumentBinding(BaseModel):
    """文档绑定（值对象）。

    决策 #2 双轨：``target_ref`` 含 route_id+route_version（MVP）或
    rule_id+rule_version（评测后切换）。inherited=True 表示继承自上一版本。
    """

    binding_type: BindingType
    target_ref: dict[str, str] = Field(default_factory=dict)
    inherited: bool = False

    @model_validator(mode="after")
    def _validate_target_ref(self) -> "DocumentBinding":
        if self.binding_type == BindingType.ROUTE_VERSION:
            if "route_id" not in self.target_ref or "route_version" not in self.target_ref:
                raise ValueError("ROUTE_VERSION 绑定必须含 route_id + route_version")
        elif self.binding_type == BindingType.RULE_VERSION:
            if "rule_id" not in self.target_ref or "rule_version" not in self.target_ref:
                raise ValueError("RULE_VERSION 绑定必须含 rule_id + rule_version（决策 #2 双轨）")
        elif self.binding_type in (BindingType.ASSET, BindingType.ASSET_MODEL):
            if "asset_id" not in self.target_ref:
                raise ValueError("ASSET 绑定必须含 asset_id")
        return self

    @property
    def route_version(self) -> str | None:
        return self.target_ref.get("route_version")

    @property
    def route_id(self) -> str | None:
        return self.target_ref.get("route_id")

    @property
    def rule_version(self) -> str | None:
        return self.target_ref.get("rule_version")

    def binding_key(self) -> tuple[BindingType, str]:
        """同类绑定等价键（用于"同类绑定同时最多一个 PUBLISHED"不变式）。"""
        ref = "|".join(f"{k}={v}" for k, v in sorted(self.target_ref.items()))
        return (self.binding_type, ref)


class DocumentVersion(BaseModel):
    """文档版本（聚合内实体）。

    不变式：
    - ``file_content_hash`` 全局唯一（MySQL ``uk_hash``，摄入幂等键）；
    - PUBLISHED -> DEPRECATED 时设置 ``deprecated_at``；
    - 工艺绑定型：``ProcessRouteActivated`` 触发直接 PUBLISHED（决策 #3，无 SUBMITTED）；
    - 新版本 PUBLISHED 时，同 document 同类绑定的旧 PUBLISHED -> DEPRECATED。
    """

    version_id: str
    document_id: str
    version_no: str
    state: VersionState = VersionState.DRAFT
    source: DocumentSource = Field(default_factory=DocumentSource)
    file_ref: str                           # MinIO URI: rag-docs/{doc_id}/{version_id}/raw
    file_content_hash: str                  # SHA-256，幂等键
    bindings: list[DocumentBinding] = Field(default_factory=list)
    effective_at: datetime | None = None
    deprecated_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def publish(self) -> None:
        """置 PUBLISHED。工艺绑定型由 ReindexCoordinator 在 ProcessRouteActivated 时调用（决策 #3）。"""
        self.state = VersionState.PUBLISHED
        self.effective_at = datetime.now(timezone.utc)

    def deprecate(self) -> None:
        self.state = VersionState.DEPRECATED
        self.deprecated_at = datetime.now(timezone.utc)

    def archive(self) -> None:
        self.state = VersionState.ARCHIVED

    def add_binding(self, binding: DocumentBinding) -> None:
        self.bindings.append(binding)

    def get_route_version(self) -> str | None:
        """从 bindings 提取 route_version（chunk metadata 同步用）。"""
        for b in self.bindings:
            if b.binding_type == BindingType.ROUTE_VERSION and b.route_version:
                return b.route_version
        return None

    def get_route_id(self) -> str | None:
        for b in self.bindings:
            if b.binding_type == BindingType.ROUTE_VERSION and b.route_id:
                return b.route_id
        return None

    def get_asset_id(self) -> str | None:
        for b in self.bindings:
            if b.binding_type in (BindingType.ASSET, BindingType.ASSET_MODEL):
                return b.target_ref.get("asset_id")
        return None


class KnowledgeDocument(BaseModel):
    """文档聚合根。

    不变式：同一 document_id 下，同类绑定（binding_type + target_ref 等价）同时最多一个 PUBLISHED。
    """

    document_id: str
    doc_type: DocType
    title: str
    category: DocumentCategory
    tenant_scope: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    versions: list[DocumentVersion] = Field(default_factory=list)

    def add_version(self, version: DocumentVersion) -> None:
        self.versions.append(version)

    def get_published_version(self) -> DocumentVersion | None:
        return next((v for v in self.versions if v.state == VersionState.PUBLISHED), None)

    def get_version_by_route(self, route_version: str) -> DocumentVersion | None:
        return next(
            (v for v in self.versions if v.get_route_version() == route_version),
            None,
        )

    def publish_version(self, version: DocumentVersion) -> list[DocumentVersion]:
        """发布某版本，并把同类绑定的旧 PUBLISHED 置 DEPRECATED。

        返回被降级的旧版本列表（供应用层持久化）。
        """
        deprecated: list[DocumentVersion] = []
        target_keys = {b.binding_key() for b in version.bindings}
        for v in self.versions:
            if v is version:
                continue
            if v.state != VersionState.PUBLISHED:
                continue
            if any(b.binding_key() in target_keys for b in v.bindings):
                v.deprecate()
                deprecated.append(v)
        version.publish()
        return deprecated

    def enforce_category_invariant(self) -> None:
        """工艺绑定型：所有 PUBLISHED 版本必须至少有一个 ROUTE_VERSION/RULE_VERSION 绑定。"""
        if self.category != DocumentCategory.PROCESS_BOUND:
            return
        for v in self.versions:
            if v.state != VersionState.PUBLISHED:
                continue
            if not any(
                b.binding_type in (BindingType.ROUTE_VERSION, BindingType.RULE_VERSION)
                for b in v.bindings
            ):
                raise ValueError(
                    f"工艺绑定型文档版本 {v.version_id} PUBLISHED 但无版本绑定（违反不变式）"
                )
