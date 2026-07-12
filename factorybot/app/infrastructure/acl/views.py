"""ACL View 模型 + 外部 DTO + Mapper。

View = 内部防腐层视图（Agent 看到的）；DTO = 外部 REST 返回的原始结构。
Mapper 把 DTO -> View，隔离外部契约变化。mock 模式下 fixtures 直接产出 View 形状
（mapper 透传），真实模式下 mapper 做字段映射。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ===========================================================================
# 过点执行上下文
# ===========================================================================
class PassRecordView(BaseModel):
    sn: str
    work_order_id: str
    station_id: str
    equipment_id: str
    route_version: str
    decision: str                     # PASS | BLOCK
    blocking_reason: str | None = None
    scanned_by: str = ""
    occurred_at: str = ""


class TestResultView(BaseModel):
    test_id: str
    sn: str
    station_id: str
    test_type: str
    raw_verdict: str                  # PASS | FAIL
    measured_items: list[dict] = Field(default_factory=list)
    source_ts: str = ""


# ===========================================================================
# 工单管理上下文
# ===========================================================================
class WorkOrderView(BaseModel):
    wo_id: str
    product_id: str
    status: str
    bom_version: str
    route_version: str
    planned_qty: int = 0
    completed_qty: int = 0
    line_id: str = ""


class WorkOrderProgressView(BaseModel):
    wo_id: str
    completed_qty: int = 0
    good_qty: int = 0
    bad_qty: int = 0
    reworked_qty: int = 0
    scrapped_qty: int = 0


class KitStatusView(BaseModel):
    wo_id: str
    kit_ready: bool = False
    kit_rate: int = 100
    missing_items: list[dict] = Field(default_factory=list)
    missing_material_ids: list[str] = Field(default_factory=list)
    checked_at: str = ""


class FirstArticleView(BaseModel):
    work_order_id: str
    status: str                       # PASS | FAIL | PENDING
    article_id: str = ""


# ===========================================================================
# 工艺管理上下文（route_version 强制 + status=ACTIVE 校验）
# ===========================================================================
class ToolingSpec(BaseModel):
    stencil_id: str = ""
    program_id: str = ""
    fixture_id: str = ""


class RouteStep(BaseModel):
    step_no: int
    operation_id: str
    station_type: str
    anti_error_items: list[str] = Field(default_factory=list)
    is_reentry_point: bool = False


class ProcessRouteView(BaseModel):
    route_id: str
    version: str
    product_id: str = ""
    route_type: str = ""
    status: str                       # 必须为 ACTIVE
    tooling: ToolingSpec = Field(default_factory=ToolingSpec)
    steps: list[RouteStep] = Field(default_factory=list)


class QualificationView(BaseModel):
    route_id: str
    version: str
    qualified: bool = True
    missing_qualifications: list[str] = Field(default_factory=list)


# ===========================================================================
# 物料上下文
# ===========================================================================
class MaterialBatchView(BaseModel):
    batch_no: str
    part_no: str
    material_name: str = ""
    category: str = ""
    supplier_id: str = ""
    lot_no: str = ""
    received_qty: int = 0
    available_qty: int = 0
    received_at: str = ""
    expiry_date: str | None = None


class BomVersionView(BaseModel):
    bom_id: str
    version: str
    product_id: str = ""
    bom_type: str = ""
    status: str = ""
    items: list[dict] = Field(default_factory=list)


class WipPositionView(BaseModel):
    sn: str
    current_station: str = ""
    current_step: int = 0
    status: str = ""
    work_order_id: str = ""
    last_updated: str = ""
    route_version: str = ""


class SupplierTraceView(BaseModel):
    batch_id: str
    supplier_id: str = ""
    received_at: str = ""
    upstream_batches: list[str] = Field(default_factory=list)


# ===========================================================================
# 设备数据接入 / 设备工装台账
# ===========================================================================
class DeviceParamsView(BaseModel):
    asset_id: str
    data_points: list[dict] = Field(default_factory=list)


class AssetStatusView(BaseModel):
    asset_id: str
    asset_kind: str = ""
    status: str = ""
    available: bool = True
    blocking_reasons: list[str] = Field(default_factory=list)
    last_updated: str = ""


# ===========================================================================
# 工装 / 设备遥测 / FMEA / 产品敏感度（L3 比对 + agent A/B 工具）
# ===========================================================================
class CurrentStencilView(BaseModel):
    asset_id: str
    stencil_id: str
    scanned_at: str = ""


class LocalProgramView(BaseModel):
    asset_id: str
    program_id: str
    last_updated: str = ""


class StencilLendingView(BaseModel):
    stencil_id: str
    current_status: str = ""            # LENT_OUT | IN_STOCK
    lent_to_asset: str = ""
    lent_by: str = ""
    lent_at: str = ""
    expected_return_at: str = ""
    returned_at: str | None = None


class ChangeoverCloseView(BaseModel):
    previous_work_order_id: str
    closed_at: str = ""
    stencil_return_triggered: bool = False
    stencil_id_at_close: str = ""


class EquipmentTelemetryView(BaseModel):
    asset_id: str
    window: list[str] = Field(default_factory=list)
    series: list[dict] = Field(default_factory=list)


class ProcessFmeaView(BaseModel):
    asset_id: str
    fmea_entries: list[dict] = Field(default_factory=list)


class ProductSensitivityView(BaseModel):
    batches: list[dict] = Field(default_factory=list)


# ===========================================================================
# 返修 / 返工上下文
# ===========================================================================
class RepairHistoryView(BaseModel):
    sn: str
    repairs: list[dict] = Field(default_factory=list)


class ReworkOrderListView(BaseModel):
    wo_id: str
    rework_orders: list[dict] = Field(default_factory=list)


class IsolationResult(BaseModel):
    isolation_order_id: str
    status: str
    batches: list[str] = Field(default_factory=list)


# ===========================================================================
# 质量上下文
# ===========================================================================
class DefectRateView(BaseModel):
    scope: str
    total_units: int = 0
    defective_units: int = 0
    defect_rate: float = 0.0
    baseline_rate: float = 0.0
    time_range: dict = Field(default_factory=dict)


# ===========================================================================
# RAG：追溯图 / 子图 / 文档检索
# ===========================================================================
class TraceGraphView(BaseModel):
    """query_traceability_graph 返回：节点 + 边 + subgraph_ref + route_version。"""
    serial_no: str = ""
    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)
    subgraph_ref: str = ""
    route_version: str | None = None


class SubgraphView(BaseModel):
    """fetch_subgraph_nodes 返回：节点列表（L2 按 subgraph_ref 回查，不重查图）。"""
    subgraph_ref: str = ""
    nodes: list[dict] = Field(default_factory=list)


# ===========================================================================
# Mapper：DTO -> View（mock 下透传，真实下做字段映射）
# ===========================================================================
class _Mapper:
    """通用 mapper：dict -> View，字段名一致时直接 model_validate。"""

    @staticmethod
    def to(view_cls: type[BaseModel], dto: Any) -> Any:
        if isinstance(dto, BaseModel):
            dto = dto.model_dump()
        return view_cls.model_validate(dto)


def to_view(view_cls: type[BaseModel], dto: Any) -> Any:
    return _Mapper.to(view_cls, dto)
