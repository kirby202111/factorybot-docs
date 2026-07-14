# 通用化版本锚点（route_version → VersionAnchor）

## 目标与决策（已与用户确认）

把贯穿 B/A/共享内核的 `route_version` 通用化为统一的 `VersionAnchor(kind, ref_id, version)`，
使工艺路线之外的文档（设备维修手册、IPC/ESD 标准）也能版本化管理。

- **范围**：全量 — B(文档 chunk/过滤/API) + A(TraceAnswer/子图) + 共享内核(VersionAnchor/ReindexRequest) + 设计文档；
  **Neo4j 图 schema 保持不变**（图节点本就多版本类型：RouteVersion/Bom/QualityGateRule），仅把领域方法 `route_version_locked()` 改名为 `version_locked()` 返回通用锚点。
- **结构**：统一 `VersionAnchor`，`asset_id`/`route_id` 折叠进 `version_ref_id`。chunk 扁平存 `version_kind`+`version_ref_id`+`version` 三字段。

## 核心模型：`VersionAnchor`（`app/shared/events/version_contract.py`）

```python
class VersionKind(str, Enum):
    ROUTE = "route"        # 工艺路线（PROCESS_BOUND SOP/检验标准）
    BOM = "bom"            # 物料清单
    RULE = "rule"          # 质量门规则
    ASSET = "asset"        # 设备资产（ASSET_BOUND 维修手册）
    STANDARD = "standard"  # 通用标准（GENERAL IPC/ESD）

class VersionAnchor(BaseModel):
    kind: VersionKind
    ref_id: str   # route_id / asset_id / standard_id / bom_id / rule_id
    version: str  # v3 / RevH

    def as_edge_attr(self) -> dict[str, str]:
        # 图边属性保持 {route_version: v3} 兼容图 schema
        return {f"{self.kind.value}_version": self.version}

    def to_metadata(self) -> dict[str, str]:
        return {"version_kind": self.kind.value, "version_ref_id": self.ref_id, "version": self.version}

    @classmethod
    def from_metadata(cls, meta: dict) -> "VersionAnchor | None": ...
    @property
    def is_bound(self) -> bool: return bool(self.version)
```

- `VersionKind` 值由 `route_version`/`bom_version`/`rule_version` 改为短名 `route`/`bom`/`rule`/`asset`/`standard`（version 是独立字段，后缀冗余）。
- `ReindexRequest`：`route_id`+`route_version` → `anchor: VersionAnchor`；`as_kafka_payload` 的 `payload` 改为 `{"anchor": anchor.model_dump()}`。
- `parse_anchor` 错误信息通用化（`{kind} 版本锚点缺失`）。

## 分层改动

### L1 共享内核
- `shared/events/version_contract.py`：如上。
- `shared/events/__init__.py`：docstring 更新。

### L2 B 文档领域
- `document/domain/chunk.py` — `DocumentChunk`：
  删 `route_version`/`route_id`/`binding_asset_id`；加 `version_kind: str=""`/`version_ref_id: str=""`/`version: str=""`；
  加 `version_anchor` 属性（from 3 字段）。`to_metadata_dict`/`from_chroma` 改读写三字段。`version_id`（文档自身版本）不动。
- `document/domain/document.py` — `DocumentBinding`/`DocumentVersion`/`KnowledgeDocument`：
  - `BindingType` 加 `STANDARD_VERSION`；`ASSET`/`ASSET_MODEL` 的 `target_ref` 增可选 `asset_version`。
  - 删 `get_route_version`/`get_route_id`/`get_asset_id`/`get_version_by_route`；加 `get_version_anchor() -> VersionAnchor | None`（按 binding_type 映射 kind，从 target_ref 取 ref_id+version）。
  - `enforce_category_invariant`：PROCESS_BOUND 需 ROUTE/RULE 绑定（不变）；GENERAL 可选 STANDARD_VERSION。
- `document/domain/answer.py` — `DocQuery`/`DocSearch`：删 `route_version`/`asset_id`，加 `version`/`version_kind`/`version_ref_id`（均 `str|None`）+ `version_anchor()` 助手；
  `ChunkHit`：`route_version` → `version`+`version_kind`+`version_ref_id`；`DocAnswer`：`route_version_filter` → `version_filter`+`version_kind_filter`。
- `document/domain/retriever_port.py` — `RetrieverPort.retrieve`：删 `route_version`/`asset_id`，加 `version_anchor: VersionAnchor | None = None`。

### L3 B 基础设施
- `document/infrastructure/chunk_filter.py` — `ChunkFilter`：`route_version`+`asset_id` → `version_anchor: VersionAnchor|None`。
  `to_where`：anchor 非空时写 `version_kind`（必）、`version`（非空时）、`version_ref_id`（非空时）— 各自独立，兼容「route 仅按 version」「asset 按 ref_id±version」两种用法。`matches` 镜像。
- `document/infrastructure/chromadb/retriever.py` + `bm25/bm25_retriever.py`：`retrieve(..., version_anchor=...)`；命中映射读三字段。
- `document/infrastructure/chromadb/schema.py`：docstring 字段列表更新（无真 schema，ChromaDB 无 schema）。
- `document/application/chunking.py` — `split`：`route_version`/`route_id`/`asset_id` 三参 → `version_anchor: VersionAnchor|None` 单参。
- `document/application/hybrid_retriever.py`：`retrieve`/`_gather` 透传 `version_anchor`。
- `document/application/retrieval_service.py`：`_enforce_route_version` → `_enforce_version_anchor`（PROCESS_BOUND 需 ROUTE/RULE 锚点且 version 非空；ASSET_BOUND 需 ASSET 锚点且 ref_id 非空；GENERAL 可选）；`_synthesize`/缓存键用锚点原语。
- `document/application/ingestion_service.py`：`_first_route_version`/`_first_route_id`/`_first_asset_id` → `_first_version_anchor(bindings)`；传 `version_anchor=` 给 split。
- `document/infrastructure/chromadb/document_repo.py`：`find_drafts_by_route`/`find_published_by_route`/`_matches_route` 保留（route 触发场景，按 binding JSON 匹配 route_id+route_version）— 仅 docstring/注释更新。🔴 或同步泛化为 `find_by_anchor`（见下）。
- `document/infrastructure/handlers/reindex.py`：从 `ReindexRequest.anchor`（kind=route）解出 route_id/route_version 调 repo；split 用 anchor。
- `document/infrastructure/handlers/process_route.py` + `quality.py`：注释更新（route 触发器保持 route-specific）。

### L4 A 追溯领域
- `traceability/domain/seed.py` — `TraceQuery`/`ExpandRequest`：`route_version` → `version`+`version_kind`（ref_id 可选，trace 一般不需）。
- `traceability/domain/answer.py` — `TraceAnswer`：`route_version` → `version`+`version_kind`+`version_ref_id`（透传给三段链 L1/L2/MES）。
- `traceability/domain/subgraph.py`：`route_version_locked()` → `version_locked() -> VersionAnchor|None`（读 Method 维度 RouteVersion 节点 `{route_id, route_version}` → 构 ROUTE 锚点）。`TraceEdge.version` 保留（边属性，rel 名隐含 kind）。图节点 props 里的 `route_version` 键不动（图数据）。
- `traceability/application/trace_retrieval_service.py`：`_synthesize` 用 `version_locked()`；prompt 里 `version={anchor.version}`；`_enrich_suggested_action` 传锚点原语给 DocRagPort；`TraceAnswer` 填锚点三字段；`on_route_upgraded` 构 `ReindexRequest(anchor=VersionAnchor(ROUTE, route_id, new_version))`（方法名保留，route 触发）。
- `traceability/infrastructure/neo4j/retriever.py`：`expand_5m1e` 签名 `route_version` → `version`+`version_kind`（MVP 实现仅认 kind=route，读 props.route_version）；Cypher 不动。`_map_wip` 同。
- `traceability/infrastructure/neo4j/subgraph_repo.py`：`SubgraphAuditModel.route_version` 列 → `version_kind`/`version_ref_id`/`version` 三列；`save` 写 `version_locked()` 的锚点。
- `traceability/infrastructure/neo4j/projections/*` + `schema.py`：**不动**（图 schema/projection 保持 route-specific，图锁版本语义不变）。

### L5 共享 ACL
- `shared/acl/ports.py` — `DocRagPort.query/search`：`route_version`+`asset_id` → `version`+`version_kind`+`version_ref_id`；`TraceRagPort.query/expand`：`route_version` → `version`+`version_kind`。
- `shared/acl/adapters.py` — InProcess/Http 四个 adapter：原语映射到新 DTO/JSON。
- `shared/acl/mes_clients.py`：**保持 route-specific**（MES process-route REST API 本就是 route 语义：`fetch_route_version`/`fetch_active_route_version`/`fetch_route_version_by_sn`）。调用方把结果包成 ROUTE 锚点。`RouteView`/`CheckpointView.route_version` 不动。

### L6 API + Agentic
- `api/v1/doc_router.py` + `trace_router.py`：docstring 更新。
- `agentic/domain/tool.py` + `agentic/infrastructure/ai/tool_executor.py`：E 的 `search_docs` 工具 `route_version` → `version`+`version_kind`+`version_ref_id`（脚手架占位留空）。

### L7 数据库迁移
- `alembic/versions/0001_initial.py`：`subgraph_audit.route_version` 列 → `version_kind`/`version_ref_id`/`version` 三列（nullable）。🔴 改 0001 in place（pre-prod/mock，无在线数据）vs 加 0002 迁移 — 倾向 in place。

### L8 数据文件
- `data/manifest.json`：每文档 `route_version`/`route_id`/`asset_id` → `version_kind`/`version_ref_id`/`version`；
  **给手册/标准补版本号**演示通用化（维修手册 v1、IPC RevH、ESD v1）。bindings 的 ASSET 加 `asset_version`、GENERAL 加 `STANDARD_VERSION` 绑定。
- `data/queries.json`：`route_version` → `version`+`version_kind`（PROCESS_BOUND 加 `version_kind:"route"`）；asset 查询 `asset_id` → `version_kind:"asset"`+`version_ref_id`。
- `data/trace/scenarios.json`：`expected.route_version_locked` → `expected.version_locked`（值 "v3"/"v4"）+ `version_kind:"route"`；节点 props/边的 `route_version`/`version` 键不动（图数据）。
- `data/README.md`：字段说明更新。

### L9 测试
更新断言到新字段/锚点：`test_version_contract`（VersionKind 短名、`as_edge_attr`=`{route_version:v3}`、`to/from_metadata`、ReindexRequest.anchor）、`test_chunk_immutability`、`test_chunk_filter`、`test_retrieval_enforce_version`（PROCESS_BOUND 缺锚点/ASSET_BOUND 缺 ref 抛错）、`test_port_adapter`、`test_mock_data_rag`、`test_mock_data_trace`、`test_hybrid_retriever`、`test_bm25_index`。`test_gates.py` 不动（测 gate，`fetch_route_version` 是 MES 只读方法名，保留）。

### L10 脚本
- `scripts/run_mock_rag.py`：CLI `--route-version`/`--asset-id` → `--version`/`--version-kind`/`--version-ref-id`；输出字段。
- `scripts/run_mock_trace.py`：`answer.route_version` → `answer.version`；`JsonLlm` hint 里 `version=`；`_print_scenario` 显示 `version_locked`。

### L11 设计文档（口径，原地整合不拆分）
- `RAG服务/rag-service-技术选型和实现方案.md`：§1.2 强制带版本、§2.10、§8.4 三段链、§10.3、决策表 #2/#7。
- `RAG服务/rag-service-整体结构设计.md`：§3.10、§6.4、version_contract 行、§6.x ACL 表。
- `RAG服务/文档型 RAG/*.md`、`RAG服务/追溯型 RAG/*.md`：route_version 引用。
- `FactoryRAG/README.md`：核心安全契约、§6 路线间调用约定。
- 三段链措辞通用化：`图 SNAPSHOT_OF_{kind}{version} → L1 evidence.version_anchor → L2 Draft.version_anchor → MES 校验 ACTIVE`。

## 🔴 待确认 / 越界

1. **agent-service（factorybot）的三段链后两段**：`evidence.route_version`/`Draft.route_version` 在 agent-service 代码与 AGENT服务/领域模型文档中。本次**不改**（用户范围限定 rag-service B+A+共享+文档）。三段链第一段（rag-service）已通用化，后两段对齐留作后续。需用户确认是否本轮一并改 agent-service。
2. **DB 迁移**：改 0001_initial in place（推荐，pre-prod）还是加 0002？
3. **document_repo 的 `find_*_by_route`/`_matches_route`**：保留 route-specific（route 触发器用）还是同步泛化为 `find_by_anchor`？倾向保留。
4. **GENERAL 标准是否强制带版本**：倾向可选（MVP 可不带），仅提供能力。
5. **手册/标准补版本号的数据富化**：是否给 manifest 现有手册/标准加示例版本号（v1/RevH）？倾向加，以验证通用化。

## 验证

1. `cd FactoryRAG && uv run pytest` 全绿（红线不变式：版本隔离/DEPRECATED 不泄漏/强制带版本/Port 契约/图锁版本）。
2. `uv run python scripts/run_mock_rag.py` 全量 7 查询：route v3/v4/box-build 隔离、asset 隔离、standard 命中均正确。
3. `uv run python scripts/run_mock_trace.py` 两场景：SN-2024-001 锁 v3、SN-2024-009 锁 v4，5M1E 假设 + 跨路线 SOP 富化（按 v3/v4）正确。
4. grep 确认 FactoryRAG 内除「图 schema/projection/MES route REST」白名单外无 `route_version` 残留。

## 实施顺序

L1 共享内核 → L2 B 领域 → L3 B 基础设施/应用 → L4 A 领域/应用/infra → L5 ACL → L6 API/Agentic → L7 迁移 → L8 数据 → L9 测试 → L10 脚本 → 验证 → L11 文档。
（先改领域模型与 VersionAnchor，再逐层适配，最后改数据/测试/脚本验证，最后同步文档口径。）

## 编码注意

- Python 代码正常 Edit。
- `RAG服务/*.md`、`FactoryRAG/README.md` 等文档：先确认换行编码（Windows 仓库可能 CRLF），多行替换若 Edit 失败则改用 bun 跑 JS 脚本（见 memory）。
