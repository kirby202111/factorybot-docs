# data/ - 模拟数据 (mock 模式数据源)

mock 模式下，ACL 客户端从这里读数据，替代真实 MES REST / RAG 服务 / Kafka / Neo4j。
真实模式（`RUN_MODE=real` + 配置连接）下这些文件不参与，ACL 直连真实服务。

## 场景主线

SMT 产线 `L-01`（车间 `SMT-1`，租户 `WS-A`）：
- 当前工单 `WO-2026-0701`，产品 `P-100`，工艺路线 `RR-B v4`(ACTIVE)。
- 上一工单 `WO-2026-0630`，工艺路线 `RR-A v2`。
- 贴片机 `ASSET-01`。
- 不良单件 `SN-2026-001234`（焊接不良）-- 诊断对象。
- 锡膏批次 `B-2026-0701`。

## 目录

| 子目录 | 内容 | 对应 ACL |
|--------|------|----------|
| `rest/` | MES 14 上下文只读 REST 响应 + 编排 比对输入 | 各只读 ACL client |
| `rag/` | 追溯图 / 子图节点 / 文档检索 | `RagAclClient` / `DocRagAclClient` |
| `kafka/` | 领域事件 payload（主动触发） | Kafka 订阅器 |
| `orchestration/` | 编排 写操作的成功响应（isolation/activate/release/sop） | 受限写 ACL client |

## 文件索引（按 ACL 方法）

- `rest/pass_records.json` — `PassExecutionAclClient.query_pass_records` (key=serial_no)
- `rest/test_results.json` — `PassExecutionAclClient.query_test_results`
- `rest/work_orders.json` — `WorkOrderManagementAclClient.query_work_order`
- `rest/wo_progress.json` — `WorkOrderManagementAclClient.query_wo_progress`
- `rest/kit_status.json` — `MaterialAclClient.query_kit_status`
- `rest/process_routes.json` — `ProcessManagementAclClient.query_route` (key=`route_id:version`)
- `rest/first_article.json` — `ProcessManagementAclClient.query_first_article_status`
- `rest/qualification.json` — `ProcessManagementAclClient.check_qualification`
- `rest/material_batches.json` — `MaterialAclClient.query_material_batch`
- `rest/boms.json` — `MaterialAclClient.query_bom_version`
- `rest/supplier_trace.json` — `MaterialAclClient.query_supplier_trace`
- `rest/device_params.json` — `DeviceDataAclClient.query_device_params`
- `rest/asset_status.json` — `EquipmentAssetLedgerAclClient.query_asset_status`
- `rest/current_stencil.json` — `ToolingAclClient.query_current_stencil`
- `rest/local_program.json` — `ToolingAclClient.query_local_program_version`
- `rest/stencil_lending.json` — `ToolingAclClient.query_stencil_lending`
- `rest/last_changeover_close.json` — `ToolingAclClient.query_last_changeover_close`
- `rest/equipment_telemetry.json` — `EquipmentTelemetryAclClient.query_equipment_telemetry`
- `rest/fault_history.json` — `EquipmentTelemetryAclClient.query_fault_history`
- `rest/process_fmea.json` — `EquipmentTelemetryAclClient.query_process_fmea`
- `rest/batches_in_window.json` — `EquipmentTelemetryAclClient.query_batches_in_window`
- `rest/product_sensitivity.json` — `EquipmentTelemetryAclClient.query_product_sensitivity`
- `rest/repair_history.json` — `ReworkAclClient.query_repair_history`
- `rest/rework_orders.json` — `ReworkAclClient.query_rework_orders`
- `rest/defect_rate.json` — `QualityAclClient.query_defect_rate`
- `rag/trace_graph.json` — `RagAclClient.query_traceability_graph` (key=serial_no)
- `rag/subgraphs.json` — `RagAclClient.fetch_subgraph_nodes` (key=subgraph_ref)
- `rag/docs.json` — `DocRagAclClient.search_docs`
- `kafka/process_route_activated.json` — 触发工艺变更场景
- `kafka/equipment_fault.json` — 触发故障复产场景
- `kafka/defect_rate_spike.json` — 触发 诊断 主动诊断
- `orchestration/write_results.json` — 受限写 ACL 的 mock 成功响应

## 切换 编排 换线 PASS / FAIL 路径

默认 `rest/current_stencil.json` 中 `ASSET-01` 的 `stencil_id=ST-A`，与 `RR-B v4` 期望的
`ST-B` 不一致 -> 换线走 **tooling FAIL -> root_cause agent A** 分支（触发 LLM）。
要看 **全程 PASS（LLM 调用=0）**：把 `ASSET-01` 的 `stencil_id` 改为 `ST-B`、
`local_program.json` 的 `program_id` 改为 `PB-B-v4` 即可。
