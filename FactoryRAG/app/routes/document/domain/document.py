"""B 聚合根：KnowledgeDocument / DocumentVersion / DocumentBinding + 枚举。

审核流（决策 #3）：工艺绑定型文档随 ``ProcessRouteActivated`` **联动 PUBLISHED**，
去掉 SUBMITTED/PENDING_REBIND 中间态；通用知识型/设备绑定型仍走独立 DRAFT->PUBLISHED。
版本绑定通用化：``DocumentBinding`` 经 ``get_version_anchor()`` 产出统一 ``VersionAnchor``
（route/bom/rule/asset/standard），MVP 工艺绑定型按 route，决策 #2 预留 rule 双轨。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.shared.events.version_contract import VersionAnchor, VersionKind


class BindingType(str, Enum):
    """文档绑定类型。决定 ``get_version_anchor()`` 产出的 ``VersionKind``。"""

    ROUTE_VERSION = "ROUTE_VERSION"     # 工艺绑定型：SOP/检验标准 -> anchor(kind=route)
    RULE_VERSION = "RULE_VERSION"       # 质量门规则绑定（决策 #2 双轨，评测后切换）-> anchor(kind=rule)
    ASSET = "ASSET"                     # 设备绑定型：维修手册 -> anchor(kind=asset)
    ASSET_MODEL = "ASSET_MODEL"         # 设备型号绑定 -> anchor(kind=asset)
    STANDARD_VERSION = "STANDARD_VERSION"  # 通用标准绑定：IPC/ESD -> anchor(kind=standard)


# 绑定类型 -> 版本锚点 kind
_BINDING_KIND = {
    BindingType.ROUTE_VERSION: VersionKind.ROUTE,
    BindingType.RULE_VERSION: VersionKind.RULE,
    BindingType.ASSET: VersionKind.ASSET,
    BindingType.ASSET_MODEL: VersionKind.ASSET,
    BindingType.STANDARD_VERSION: VersionKind.STANDARD,
}

# 各绑定类型在 target_ref 中的 ref_id 键 / version 键
_REF_KEY = {
    BindingType.ROUTE_VERSION: "route_id",
    BindingType.RULE_VERSION: "rule_id",
    BindingType.ASSET: "asset_id",
    BindingType.ASSET_MODEL: "asset_model_id",
    BindingType.STANDARD_VERSION: "standard_id",
}
_VERSION_KEY = {
    BindingType.ROUTE_VERSION: "route_version",
    BindingType.RULE_VERSION: "rule_version",
    BindingType.ASSET: "asset_version",
    BindingType.ASSET_MODEL: "asset_model_version",
    BindingType.STANDARD_VERSION: "standard_version",
}


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

    ``target_ref`` 含被绑定目标的 ref_id + version（键随 ``binding_type`` 走，见
    ``_REF_KEY``/``_VERSION_KEY``）。inherited=True 表示继承自上一版本。
    ASSET/STANDARD 的 version 键可选（设备手册/标准可不带版本）。
    """

    binding_type: BindingType
    target_ref: dict[str, str] = Field(default_factory=dict)
    inherited: bool = False

    @model_validator(mode="after")
    def _validate_target_ref(self) -> "DocumentBinding":
        ref_key = _REF_KEY.get(self.binding_type)
        if ref_key and ref_key not in self.target_ref:
            raise ValueError(f"{self.binding_type.value} 绑定必须含 {ref_key}")
        return self

    def version_anchor(self) -> VersionAnchor | None:
        """从本绑定构造版本锚点；无 version 返回 None（如未带版本的 ASSET 绑定）。"""
        kind = _BINDING_KIND.get(self.binding_type)
        if kind is None:
            return None
        ref_key = _REF_KEY[self.binding_type]
        ver_key = _VERSION_KEY[self.binding_type]
        ref_id = self.target_ref.get(ref_key, "")
        version = self.target_ref.get(ver_key, "")
        if not version:
            return None
        return VersionAnchor(kind=kind, ref_id=ref_id, version=version)

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

    def get_version_anchor(self) -> VersionAnchor | None:
        """从 bindings 提取第一个有效版本锚点（chunk metadata 同步用）。

        优先级按 bindings 顺序；工艺绑定型取 ROUTE/RULE，设备绑定型取 ASSET，通用标准取 STANDARD。
        """
        for b in self.bindings:
            anchor = b.version_anchor()
            if anchor is not None:
                return anchor
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

    def get_version_by_anchor(self, anchor: VersionAnchor) -> DocumentVersion | None:
        """按版本锚点查版本（同 kind+version）。"""
        for v in self.versions:
            va = v.get_version_anchor()
            if va is not None and va.kind == anchor.kind and va.version == anchor.version:
                return v
        return None

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
