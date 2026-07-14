# agent-service 版本锚点对齐方案

## 目标
把 agent-service (factorybot) 的版本一致性三段链 L1/L2 段对齐到 RAG 侧已通用化的
`VersionAnchor(kind, ref_id, version)`，并同步 AGENT服务 / 领域模型 文档口径。

三段链（核心安全契约）：
图 `SNAPSHOT_OF_{kind}{version}` (RAG 侧, 已完成) -> **L1 `DiagnosisReport.version`** ->
**L2 `Draft.version`** -> MES 应用服务校验 ACTIVE (`process_management.py`, 保留 route-specific)。

本方案严格镜像 RAG 侧已批准的通用化决策（`统一 VersionAnchor(折叠 ref)`，保留 MES REST /
图 schema / `ProcessRouteActivated` 事件 payload 为 route-specific）。

## 设计决策（与 RAG 侧一致，无需再问）
1. **新增 `VersionAnchor` 值对象**于 `factorybot/app/domain/version.py`：`VersionKind`
   (route/bom/rule/asset/standard) + `VersionAnchor(kind, ref_id, version)`，含 `is_bound`、
   `from_flat(version, version_kind, version_ref_id)`、`to_flat()`。factorybot 是独立微服务，
   不导入 FactoryRAG，需自持一份契约副本（同 RAG 侧 `shared/events/version_contract.py` 的角色）。
   不含 RAG 专属的 `to_metadata`/`as_edge_attr`（ChromaDB/图专属）。
2. **领域链实体用扁平字段 + `version_anchor()` 属性**（镜像 RAG `DocQuery`/`ChunkHit`）：
   `DiagnosisReport`/`Draft`/`DiagnosisSession`/`TraceGraphView`/`DocSearchHit` 持
   `version`/`version_kind`/`version_ref_id` 三扁平字段 + `version_anchor()` 属性构造锚点。
   这样 LLM 结构化输出直接产扁平字段、`model_validate` 直校验，避免 flat↔object 转换。
3. **ACL 方法收 `VersionAnchor|None`**（canonical）：`search_docs(version_anchor=...)`、
   `query_traceability_graph(..., version_anchor=...)`，内部映射为 RAG API 扁平参数。
4. **工具入参 / API / LLM schema 用扁平字段**（镜像 RAG `TraceQuery`/`DocSearch` 请求 DTO）。

## 通用化范围（CHAIN：route_version -> 版本锚点）

### 新增
- `app/domain/version.py` — `VersionKind` + `VersionAnchor`（from_flat/to_flat/is_bound）。

### Domain
- `app/domain/report.py` — `DiagnosisReport.route_version` -> `version`/`version_kind`/`version_ref_id`
  + `version_anchor()`；`partial()` 不带版本。
- `app/domain/draft.py` — `Draft.route_version` -> 三扁平字段 + `version_anchor()`；注释改"三段链第三段"。
- `app/domain/session.py` — `DiagnosisSession.route_version` -> 三扁平字段 + `version_anchor()`。
- `app/domain/evidence.py` — `DocSearchHit.route_version` -> 三扁平字段 + `version_anchor()`。

### ACL (infrastructure/acl)
- `views.py` — `TraceGraphView.route_version` -> `version`/`version_kind`/`version_ref_id` + `version_anchor()`。
  （`PassRecordView`/`WorkOrderView`/`WipPositionView.route_version` **保留**——真实 MES 字段。）
- `rag.py` — `query_traceability_graph(serial_no, tenant, subgraph_ref, version_anchor: VersionAnchor|None)`；
  发 `version`/`version_kind` 参数（TraceQuery 无 ref_id）；docstring 改。
- `doc_rag.py` — `search_docs(query, tenant, version_anchor: VersionAnchor|None)`；发
  `version`/`version_kind`/`version_ref_id`；fixture 过滤按锚点三字段。

### Tools (application/tools.py)
- `QueryTraceabilityGraphArgs.route_version` -> `version`/`version_kind`（TraceQuery 无 ref_id）。
- `SearchDocsArgs.route_version_filter` -> `version`/`version_kind`/`version_ref_id`。
- 注册 lambda 透传 `version_anchor=`（由扁平字段构造）。
- （`QueryProcessRouteArgs`/`WriteRouteActivateArgs`/`WriteSopPublishArgs` 的 `route_id`/`route_version`
  **保留**——MES 工艺路线工具，route-specific。）

### AI (infrastructure/ai)
- `graph_builder.py` — `AgentState.route_version` -> `version`/`version_kind`/`version_ref_id`；
  L1_SYSTEM_PROMPT 字段说明与示例改扁平三字段；`_build_user_prompt` `工艺版本 route_version=X` ->
  `版本锚点 version=X (kind=Y)`；`_parse_report` 兜底从 state 透传三扁平字段。
- `mock_chat_model.py` — mock 报告 dict `route_version: "v4"` -> `version`/`version_kind`/`version_ref_id`。

### Application
- `diagnosis_service.py` — `diagnose(..., route_version=)` -> `..., version_anchor: VersionAnchor|None`；
  session 与 initial state 携带三扁平字段（或 anchor）；state 透传给 graph。
- `draft_service.py` — 第三段透传：`if not draft.version: draft.version/kind/ref_id = report...`。
- `builders/base.py` — `extract_route_version` -> `extract_version_anchor(report) -> VersionAnchor|None`
  （返回 `report.version_anchor()`）。
- `builders/eight_d.py`/`sop.py`/`rework_order.py` — `anchor = extract_version_anchor(report)`；
  `search_docs(..., version_anchor=anchor)`；prompt 文本 `route_version=X` -> `version=X(kind=Y)`；
  `draft.version/kind/ref_id = anchor...`。
  - `sop.py` `build_from_route_activated(route_id, route_version)` **签名保留**（事件 route-specific），
    内部 `draft.version_anchor` 设为 `VersionAnchor(ROUTE, route_id, route_version)`。

### API (api/)
- `schemas.py` — `DiagnosisRequest.route_version` -> `version`/`version_kind`/`version_ref_id`；
  `DiagnosisReportResponse.route_version` -> 三扁平字段。（L3 `target_route_id`/`target_route_version`
  **保留**——L3 工艺切换目标，route-specific。）
- `diagnosis_router.py` — 透传 `version_anchor=`（由请求扁平字段构造）；响应回填三字段。

### Cost (infrastructure/cost)
- `result_compactor.py` — `query_traceability_graph` 白名单 `[..., "route_version"]` ->
  `[..., "version", "version_kind", "version_ref_id"]`。（`query_pass_records` 白名单的 `route_version`
  **保留**——MES 过点记录字段。）
- `redis_/tool_cache.py` + `cost/tool_result_cache.py` — `_key(..., route_version)` ->
  `_key(..., version_anchor)`，序列化锚点入 key。**无调用方**（cache 默认关闭的 stub），仅术语对齐。

### Fixtures (data/)
- `data/rag/trace_graph.json` — 顶层 `route_version: "v4"` -> `version: "v4"`/`version_kind: "route"`/
  `version_ref_id: "RR-B"`（两条 SN 记录都改）。`RouteVersion` 节点 props 与 `SNAPSHOT_OF_ROUTE`
  边 prop **保留**（图 schema 不变）。
- `data/rag/docs.json` — 各 doc `route_version` -> `version`/`version_kind`/`version_ref_id`：
  SOP 文档 `version_kind="route"`/`version_ref_id="RR-B"`；8D/手册留空（无锚点）。

### Tests
- `test_l1_diagnosis.py` — docstring `route_version=v4` 改；`assert report.route_version == "v4"` ->
  `assert report.version == "v4" and report.version_kind == "route"`。
- `test_l2_draft.py` — 模块 docstring；`_fake_report` 构造 `route_version="v4"` ->
  `version="v4"`/`version_kind="route"`/`version_ref_id="RR-B"`；`assert draft.route_version == "v4"` ->
  `assert draft.version == "v4"`。
- （`test_l3_changeover.py` 的 `target_route_version="v4"` **保留**——L3 route-specific。）

### Docs（实现阶段按映射结果精确改）
- **AGENT服务/**：扫 9 篇含 route_version 的文档（L1/L2/L3 实现方案、两份 LangGraph 架构流程图、
  Agent可观测性、AgentToken成本优化、Agent记忆系统、Agent长程任务、引入路线、LLM接入与模型兼容），
  把 CHAIN 类引用（三段链/evidence/Draft/Report 的 route_version、RAG 版本过滤、版本锚点概念）
  改为 `VersionAnchor` 口径；MES-ROUTE / GRAPH-SCHEMA 类引用保留。CRLF 保留。
- **领域模型/**：按映射结果验证。若仅含真实 MES route 字段（WorkOrder/PassRecord/ProcessRoute）
  则不动；若提及 RAG/agent 三段链或版本锚点则对齐。
- **RAG服务/**：若 agent-service 链路被引用（如协同文档），同步口径。已在前次通用化处理过，复核即可。

## 保留 route-specific（不动，与 RAG 侧一致）
- MES 真实字段：`PassRecordView`/`WorkOrderView`/`WipPositionView.route_version`。
- MES REST ACL：`process_management.py`（`query_route`/`check_qualification` 的 route_id+route_version +
  ACTIVE 校验，对应 RAG 侧 `fetch_route_version`）。
- MES 工艺工具/写：`QueryProcessRouteArgs`、`WriteRouteActivateArgs`、`WriteSopPublishArgs`。
- L3 工艺切换：`l3_state.py` 的 `target_route_id`/`target_route_version`、`action_card_builder.py`、
  `code_nodes/query_compare.py`、`code_nodes/write_via_appservice.py`、`l3_orchestrator.py`、
  `api/l3_router.py`、L3 schemas。
- `ProcessRouteActivated` 事件 payload（`data/kafka/process_route_activated.json` 的 route_id/version）+
  `sop.py.build_from_route_activated(route_id, route_version)` 签名（对应 RAG 侧保留事件 payload）。
- 图 schema：`RouteVersion` 节点 props、`SNAPSHOT_OF_ROUTE` 边 `{route_version}` 属性。
- Observability：仅 `prompt_version`（无关）。

## 验证
1. `cd factorybot && uv run python -m pytest tests/ -q` — 10 passed 基线，改后期望仍 10 passed。
2. 文档编码：CRLF 保留（领域模型/AGENT服务 md 用 CRLF）。
3. 复核 `route_version` 残留仅限"保留 route-specific"清单内的命中。

## 风险与边界
- LLM 结构化输出改扁平三字段：mock_chat_model 已覆盖；真实 LLM prompt 示例同步更新，风险低。
- ACL mock fixture shape（TraceGraphView 带 nodes/edges）与真实 RAG `TraceAnswer`（带 hypotheses）
  的既有差异**不在本次范围**——本次只通用化版本字段，不重构 mock/真实契约 shape。
- agent-service 不发布 reindex 事件（那是 RAG 内部 A->B），故 `VersionAnchor` 无事件用途，仅作链路值对象。
