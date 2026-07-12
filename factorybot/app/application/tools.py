"""L1/L3 工具注册：把 ACL client 方法包装成 ToolDescriptor 注册到 ToolRegistry。

工具边界即限界上下文边界。L1 全部只读（ReadOnlyToolGate）；L3 含受限写工具
（WriteToolGate：必须 requires_confirmation + writes_via）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.domain.tool import ToolDescriptor, ToolRegistry


# ===========================================================================
# L1 工具入参 schema
# ===========================================================================
class QueryTraceabilityGraphArgs(BaseModel):
    serial_no: str
    subgraph_ref: str | None = None
    route_version: str | None = None

class QueryPassRecordsArgs(BaseModel):
    serial_no: str

class QueryTestResultsArgs(BaseModel):
    serial_no: str

class QueryWorkOrderArgs(BaseModel):
    work_order_id: str

class QueryWoProgressArgs(BaseModel):
    work_order_id: str

class QueryProcessRouteArgs(BaseModel):
    route_id: str
    route_version: str            # 强制（ACL 层再校验 + ACTIVE）

class QueryMaterialBatchArgs(BaseModel):
    batch_no: str

class QueryBomVersionArgs(BaseModel):
    bom_id: str
    version: str

class QueryKitStatusArgs(BaseModel):
    work_order_id: str

class QueryDeviceParamsArgs(BaseModel):
    asset_id: str
    time_range_start: str = ""
    time_range_end: str = ""

class QueryAssetStatusArgs(BaseModel):
    asset_id: str

class QueryRepairHistoryArgs(BaseModel):
    serial_no: str

class QueryReworkOrdersArgs(BaseModel):
    work_order_id: str

class QueryDefectRateArgs(BaseModel):
    batch_no: str | None = None
    work_order_id: str | None = None
    time_range_start: str | None = None
    time_range_end: str | None = None

class SearchDocsArgs(BaseModel):
    query: str
    route_version_filter: str | None = None


# ===========================================================================
# L3 只读工具入参 schema（agent A/B）
# ===========================================================================
class QueryStencilLendingArgs(BaseModel):
    stencil_id: str

class QueryLastChangeoverCloseArgs(BaseModel):
    asset_id: str

class QueryCurrentStencilArgs(BaseModel):
    asset_id: str

class QueryLocalProgramArgs(BaseModel):
    asset_id: str

class QueryEquipmentTelemetryArgs(BaseModel):
    asset_id: str
    start: str = ""
    end: str = ""

class QueryProcessFmeaArgs(BaseModel):
    asset_id: str

class QueryProductSensitivityArgs(BaseModel):
    batch_ids: list[str] = []


# ===========================================================================
# L3 受限写工具入参 schema
# ===========================================================================
class WriteIsolationArgs(BaseModel):
    batches: list[str]
    reason: str

class WriteRouteActivateArgs(BaseModel):
    route_id: str
    version: str

class WritePassReleaseArgs(BaseModel):
    work_order_id: str

class WriteSopPublishArgs(BaseModel):
    route_id: str
    version: str
    sop_content: dict = {}

class WriteRepairOrderArgs(BaseModel):
    asset_id: str
    fault_time: str = ""
    description: str = ""


def _ro(name, desc, ctx, capability, args_schema, handler, scopes=None) -> ToolDescriptor:
    return ToolDescriptor(
        name=name, description=desc, bounded_context=ctx, capability=capability,
        read_only=True, args_schema=args_schema, handler=handler,
        required_tenant_scopes=scopes or [],
    )


def _write(name, desc, ctx, writes_via, args_schema, handler, scopes=None) -> ToolDescriptor:
    return ToolDescriptor(
        name=name, description=desc, bounded_context=ctx, capability="write",
        read_only=False, requires_confirmation=True, writes_via=writes_via,
        args_schema=args_schema, handler=handler, required_tenant_scopes=scopes or [],
    )


# ===========================================================================
# L1 工具注册表（全程只读）
# ===========================================================================
def build_l1_tool_registry(acl) -> ToolRegistry:
    reg = ToolRegistry(level="L1")
    # 注册首位：query_traceability_graph（system prompt 引导"先调图"）
    reg.register(_ro(
        "query_traceability_graph", "追溯图快路径查询（5M1E 全链路视图）",
        "RAG服务", "l1", QueryTraceabilityGraphArgs,
        lambda serial_no, subgraph_ref=None, route_version=None, tenant=None, **_:
            acl.rag.query_traceability_graph(serial_no, tenant, subgraph_ref, route_version),
        ["rag:read"],
    ))
    reg.register(_ro("query_pass_records", "查过点记录", "过点执行上下文", "l1",
        QueryPassRecordsArgs, lambda serial_no, tenant=None, **_: acl.pass_execution.query_pass_records(serial_no, tenant), ["pass:read"]))
    reg.register(_ro("query_test_results", "查测试结果", "过点执行上下文", "l1",
        QueryTestResultsArgs, lambda serial_no, tenant=None, **_: acl.pass_execution.query_test_results(serial_no, tenant), ["pass:read"]))
    reg.register(_ro("query_work_order", "查工单", "工单管理上下文", "l1",
        QueryWorkOrderArgs, lambda work_order_id, tenant=None, **_: acl.work_order.query_work_order(work_order_id, tenant), ["workorder:read"]))
    reg.register(_ro("query_wo_progress", "查工单进度", "工单管理上下文", "l1",
        QueryWoProgressArgs, lambda work_order_id, tenant=None, **_: acl.work_order.query_wo_progress(work_order_id, tenant), ["workorder:read"]))
    reg.register(_ro("query_process_route", "查工艺路线（必须带 route_version）", "工艺管理上下文", "l1",
        QueryProcessRouteArgs, lambda route_id, route_version, tenant=None, **_: acl.process.query_route(route_id, route_version, tenant), ["process:read"]))
    reg.register(_ro("query_material_batch", "查物料批次", "物料上下文", "l1",
        QueryMaterialBatchArgs, lambda batch_no, tenant=None, **_: acl.material.query_material_batch(batch_no, tenant), ["material:read"]))
    reg.register(_ro("query_bom_version", "查 BOM 版本", "物料上下文", "l1",
        QueryBomVersionArgs, lambda bom_id, version, tenant=None, **_: acl.material.query_bom_version(bom_id, version, tenant), ["material:read"]))
    reg.register(_ro("query_kit_status", "查工单齐套状态", "物料上下文", "l1",
        QueryKitStatusArgs, lambda work_order_id, tenant=None, **_: acl.material.query_kit_status(work_order_id, tenant), ["material:read"]))
    reg.register(_ro("query_device_params", "查设备参数时序", "设备数据接入上下文", "l1",
        QueryDeviceParamsArgs, lambda asset_id, time_range_start="", time_range_end="", tenant=None, **_: acl.device_data.query_device_params(asset_id, time_range_start, time_range_end, tenant), ["device:read"]))
    reg.register(_ro("query_asset_status", "查资产状态", "设备工装台账上下文", "l1",
        QueryAssetStatusArgs, lambda asset_id, tenant=None, **_: acl.asset_ledger.query_asset_status(asset_id, tenant), ["equipment:read"]))
    reg.register(_ro("query_repair_history", "查维修历史", "返修上下文", "l1",
        QueryRepairHistoryArgs, lambda serial_no, tenant=None, **_: acl.rework.query_repair_history(serial_no, tenant), ["rework:read"]))
    reg.register(_ro("query_rework_orders", "查返工单", "返工上下文", "l1",
        QueryReworkOrdersArgs, lambda work_order_id, tenant=None, **_: acl.rework.query_rework_orders(work_order_id, tenant), ["rework:read"]))
    reg.register(_ro("query_defect_rate", "查不良率", "质量上下文", "l1",
        QueryDefectRateArgs, lambda batch_no=None, work_order_id=None, time_range_start=None, time_range_end=None, tenant=None, **_: acl.quality.query_defect_rate(tenant, batch_no, work_order_id, time_range_start, time_range_end), ["quality:read"]))
    reg.register(_ro("search_docs", "文档型 RAG 检索（SOP/手册/8D）", "RAG服务", "l1",
        SearchDocsArgs, lambda query, route_version_filter=None, tenant=None, **_: acl.doc_rag.search_docs(query, tenant, route_version_filter), ["doc:read"]))
    return reg


# ===========================================================================
# L3 工具注册表（agent 只读 + 受限写）
# ===========================================================================
def build_l3_tool_registry(acl) -> ToolRegistry:
    reg = ToolRegistry(level="L3")
    # ---- agent A: root_cause（只读）----
    reg.register(_ro("query_stencil_lending", "查钢网借还记录", "工装上下文", "root_cause",
        QueryStencilLendingArgs, lambda stencil_id, tenant=None, **_: acl.tooling.query_stencil_lending(stencil_id, tenant), ["tooling:read"]))
    reg.register(_ro("query_last_changeover_close", "查上工单收线记录", "工装上下文", "root_cause",
        QueryLastChangeoverCloseArgs, lambda asset_id, tenant=None, **_: acl.tooling.query_last_changeover_close(asset_id, tenant), ["tooling:read"]))
    reg.register(_ro("query_current_stencil", "查产线当前钢网", "工装上下文", "root_cause",
        QueryCurrentStencilArgs, lambda asset_id, tenant=None, **_: acl.tooling.query_current_stencil(asset_id, tenant), ["tooling:read"]))
    reg.register(_ro("query_local_program", "查设备本地程序版本", "工装上下文", "root_cause",
        QueryLocalProgramArgs, lambda asset_id, tenant=None, **_: acl.tooling.query_local_program_version(asset_id, tenant), ["tooling:read"]))
    # ---- agent B: fault_impact（只读）----
    reg.register(_ro("query_equipment_telemetry", "查设备遥测时序", "设备数据接入上下文", "fault_impact",
        QueryEquipmentTelemetryArgs, lambda asset_id, start="", end="", tenant=None, **_: acl.telemetry.query_equipment_telemetry(asset_id, start, end, tenant), ["device:read"]))
    reg.register(_ro("query_process_fmea", "查工艺 FMEA 敏感度", "设备数据接入上下文", "fault_impact",
        QueryProcessFmeaArgs, lambda asset_id, tenant=None, **_: acl.telemetry.query_process_fmea(asset_id, tenant), ["device:read"]))
    reg.register(_ro("query_product_sensitivity", "查产品敏感度", "质量上下文", "fault_impact",
        QueryProductSensitivityArgs, lambda batch_ids, tenant=None, **_: acl.telemetry.query_product_sensitivity(batch_ids, tenant), ["quality:read"]))
    # ---- 受限写（capability="write"，write_via_appservice 调用）----
    reg.register(_write("write_isolation", "下达批次隔离", "返工上下文",
        "返工上下文.application.issue_isolation", WriteIsolationArgs,
        lambda batches, reason, confirmation=None, tenant=None, **_: acl.rework_write.issue_isolation(batches, reason, confirmation, tenant), ["rework:write"]))
    reg.register(_write("write_route_activate", "激活工艺路线", "工艺管理上下文",
        "工艺管理上下文.application.activate_route", WriteRouteActivateArgs,
        lambda route_id, version, confirmation=None, tenant=None, **_: acl.process_write.activate_route(route_id, version, confirmation, tenant), ["process:write"]))
    reg.register(_write("write_pass_release", "过点放行", "过点执行上下文",
        "过点执行上下文.application.release", WritePassReleaseArgs,
        lambda work_order_id, confirmation=None, tenant=None, **_: acl.pass_write.release(work_order_id, confirmation, tenant), ["pass:write"]))
    reg.register(_write("write_sop_publish", "发布 SOP", "工艺管理上下文",
        "工艺管理上下文.application.publish_sop", WriteSopPublishArgs,
        lambda route_id, version, sop_content=None, confirmation=None, tenant=None, **_: acl.process_write.publish_sop(route_id, version, sop_content or {}, confirmation, tenant), ["process:write"]))
    reg.register(_write("write_repair_order", "创建维修单", "设备管理上下文",
        "设备管理上下文.application.create_repair", WriteRepairOrderArgs,
        lambda asset_id, fault_time="", description="", confirmation=None, tenant=None, **_: acl.rework_write.create_repair_order(asset_id, fault_time, description, confirmation, tenant), ["rework:write"]))
    return reg
