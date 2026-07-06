# 追溯型 RAG 详细设计（GraphRAG + 领域事件流）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.1 路线 A（追溯型 RAG）的落地展开，输出**技术栈、图谱建模、事件流索引构建、5M1E 检索推理、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + Neo4j + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 Kafka 只读事件 + REST 只读查询解耦，互不侵入。
> **口径纪律**：追溯型 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律——说"规划方向 / 设计取舍"，不说"我们已经做了 GraphRAG"。MES 领域对错误答案零容忍（错给一条已失效工艺会直接导致批量不良），所以本文强调**图是领域事件的只读投影 + 版本快照不可变 + 可观测兜底**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 目标

把 MES 已有的**全链路追溯**（[领域总览.md](../../领域模型/领域总览.md) §5 的核心价值）做成可被 LLM 检索 + 推理的**属性图**，让"某单件出现焊接不良，根因是什么"这类问题不再依赖工程师跨 5 个界面手动串，而是一次图谱检索 + LLM 综合即按 **5M1E** 给出根因假设 + 证据链。

典型场景："单件 SN-001 焊接不良" → 系统自动按 5M1E 串起：

1. **Man（人）**：该单件每站过点记录的 `scanned_by`、首件放行人、返修诊断人
2. **Machine（机）**：焊接站当时绑定的 `equipment_id` / `fixture_id`（过点执行上下文 `CheckpointRecord`）、该设备当时可用性、近期维修/点检/计量状态
3. **Material（料）**：该单件过点时消耗的锡膏批次 / 元件批次（物料上下文 `InventoryBatch`）、供应商、替代料规则
4. **Method（法）**：该单件锁定的 `routeVersion` 快照（§5.1 版本一致性）、焊接站 `RouteStep` 参数模板、质量门禁规则版本
5. **Measurement（测）**：该单件 `TestResult`（AOI/SPI/FCT）、`QualityVerdict` 业务判定、缺陷记录
6. **Environment（环）**：老化房环境采样、焊接站温区实时数据（设备数据接入上下文 `DataPacket` 的语义子集）

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **只读投影** | 图是领域事件的**只读投影**，不是事实源；事实源是各上下文的聚合根 | 仅订阅 Kafka 只读事件 + REST 只读降级查询；图库归 RAG 服务自有，从不回写 MES |
| **不进过点主事务** | 索引构建异步消费事件，与过点判定完全解耦 | 过点 P99 ≤200ms（§4.1）不受图索引影响；图允许秒级最终一致 |
| **版本快照不可变** | 历史过点记录锁定的 `routeVersion` 不随工艺变更改变 | `CheckpointRecord` 节点带 `route_version` 属性 + `[:SNAPSHOT_OF_ROUTE]` 边指向当时版本；工艺变更只新增版本节点，不改历史边（INV-09） |
| **权限隔离** | 检索前按车间 / 产线 / 角色过滤，不是答完再裁剪 | 图节点带 `tenant_scope`（workshop/line），Cypher 查询前置过滤 |
| **可观测兜底** | 每个答案带证据链（节点/事件引用）+ 置信度，低置信度转人工 | 检索结果结构化落库 + 置信度阈值；与 MES 防错理念一致：宁可拦下让人判 |
| **高频采集不全量入图** | 设备原始报文（设计容量 ~1 万报文/秒）不进图，否则图被撑爆 | 仅消费 `dc.*` 的**语义事件**（`SerialNumberMarked` / `EquipmentRunHourAggregated` / `EquipmentAlarmRaised`），不消费原始 `DataPacket` 流 |

### 1.3 与 L1 诊断型 Agent 的关系

[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §2.2 的 L1 诊断型 Agent 是**追溯型 RAG 的多步推理升级**。两者不重复，是分层关系：

| 层 | 追溯型 RAG（本文） | L1 诊断型 Agent |
|----|-------------------|-----------------|
| 形态 | 图 + 一次检索综合 | 多步 ReAct 工具调用 |
| 数据 | 预投影的属性图（事件物化） | 实时调各上下文只读 REST |
| 时延 | 秒级（图检索 + LLM 综合） | 数十秒（多步工具循环） |
| 适用 | "这条单件 5M1E 全貌"一次性给齐 | "根因要递进追问、跨上下文跳转"的深度诊断 |

- L1 的 `query_traceability_graph` 工具即封装本文的检索 API（httpx 调 `rag-service`）；L1 也可绕过图直接调上下文 REST，两条路径互补。
- **先有图、后有 Agent**：L1 依赖图检索作为"快路径"，图没建起来时 L1 退化为纯工具循环，体验差。落地顺序见 [RAG服务引入路线.md](../RAG服务引入路线.md) §3"先 B 后 A"。

### 1.4 与 Java 技术栈的关系

- RAG 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈——只订阅 Kafka 只读事件、调只读 REST。
- 跨语言的物理边界反而是好事：RAG 服务无法共享 Java 事务 / 内存，天然强制只读、不进过点主事务、不旁路应用服务写路径。
- 复用 [实现说明](../../实现说明/) 既有的 Kafka topic、REST 只读接口、领域事件 envelope（`source_event_id` / `occurred_at` / 租户头），不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 类型提示 + Pydantic 校验，AI 生态最成熟，与 L1 Agent 同栈复用 LLM 抽象 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI，与 L1 Agent 一致，适合做检索 HTTP 入口 |
| 图存储 | **Neo4j 5.x** | 属性图 + Cypher + GDS 图算法 + **原生向量索引**（语义节点直接挂向量，免二套库） |
| 图存储（替代） | NebulaGraph / Apache AGE (PostgreSQL 扩展) | 车间网与办公网隔离时的本地化部署备选，见 §2.4 |
| 检索编排 | **LlamaIndex `PropertyGraphIndex`** | 图检索 + LLM 综合的上层抽象，支持 Cypher / 文本转 Cypher / 子图扩展三种模式 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 模型可插拔，配置切换 Claude / 通义千问 / DeepSeek / 本地化模型，与 L1 一致 |
| Embedding | **bge-m3**（多语种，可本地化） | 缺陷描述 / SOP 片段 / 自然语言问题的语义向量化，覆盖中英文 |
| 数据校验 | **Pydantic v2** | 检索请求 / 子图视图 / 报告 DTO 的 schema 即类型 |
| HTTP 客户端 | **httpx**（异步） | 降级查询各上下文只读 REST |
| 消息 | **aiokafka** | 订阅领域事件（`mes.*` / `eam.*` / `process.*` 等），异步非阻塞 |
| 元数据持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 索引位点（consumer offset）、`event_id` 幂等表、检索审计 |
| 缓存 | **redis-py (async)** | 子图结果短期缓存（同种子重复检索去重） |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 串联、指标告警 |
| 配置 | pydantic-settings | 环境变量 / 配置文件统一管理 |
| 部署 | 独立微服务 `rag-service`（uvicorn + gunicorn worker） | 与三大服务同网格，K8s 部署 |

### 2.2 为什么是 GraphRAG 而非纯向量检索

- 本 MES 的追溯链是**结构化关系**：`SN → 过点记录 → 设备 / 工艺版本 / 物料批次 / 测试结果`，这些是显式引用（`source_work_order_id`、`routeVersion`、`asset_id`、`batch_no`），不是文本相似度。向量检索只能找"语义相近的文档"，找不到"这条单件用了哪批锡膏、那批锡膏还进了哪些单件"。
- GraphRAG 把跨上下文引用**显式建成图边**，5M1E 串联回退为一跳/两跳的 Cypher 扩展，准确率远高于向量近似匹配。
- 向量在本文里只承担**语义入口**：把自然语言问题解析成图种子（SN / 批次 / 工单 / 设备），以及缺陷描述相似度——是图的补充，不是主体。这区别于 [RAG服务引入路线.md](../RAG服务引入路线.md) 路线 B（文档型 RAG，向量是主体）。

### 2.3 为什么图谱建立在领域事件流之上

- 图不是凭空建模的，是各上下文**领域事件的投影**——与过点执行上下文的 `ProcessRouteCache` / `EquipmentAvailabilityCache` 同构（[过点执行上下文.md](../../领域模型/生产执行服务/事件风暴/过点执行上下文.md) §1.2），只是投影目标是属性图而非键值缓存。
- 这带来三个红利：① **天然只读**——投影只消费事件，不回写；② **天然解耦**——不进过点主事务（§5.3），允许秒级最终一致；③ **版本一致性能兜住**——工艺变更事件 `ProcessRouteActivated` 触发新版本节点入图，历史边不动，与 §5.1 的"过点记录绑定 `routeVersion`"一脉相承。
- 投影的可靠性复用既有消息基础设施：事件经各上下文的 Transactional Outbox 至少一次投递（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §1），RAG 侧 `event_id` 幂等消费（§5.3）。

### 2.4 部署形态（车间网隔离）

- 车间网络常与办公网隔离（[RAG服务引入路线.md](../RAG服务引入路线.md) §4 硬约束）。若走云端 LLM API，需在车间网关开白名单且接受外发延迟；若走本地化模型 + 本地图库，则完全离线。
- **建议**：图库（Neo4j）与 Embedding（bge-m3）本地化部署，LLM 视车间安全策略二选一——云端 API（质量高、需出网）或本地化模型（离线、质量折衷）。`BaseChatModel` 抽象保证两者切换零代码改动。
- 若本地化且需避开 Neo4j 商业条款，可替换为 NebulaGraph 或 Apache AGE（PostgreSQL 扩展），Cypher 子集兼容；本文代码骨架以 Neo4j Cypher 为准，替换时仅改 driver 方言。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ rag-service（独立微服务，Python + FastAPI + Neo4j）                │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ TraceRetrievalService                      │  │
│  │ /rag/trace/* │  │  种子解析 → 5M1E 子图扩展 → LLM 综合       │  │
│  └──────────────┘  └─────────────────┬────────────────────────┘  │
│                                      │                            │
│              ┌───────────────────────┼───────────────────────┐    │
│              ▼                       ▼                       ▼    │
│  ┌───────────────────┐  ┌─────────────────────┐  ┌──────────┐ │
│  │ GraphProjector    │  │ GraphRetriever      │  │ LLM      │ │
│  │ 事件 → 图增量写入  │  │ Cypher 5M1E 扩展    │  │ Synth    │ │
│  └────────┬──────────┘  └──────────┬──────────┘  └──────────┘ │
│           │                          │                          │
│  ┌────────▼────────┐        ┌────────▼─────────┐                │
│  │ Neo4j Graph     │        │ SeedResolver     │                │
│  │ (追溯图投影)     │◀──────▶│ (NL→种子, 向量)   │                │
│  └────────┬────────┘        └──────────────────┘                │
│           │ event_id 幂等 / 位点                              │
│  ┌────────▼────────┐  ┌─────────────────────┐                  │
│  │ Idempotency     │  │ consumer offset     │                  │
│  │ Table (MySQL)   │  │ (MySQL)             │                  │
│  └─────────────────┘  └─────────────────────┘                  │
└───────────────────────────────────┼──────────────────────────────┘
                                    │ 订阅领域事件（只读）
                          ┌─────────▼──────────┐
                          │ aiokafka Consumer   │
                          │ mes.* eam.* dc.*    │
                          │ process.* quality.* │
                          │ rework.* brework.*  │
                          │ wip.* repair.* ...  │
                          └─────────────────────┘
                                    ▲
                                    │ 各上下文 Outbox 投递（至少一次）
              ┌─────────────────────┴─────────────────────┐
              │  制造资源 / 设备管理 / 生产执行 三大服务    │
              │  （Java/Spring，事实源）                   │
              └───────────────────────────────────────────┘
```

### 3.1 关键设计决策

- **图即投影（CQRS 读模型）**：`GraphProjector` 是各上下文事件的读模型投影器，与过点执行上下文的本地缓存同构。事实源永远是 MES 服务的聚合根，图库崩溃不影响生产，重建即可（事件回放）。
- **事件驱动增量 + 位点管理**：消费者维护 `consumer offset` 落 MySQL，重启从断点续跑；`event_id` 幂等表保证重复投递不产生重复边（§5.3）。
- **检索与投影分离**：`GraphProjector`（写图）与 `GraphRetriever`（读图）解耦，投影滞后不阻塞检索——检索带 `as_of` 时间窗，滞后时段内低置信度兜底（§10.3）。
- **ACL 防腐层**：降级查询各上下文 REST 时经 ACL 适配，外部 DTO → 内部视图（`TraceNode` / `ProcessVersionSnapshot`），外部 schema 变化不污染检索核心。符合 CLAUDE.md 的低耦合 / ACL 约束。

---

## 4. 图谱建模：对齐 14 个限界上下文

图谱的节点 = 各上下文的**聚合根实例**，边 = 聚合之间的**跨上下文引用** + **时序流转**。建模严格对齐 [领域总览.md](../../领域模型/领域总览.md) §2 的 14 个限界上下文，不另造一套分类——上下文边界即节点分区边界，权限与版本过滤都跟着上下文走。

### 4.1 节点类型（按限界上下文）

每个节点带统一属性：`node_id`（上下文内唯一）、`bounded_context`、`tenant_scope`（workshop/line，权限过滤用）、`source_event_id`（创建该节点的事件，溯源用）、`occurred_at`、`version`（仅版本化聚合）。

| 限界上下文 | 节点标签 | 源聚合根 | 关键属性 | 版本化 |
|-----------|---------|---------|---------|--------|
| 工单管理 | `WorkOrder` | WorkOrder | work_order_id, status, target_qty, bom_id, route_id | ✗（绑定快照） |
| 过点执行 | `CheckpointRecord` | CheckpointRecord | sn, work_order_id, station_id, equipment_id, fixture_id, **route_version**, decision, scanned_by | ✗（携带版本快照） |
| 过点执行 | `TestResult` | TestResult | test_id, sn, station_id, test_type, raw_verdict, measured_items | ✗ |
| 过点执行 | `RoutingProgress` | RoutingProgress | sn, current_step, **route_version**, status | ✗ |
| 过点执行 | `WorkOrderProgress` | WorkOrderProgress | work_order_id, completed/good/bad/reworked_qty | ✗ |
| 在制品追踪 | `WipUnit` | WipUnit | sn, work_order_id, **route_version**, status, position | ✗ |
| 在制品追踪 | `KitStatus` | KitStatus | work_order_id, kit_ready, missing_items | ✗ |
| 首件处理 | `FirstArticle` | FirstArticle | first_article_id, work_order_id, trigger, status, first_article_unit_id | ✗ |
| 返修 | `ReworkTask` | ReworkTask | task_id, sn, defect_reason, source_station, status, reentry_point | ✗ |
| 返工 | `BatchReworkOrder` | BatchReworkOrder | rework_order_id, **source_work_order_id**, sn_list, reentry_point, trigger_source | ✗ |
| 物料 | `Material` | Material | part_no | ✗ |
| 物料 | `Product` | Product | product_id | ✗ |
| 物料 | `Bom` | Bom | bom_id, **bom_version**, bom_type, status | ✓ |
| 物料 | `InventoryBatch` | Inventory | part_no, batch_no/lot_no, location, supplier_id | ✗ |
| 物料 | `SubstituteRule` | SubstituteRule | rule_id, primary_part_no, substitute_part_nos | ✗ |
| 物料 | `Supplier` | Supplier | supplier_id | ✗ |
| 工艺管理 | `RouteVersion` | RouteVersion | route_id, **route_version**, route_type, status | ✓ |
| 工艺管理 | `RouteStep` | RouteStep（实体） | step_no, operation_id, station_type, is_reentry_point | 随路线版本 |
| 工艺管理 | `Operation` | Operation | operation_id | ✗ |
| 质量 | `QualityVerdict` | QualityVerdict | verdict_id, sn, station_id, business_verdict, defect_records, **rule_version** | ✗（带规则版本） |
| 质量 | `DefectCatalog` | DefectCatalog | defect_code, name, severity | ✗ |
| 质量 | `QualityGateRule` | QualityGateRule | rule_id, **rule_version**, status | ✓ |
| 质量 | `BatchQualityAnomaly` | BatchQualityAnomaly | anomaly_id, work_order_id, defect_code, affected_sn_list | ✗ |
| 设备工装台账 | `Asset` | Equipment/Fixture/Instrument | asset_id, asset_kind, status | ✗ |
| 设备工装台账 | `AssetSpecProfile` | AssetSpecificationProfile | spec_keys, spec_values | ✗ |
| 设备数据接入 | `EquipmentChannel` | Equipment(采集侧) | equipment_id, online_status, last_seen_at | ✗ |
| 维修 | `RepairOrder` | RepairOrder | order_id, asset_id, severity, status | ✗ |
| 点检保养 | `MaintenanceTask` | InspectionTask/MaintenanceTask | task_id, asset_id, verdict, metric_reset | ✗ |
| 计量检定 | `CalibrationCert` | CalibrationCertificate | cert_no, instrument_id, valid_until, status | ✗ |

> **节点不入图原则**：设备数据接入上下文的原始 `DataPacket`（[设备数据接入.md](../../领域模型/设备管理服务/事件风暴/设备数据接入上下文.md) §3.5，设计容量 ~1 万报文/秒）**不逐条入图**——它属高频采集流，进图会撑爆图库且与过点解耦（§5.3）。只把 `SerialNumberMarked` / `EquipmentRunHourAggregated` / `EquipmentAlarmRaised` 这类**已结构化、已瘦身**的语义事件投影成 `EquipmentChannel` 节点与少量告警边，原始曲线 / 固件文件走对象存储，图里只挂 URI。

---

### 4.2 边类型（跨上下文引用 = 图边）

边是图谱的灵魂——本 MES 的护城河就建立在 `source_work_order_id`、`routeVersion`、`asset_id`、`batch_no` 这些跨上下文引用上（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.1）。把它们显式建成图边，5M1E 串联才能一跳到位。边分四类：

**A. 引用边（聚合间显式引用，建图主干）**

| 边类型 | 起点 → 终点 | 源字段 | 带版本属性 |
|--------|------------|--------|-----------|
| `BELONGS_TO` | WipUnit → WorkOrder | work_order_id | — |
| `BINDS_BOM` | WorkOrder → Bom | bom_binding.bom_id | bom_version |
| `BINDS_ROUTE` | WorkOrder → RouteVersion | route_binding.route_id | route_version |
| `REWORKS_FROM` | BatchReworkOrder → WorkOrder | source_work_order_id | — |
| `FOR_UNIT` | CheckpointRecord → WipUnit | sn + work_order_id | — |
| `SNAPSHOT_OF_ROUTE` | CheckpointRecord → RouteVersion | route_version | route_version（INV-09） |
| `USED_EQUIPMENT` | CheckpointRecord → Asset | equipment_id | — |
| `USED_FIXTURE` | CheckpointRecord → Asset | fixture_id | — |
| `PRODUCED_TESTRESULT` | CheckpointRecord → TestResult | 同事务 | — |
| `JUDGED_BY` | TestResult → QualityVerdict | source_test_result_id | — |
| `CITES_DEFECT` | QualityVerdict → DefectCatalog | defect_records[].defect_code | — |
| `UNDER_RULE` | QualityVerdict → QualityGateRule | rule_id | rule_version |
| `ROUTES_FROM` | ReworkTask → CheckpointRecord | source_event_id（UnitRoutedToRework） | — |
| `REENTERS_AT` | ReworkTask → RouteStep | reentry_point | — |
| `CONSUMED_BATCH` | WipUnit → InventoryBatch | 过点消耗（MaterialConsumed） | — |
| `SUPPLIED_BY` | InventoryBatch → Supplier | supplier_id | — |
| `SUBSTITUTE_OF` | InventoryBatch → SubstituteRule | 替代料命中 | — |
| `AFFECTS` | BatchQualityAnomaly → WipUnit | affected_sn_list | — |
| `BINDS_ASSET` | EquipmentChannel → Asset | equipment_id（台账颁发） | — |

**B. 时序边（追溯链骨干）**

| 边类型 | 起点 → 终点 | 说明 |
|--------|------------|------|
| `NEXT` | CheckpointRecord → CheckpointRecord | 同一 SN 按 `occurred_at` 排序的过点序列；5M1E 串联的骨架 |
| `REENTERED_FROM` | CheckpointRecord → ReworkTask | 返修/返工再入点（`reentered_from=Rework`） |

**C. 配置绑定边（工艺/质量规则，版本化）**

| 边类型 | 起点 → 终点 | 源字段 | 带版本属性 |
|--------|------------|--------|-----------|
| `HAS_STEP` | RouteVersion → RouteStep | 路线步骤序列 | route_version |
| `USES_OPERATION` | RouteStep → Operation | operation_id | — |
| `ENFORCES_GATE` | RouteStep → QualityGateRule | quality_gate_rule_id | rule_version |
| `REQUIRES_SPEC` | RouteStep → AssetSpecProfile | spec_keys | — |
| `HAS_BOM_ITEM` | Bom → Material | BomItem.part_no | bom_version |

**D. 运维边（设备视角的 5M1E Machine 维度）**

| 边类型 | 起点 → 终点 | 源字段 |
|--------|------------|--------|
| `REPAIRS` | RepairOrder → Asset | asset_id |
| `SUGGESTS_SCRAP` | RepairOrder → ScrapApplication | ScrapRecommendation |
| `INSPECTS` | MaintenanceTask → Asset | asset_id |
| `CERTIFIES` | CalibrationCert → Asset | instrument_id |

> **所有边不可变**：边一旦写入不修改、不删除（与 `CheckpointRecord` INV-12 不可变一致）。工艺版本变更只新增 `RouteVersion` 节点与新 `BINDS_ROUTE` 边，**不改动历史 `SNAPSHOT_OF_ROUTE` 边**——这是版本一致性能兜住的根因（§4.4）。

### 4.3 5M1E 维度建模

从种子节点（通常是 `WipUnit{sn}`）出发，5M1E 每个维度对应一组确定的一跳/两跳扩展，不需要 LLM 猜路径：

```mermaid
graph LR
    SEED(("种子<br/>WipUnit{sn}"))

    SEED -->|FOR_UNIT| CR[CheckpointRecord]
    SEED -->|BELONGS_TO| WO[WorkOrder]
    SEED -->|CONSUMED_BATCH| IB[InventoryBatch]

    CR -->|SNAPSHOT_OF_ROUTE / route_version| RV[RouteVersion]
    CR -->|USED_EQUIPMENT| EQ[Asset 设备]
    CR -->|USED_FIXTURE| FX[Asset 工装]
    CR -->|PRODUCED_TESTRESULT| TR[TestResult]
    CR -->|NEXT| CR2[下一站 CheckpointRecord]

    RV -->|HAS_STEP| RS[RouteStep]
    RS -->|ENFORCES_GATE| QGR[QualityGateRule]
    RS -->|REQUIRES_SPEC| SP[AssetSpecProfile]

    TR -->|JUDGED_BY| QV[QualityVerdict]
    QV -->|CITES_DEFECT| DC[DefectCatalog]
    QV -->|UNDER_RULE| QGR

    IB -->|SUPPLIED_BY| SUP[Supplier]
    IB -->|SUBSTITUTE_OF| SR[SubstituteRule]

    EQ -->|REPAIRS| RO[RepairOrder]
    EQ -->|INSPECTS| MT[MaintenanceTask]
    EQ -->|CERTIFIES| CC[CalibrationCert]

    WO -->|BINDS_BOM| BOM[Bom]
    WO -->|BINDS_ROUTE| RV

    classDef man fill:#ffe0e6,stroke:#d63384;
    classDef machine fill:#e6f0ff,stroke:#1c7ed6;
    classDef material fill:#fff4e6,stroke:#f08c00;
    classDef method fill:#e6ffe6,stroke:#2f9e44;
    classDef measure fill:#f3e6ff,stroke:#7048e8;
    class CR,CR2,WO:::man;
    class EQ,FX,RO,MT,CC,SP:::machine;
    class IB,SUP,SR,BOM:::material;
    class RV,RS,QGR:::method;
    class TR,QV,DC:::measure;
```

| 5M1E | 扩展路径（从种子 SN） | 命中上下文 |
|------|---------------------|-----------|
| **Man** | `WipUnit → CheckpointRecord.scanned_by`、`FirstArticle` 放行人、`ReworkTask` 诊断人 | 过点执行 / 首件 / 返修 |
| **Machine** | `CheckpointRecord → USED_EQUIPMENT/USED_FIXTURE → Asset`；`Asset → REPAIRS/INSPECTS/CERTIFIES` | 设备工装台账 / 维修 / 点检保养 / 计量检定 |
| **Material** | `WipUnit → CONSUMED_BATCH → InventoryBatch → SUPPLIED_BY/SUBSTITUTE_OF`；`WorkOrder → BINDS_BOM → Bom` | 物料 / 在制品追踪 |
| **Method** | `CheckpointRecord → SNAPSHOT_OF_ROUTE{route_version} → RouteVersion → HAS_STEP → RouteStep → ENFORCES_GATE/REQUIRES_SPEC` | 工艺管理 / 质量 |
| **Measurement** | `CheckpointRecord → PRODUCED_TESTRESULT → TestResult → JUDGED_BY → QualityVerdict → CITES_DEFECT/UNDER_RULE` | 过点执行 / 质量 |
| **Environment** | `EquipmentChannel`（老化房采样语义事件）+ 焊接站温区 `DataPacket` 的 URI 引用 | 设备数据接入 |

---

### 4.4 版本快照节点（routeVersion / bomVersion / rule_version）

本 MES 的工艺路线、BOM、质量门禁规则都有**版本生命周期**（[领域总览.md](../../领域模型/领域总览.md) §5.1；工艺管理上下文 `RouteVersionState`：DRAFT/SUBMITTED/ACTIVATED/DEPRECATED/ARCHIVED）。图谱对版本的处理是**版本即节点**，不覆盖：

- `RouteVersion{route_id, route_version}` 是独立节点，`status` 属性标记 `ACTIVATED`/`DEPRECATED`。新版本生效 → 新增节点，旧节点 `status` 改 `DEPRECATED` 但**不删除**。
- `CheckpointRecord` 通过 `[:SNAPSHOT_OF_ROUTE {route_version}]` 边指向**当时生产用的版本**（INV-09）。工艺变更后，旧过点记录的边仍指向旧版本节点——历史追溯按当时版本回放，不受新版本影响。
- `WorkOrder` 的 `BINDS_ROUTE` / `BINDS_BOM` 边携带 `route_version` / `bom_version` 属性，锁定的版本在工单 `ReviewAndReleaseWorkOrder` 时固化（工单管理 INV-07：RELEASED/IN_PROGRESS 后禁止变更）。
- `QualityVerdict` 的 `UNDER_RULE` 边携带 `rule_version`，保证判定结果可回溯到当时生效的规则。

> 这是和通用 RAG 最大的区别，也是面试最该展开的点：通用 RAG 检索文档不分版本，可能答出已失效工艺；本文的图把版本做成显式节点 + 快照边，检索时带 `route_version` 过滤（§6.4），从结构上杜绝"错给一条已失效工艺导致批量不良"。

### 4.5 不可变性与时间维度

- **历史节点不可变**：`CheckpointRecord`、`TestResult`、`QualityVerdict`、`WipHistoryEntry`（在制品追踪 INV-02：只增不可改不可删）一旦写入永不修改——与过点执行上下文 INV-12（过点记录不可变）一致。
- **边不可变**：引用边、时序边、配置绑定边一旦建立不修改。版本变更通过新增节点 + 新边表达，不改动旧边。
- **时间维度**：每条边带 `occurred_at`，`NEXT` 边按时间排序构成 SN 的过点时间轴。检索支持 `as_of` 时间窗——"截至昨天 18 点这条单件的状态"，用于复盘。
- **图重建**：因节点/边全不可变且由事件驱动，图库可从 Kafka 事件回放完整重建（§5.1 投影幂等），无需 MES 侧配合。

---

## 5. 索引构建：领域事件流驱动

图的写入完全由领域事件驱动，`GraphProjector` 是各上下文事件的**读模型投影器**。这一节定义"事件 → 图增量"的投影规则。

### 5.1 事件 → 图增量更新（投影规则）

每个领域事件对应一个投影处理器，幂等地 upsert 节点与边。投影规则按"事件创建/更新什么节点 + 建什么边"描述：

| 领域事件 | 来源上下文 / 主题 | 投影动作（节点 + 边） |
|---------|-------------------|----------------------|
| `WorkOrderCreated` / `WorkOrderBindingLocked` | 工单管理 `wo.*` | upsert `WorkOrder`；建 `BINDS_BOM{bom_version}` → `Bom`、`BINDS_ROUTE{route_version}` → `RouteVersion` |
| `WipUnitRegistered` | 在制品追踪 `wip.*` | upsert `WipUnit{sn, route_version}`；建 `BELONGS_TO` → `WorkOrder` |
| `CheckpointScanned` / `CheckpointReleased` | 过点执行 `mes.checkpoint.lifecycle` | upsert `CheckpointRecord{route_version, decision}`；建 `FOR_UNIT` → `WipUnit`、`USED_EQUIPMENT` → `Asset`、`SNAPSHOT_OF_ROUTE{route_version}` → `RouteVersion`、`NEXT` → 上一条 `CheckpointRecord` |
| `TestResultStructured` | 过点执行 `mes.testresult.structured` | upsert `TestResult`；建 `PRODUCED_TESTRESULT` ← `CheckpointRecord` |
| `RoutingProgressed` | 过点执行 `mes.routing.progress` | upsert `RoutingProgress{current_step}`；建 `AT_STEP` → `RouteStep` |
| `QualityVerdictIssued` | 质量 `quality.*` | upsert `QualityVerdict`；建 `JUDGED_BY` ← `TestResult`、`CITES_DEFECT` → `DefectCatalog`、`UNDER_RULE{rule_version}` → `QualityGateRule` |
| `MaterialConsumed` / `InventoryChanged` | 物料 `material.*` | upsert `InventoryBatch`；建 `CONSUMED_BATCH` ← `WipUnit`、`SUPPLIED_BY` → `Supplier` |
| `BomActivated` / `SubstituteRuleActivated` | 物料 `material.*` | upsert `Bom{bom_version, status=ACTIVATED}`；旧版本节点 `status=DEPRECATED`（不删） |
| `ProcessRouteActivated` | 工艺管理 `process.*` | upsert `RouteVersion{route_version, status=ACTIVATED}`；旧版本 `DEPRECATED`；触发文档型 RAG（路线 B）重索引（§5.4） |
| `RouteStepConfigured` / `QualityGateBound` | 工艺管理 `process.*` | upsert `RouteStep`；建 `HAS_STEP` ← `RouteVersion`、`ENFORCES_GATE` → `QualityGateRule`、`REQUIRES_SPEC` → `AssetSpecProfile` |
| `UnitRoutedToRework` | 过点执行 `mes.unit.routed-to-rework` | upsert `ReworkTask`；建 `ROUTES_FROM{source_event_id}` ← `CheckpointRecord` |
| `UnitReworkCompleted` | 返修 `rework.*` | 建 `REENTERS_AT` → `RouteStep`；建 `REENTERED_FROM` ← `CheckpointRecord` |
| `BatchReworkOrderCreated` / `Released` | 返工 `brework.*` | upsert `BatchReworkOrder`；建 `REWORKS_FROM` → `WorkOrder{source_work_order_id}` |
| `FirstArticleReleased` / `Blocked` | 首件 `fai.*` | upsert `FirstArticle`；建 `TRIGGERED_BY` → 触发事件 |
| `AssetCommissioned` / `AssetAvailabilityChanged` | 台账 `eam.*` | upsert `Asset` / `AssetSpecProfile` |
| `SerialNumberMarked` / `EquipmentRunHourAggregated` | 设备数据接入 `dc.*` | upsert `EquipmentChannel`；建 `BINDS_ASSET` → `Asset`（**仅语义事件，不接原始 DataPacket**） |
| `RepairOrderCreated` / `RepairCompleted` | 维修 `repair.*` | upsert `RepairOrder`；建 `REPAIRS` → `Asset` |
| `InspectionCompleted` / `FixtureLifeExceeded` | 点检保养 `pm.*` | upsert `MaintenanceTask`；建 `INSPECTS` → `Asset` |
| `CalibrationCertificateIssued` / `CalibrationExpired` | 计量检定 `calibration.*` | upsert `CalibrationCert`；建 `CERTIFIES` → `Asset` |
| `BatchQualityAnomalyDetected` | 质量 `quality.*` | upsert `BatchQualityAnomaly`；建 `AFFECTS` → `WipUnit`（按 affected_sn_list） |

> **投影器是领域服务的镜像**：每个投影处理器只处理自己上下文的事件，互不干涉——与过点执行上下文 §2.7"只消费不重发、按主题前缀隔离"完全同构。处理器注册到 `ProjectionDispatcher`，按主题路由。

---

### 5.2 订阅拓扑与位点管理

- **按服务前缀分消费者组**：`rag-service` 按主题前缀起多个消费者组，避免单组拉全部主题导致积压——`rag-mes`（`mes.*`）、`rag-wo`（`wo.*`）、`rag-wip`（`wip.*`）、`rag-process`（`process.*`）、`rag-quality`（`quality.*`）、`rag-material`（`material.*`）、`rag-eam`（`eam.*`）、`rag-dc-semantic`（仅 `dc.identity.sn.minted` / `dc.equipment.runtime` / `dc.equipment.alarm.raw` 三个**语义**主题，不全量订 `dc.*`）、`rag-rework` / `rag-brework` / `rag-fai` / `rag-repair` / `rag-pm` / `rag-calibration`。
- **位点落 MySQL**：每消费者组维护 `consumer_offset`（topic + partition + offset）落 `index_offset` 表，重启从断点续跑，不依赖 Kafka 自动提交——崩溃窗口可回退到上次成功位点重放（幂等兜住重复）。
- **手动 ack**：投影事务（图 upsert + 幂等记录 + 位点更新）成功后才 ack offset，与 [消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §6 的"业务处理与幂等记录同事务、手动 ack"一致。
- **dc.* 限流**：`rag-dc-semantic` 只订三个语义主题，物理上无法消费原始 `DataPacket` 流——从订阅拓扑层面兜住"高频采集不全量入图"红线（§1.2）。

### 5.3 幂等与去重

事件经各上下文 Transactional Outbox **至少一次**投递（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §1），RAG 侧必须幂等消费，否则重复投递会产生重复边。

```sql
-- 幂等表：event_id + consumer_group 唯一键
CREATE TABLE index_idempotency (
  event_id        VARCHAR(64)  NOT NULL,
  consumer_group  VARCHAR(64)  NOT NULL,
  topic           VARCHAR(128) NOT NULL,
  projected_at    DATETIME(3)  NOT NULL,
  PRIMARY KEY (event_id, consumer_group)
);
```

- **投影事务**：`图 upsert` + `INSERT index_idempotency` 在 Neo4j 与 MySQL 间**非分布式**——Neo4j 写图后写幂等表，若幂等键冲突说明已投影，跳过图写入并 ack。崩溃重放时重复投递被幂等表挡住，图不产生重复边。
- **MERGE 幂等**：Neo4j 侧所有节点/边写入用 `MERGE`（按 `node_id` / 边端点 + `source_event_id` 去重），即使幂等表漏挡，图层面也不重复。
- **位点上移**：幂等记录与位点更新同 MySQL 事务，保证"已投影 ⇒ 已 ack"。

### 5.4 版本失效与重索引

工艺版本变更是 MES 上 RAG 最危险的环节——检索到已失效工艺会直接导致批量不良（[RAG服务引入路线.md](../RAG服务引入路线.md) §5 面试 Q&A）。本文从三个层面兜住：

1. **图层面（节点不删）**：`ProcessRouteActivated` 投影时，新 `RouteVersion{route_version, status=ACTIVATED}` 入图，旧版本节点 `status` 改 `DEPRECATED` 但**不删除**——历史 `SNAPSHOT_OF_ROUTE` 边仍指向旧版本，历史追溯不受影响（INV-09）。
2. **检索层面（版本过滤）**：检索 `RouteVersion` 时带 `status=ACTIVATED` 过滤；查历史单件时按 `CheckpointRecord.route_version` 精确定位当时版本，不取"当前生效版"——§6.4 强制版本入参。
3. **文档层面（重索引）**：`ProcessRouteActivated` 同时触发文档型 RAG（路线 B）重索引关联的 SOP / 作业指导书，保证文档检索结果与生产执行侧工艺缓存版本一致（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.2）。本文通过发布内部 `rag.reindex.request` 事件通知路线 B。

> 这条要讲清楚：**版本一致性不是 RAG 自己保证的，是从领域模型兜上来的**——过点记录绑 `routeVersion`（§5.1）、工艺版本有生命周期、变更事件驱动重索引。RAG 只是严格遵循这套契约，不另搞一套版本管理。

---

## 6. 检索与推理：5M1E 自动串联

### 6.1 检索入口（种子解析）

检索从**种子节点**出发。种子可由用户直接给（`sn` / `batch_no` / `work_order_id` / `asset_id`），也可由自然语言问题经 `SeedResolver` 解析得到：

```text
用户问题："SN-001 昨天焊接不良，根因？"
        │
        ▼
SeedResolver（NL → 种子）
  ├─ 实体抽取：SN-001（正则命中 SN 规则）
  ├─ 意图识别：根因诊断（5M1E 扩展）
  └─ 时间窗：昨天 → as_of = 今日 00:00
        │
        ▼
seed = {kind: "WipUnit", sn: "SN-001", as_of: <今日00:00>}
```

- **实体抽取**优先规则匹配（SN / 工单号 / 批次号都有编码规则，正则即可），命中不了再走 LLM 抽取——降低对模型的依赖，提升确定性。
- **语义兜底**：缺陷描述（"焊接不良"→ `defect_code=SW-001`）走 Embedding（bge-m3）在 `DefectCatalog` 节点的向量索引上近邻检索，把自然语言缺陷描述映射到标准缺陷码。
- **多种子**：问题可能涉及批次（"B-77 这批锡膏进了哪些单件"），种子是 `InventoryBatch`，扩展方向反转（`CONSUMED_BATCH` 反向）。

### 6.2 5M1E 子图扩展（Cypher）

种子确定后，5M1E 扩展是确定性的 Cypher 一跳/两跳查询，不需要 LLM 猜路径。以下是从 `WipUnit{sn}` 出发的 5M1E 全量扩展（实际可按维度拆分懒加载）：

```cypher
// 参数：$sn, $tenant_scopes, $as_of, $route_version（可选，锁定历史版本）
MATCH (w:WipUnit {sn: $sn})
WHERE w.tenant_scope IN $tenant_scopes
  AND w.occurred_at <= $as_of
// Man / Measurement：过点序列 + 测试结果 + 质量判定
OPTIONAL MATCH (cr:CheckpointRecord)-[:FOR_UNIT]->(w)
  WHERE cr.occurred_at <= $as_of
OPTIONAL MATCH (cr)-[:PRODUCED_TESTRESULT]->(t:TestResult)
OPTIONAL MATCH (t)-[:JUDGED_BY]->(qv:QualityVerdict)
OPTIONAL MATCH (qv)-[:CITES_DEFECT]->(dc:DefectCatalog)
OPTIONAL MATCH (qv)-[ur:UNDER_RULE]->(qgr:QualityGateRule)
// Machine：设备 / 工装 + 运维历史
OPTIONAL MATCH (cr)-[:USED_EQUIPMENT]->(eq:Asset)
OPTIONAL MATCH (cr)-[:USED_FIXTURE]->(fx:Asset)
OPTIONAL MATCH (ro:RepairOrder)-[:REPAIRS]->(eq)
OPTIONAL MATCH (mt:MaintenanceTask)-[:INSPECTS]->(eq)
OPTIONAL MATCH (cc:CalibrationCert)-[:CERTIFIES]->(eq)
// Method：当时工艺版本快照 + 步骤 + 门禁（版本一致性核心）
OPTIONAL MATCH (cr)-[sr:SNAPSHOT_OF_ROUTE]->(rvSnap:RouteVersion)
OPTIONAL MATCH (rvSnap)-[:HAS_STEP]->(rs:RouteStep)
OPTIONAL MATCH (rs)-[eg:ENFORCES_GATE]->(qgrStep:QualityGateRule)
// Material：消耗批次 + 供应商 + 替代料
OPTIONAL MATCH (w)-[:CONSUMED_BATCH]->(ib:InventoryBatch)
OPTIONAL MATCH (ib)-[:SUPPLIED_BY]->(sup:Supplier)
OPTIONAL MATCH (ib)-[:SUBSTITUTE_OF]->(sub:SubstituteRule)
RETURN w,
       collect(DISTINCT cr { .node_id, .station_id, .scanned_by, .decision, .occurred_at, .route_version }) AS man_checkpoints,
       collect(DISTINCT t { .test_id, .test_type, .raw_verdict }) AS measurements,
       collect(DISTINCT qv { .verdict_id, .business_verdict }) + collect(DISTINCT dc { .defect_code, .severity }) AS measurement_verdicts,
       collect(DISTINCT eq { .asset_id, .status }) + collect(DISTINCT ro { .order_id, .severity, .status }) AS machines,
       collect(DISTINCT rvSnap { .route_id, .route_version, .status }) AS method_route,
       collect(DISTINCT rs { .step_no, .operation_id }) AS method_steps,
       collect(DISTINCT ib { .batch_no, .location }) + collect(DISTINCT sup { .supplier_id }) AS materials
```

- **`route_version` 锁定**：`SNAPSHOT_OF_ROUTE` 边的 `route_version` 来自 `CheckpointRecord` 本身（INV-09），不取"当前生效版"——保证历史单件按当时工艺回放。若用户问"当前工艺"则另走 `status=ACTIVATED` 过滤的查询。
- **`tenant_scope` 前置过滤**：`WHERE w.tenant_scope IN $tenant_scopes` 在扩展前裁剪，权限不达标看不到节点，不是答完再裁剪（§1.2）。
- **`as_of` 时间窗**：所有节点按 `occurred_at <= $as_of` 过滤，支持"截至昨天"复盘。

---

### 6.3 检索结果结构化（TraceSubgraph）

检索返回的子图不是自由文本，而是**结构化 DTO**（Pydantic 强约束），既给 LLM 做综合的上下文，也给工程师 UI 直接渲染证据链：

```python
class TraceNode(BaseModel):
    label: str                       # "CheckpointRecord" / "Asset" ...
    bounded_context: str             # "过点执行上下文"
    node_id: str
    props: dict[str, Any]            # 节点属性
    source_event_id: str             # 创建该节点的事件（证据回溯）

class TraceEdge(BaseModel):
    rel: str                         # "USED_EQUIPMENT" / "SNAPSHOT_OF_ROUTE"
    from_id: str
    to_id: str
    version: str | None = None       # route_version / bom_version / rule_version

class FiveM1ECluster(BaseModel):
    man: list[TraceNode]             # 过点记录、首件放行人
    machine: list[TraceNode]         # 设备、工装、维修/点检/计量
    material: list[TraceNode]        # 批次、供应商、替代料
    method: list[TraceNode]          # 工艺版本快照、步骤、门禁
    measurement: list[TraceNode]     # TestResult、QualityVerdict、缺陷
    environment: list[TraceNode]     # 设备数据接入语义采样

class TraceSubgraph(BaseModel):
    seed: TraceNode
    clusters: FiveM1ECluster
    edges: list[TraceEdge]
    as_of: datetime
    projection_lag_ms: int           # 图投影滞后（检索时与最新事件的差值）
```

- `source_event_id` 让每个证据节点可回溯到领域事件——工程师点击证据能跳到原始事件 / 过点记录。
- `projection_lag_ms` 暴露图投影的滞后程度，滞后过大时降置信度（§10.3）。

### 6.4 LLM 综合与输出

`TraceRetrievalService` 把 `TraceSubgraph` + 用户问题交给 LLM，产出带证据的 5M1E 假设排序：

```python
class RootCauseHypothesis(BaseModel):
    category: FiveM1ECategory        # Material / Machine / Method / Measurement / Man / Environment
    rank: int
    statement: str
    evidence: list[str]              # ["node_id=CheckpointRecord:xxx", "defect_code=SW-001"]
    suggested_action: str

class TraceAnswer(BaseModel):
    summary: str
    confidence: float                # 0.0 ~ 1.0
    hypotheses: list[RootCauseHypothesis]
    subgraph_ref: str                # 指向落库的 TraceSubgraph
    disclaimer: str = "本答案为追溯型 RAG 的辅助假设，最终处置需工程师确认"
    needs_human_review: bool = False
```

- **5M1E 分类固化在 Enum**，模型只能选枚举值，避免乱编类别（与 L1 Agent 一致）。
- **证据强制引用 `node_id`**：每个假设必须引用子图节点，无证据的假设判失败重试。
- **置信度阈值**：`confidence < 0.6` 或 `projection_lag_ms` 超阈值 → `needs_human_review=True`，不展示给操作工，只推工程师。
- **系统提示词约束**：明确告诉模型"只能基于提供的 TraceSubgraph 推理，不得编造未在子图中出现的节点；查工艺必须带 `route_version`；输出严格遵循 TraceAnswer 结构"。

### 6.5 与文档型 RAG、L1 Agent 的协同

- **与路线 B（文档型 RAG）协同**：追溯型 RAG 给"结构化事实链"（哪批锡膏、哪台设备），文档型 RAG 给"处置知识"（SPI 报警怎么处置、IPC 标准）。`TraceAnswer` 的 `suggested_action` 可调路线 B 的 `search_docs(query, route_version_filter)` 补 SOP 片段——两者版本过滤都对齐 `ProcessRouteActivated`（§5.4）。
- **与 L1 Agent 协同**：L1 的 `query_traceability_graph` 工具封装本文 `POST /rag/trace/query`；L1 在图检索基础上做**多步递进追问**（图给全貌，Agent 深挖某一维），本文是其快路径（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §5.5）。
- **不互相替代**：图检索是"一次性给齐 5M1E"，Agent 是"一步步问下去"。简单根因用图够了，复杂跨上下文递进用 Agent。

---

## 7. 实现方案

### 7.1 索引构建管线（GraphProjector）

`GraphProjector` 是事件 → 图增量的投影器，按主题前缀分派到各上下文的 `ProjectionHandler`：

```python
class ProjectionHandler(Protocol):
    """一个上下文一个投影处理器，处理本上下文事件 → 图增量。"""
    bounded_context: str
    async def handle(self, event: DomainEvent, tx: AsyncGraphTransaction) -> None: ...

class GraphProjector:
    """消费领域事件，幂等投影到 Neo4j。"""

    def __init__(
        self,
        handlers: dict[str, ProjectionHandler],   # 按 topic 前缀路由
        graph: AsyncNeo4jDriver,
        idem_repo: IdempotencyRepo,
        offset_repo: OffsetRepo,
        metrics: MetricsCollector,
    ) -> None:
        self._handlers = handlers
        self._graph = graph
        self._idem = idem_repo
        self._offset = offset_repo
        self._metrics = metrics

    async def consume(self, msg: ConsumerRecord, group: str) -> None:
        event = DomainEvent.model_validate_json(msg.value)
        # 1. 幂等检查：event_id + consumer_group
        if await self._idem.exists(event.event_id, group):
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        # 2. 路由到对应上下文处理器
        handler = self._handlers.get(msg.topic.split(".")[0])  # 前缀路由
        if handler is None:
            self._metrics.unknown_topic.inc(msg.topic)
            await self._offset.advance(...)  # 不认识的主题跳过但不阻塞
            return
        # 3. 图事务内投影（MERGE 保证幂等）
        async with self._graph.session() as session:
            await session.execute_write(
                lambda tx: handler.handle(event, tx)
            )
        # 4. 幂等记录 + 位点推进（同 MySQL 事务）
        await self._idem.record(event.event_id, group, msg.topic)
        await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
        self._metrics.projected.inc(handler.bounded_context)
```

- 处理器内部用 Cypher `MERGE` 写节点/边，按 `node_id` 与边端点 + `source_event_id` 去重——即使幂等表漏挡，图层面也不重复（§5.3 双层幂等）。
- 处理器按上下文隔离，新增上下文事件只需新增一个 `ProjectionHandler`，不改动检索侧（OCP）。

### 7.2 检索服务（TraceRetrievalService）

```python
class TraceRetrievalService:
    def __init__(
        self,
        retriever: GraphRetriever,
        seed_resolver: SeedResolver,
        llm: BaseChatModel,
        subgraph_repo: SubgraphRepo,
        cache: SubgraphCache,
    ) -> None:
        self._retriever = retriever
        self._seed_resolver = seed_resolver
        self._llm = llm
        self._subgraph_repo = subgraph_repo
        self._cache = cache

    async def query(
        self, request: TraceQuery, tenant: TenantContext
    ) -> TraceAnswer:
        # 1. 种子解析（NL → seed，或直接用 request.seed）
        seed = await self._seed_resolver.resolve(request.question, tenant)
        # 2. 子图缓存（同种子 + as_of 命中即用）
        cached = await self._cache.get(seed, request.as_of, tenant)
        if cached:
            subgraph = cached
        else:
            # 3. 5M1E 子图扩展（Cypher，带 tenant + version + as_of 过滤）
            subgraph = await self._retriever.expand_5m1e(seed, request.as_of, tenant)
            await self._subgraph_repo.save(subgraph)
            await self._cache.set(seed, request.as_of, tenant, subgraph)
        # 4. LLM 综合（结构化输出）
        answer = await self._synthesize(request.question, subgraph)
        answer.subgraph_ref = subgraph.seed.node_id
        return answer

    async def _synthesize(self, question: str, subgraph: TraceSubgraph) -> TraceAnswer:
        prompt = self._build_prompt(question, subgraph)
        # structured_output 强制模型返回 TraceAnswer schema
        return await self._llm.with_structured_output(TraceAnswer).ainvoke(prompt)
```

- 检索与综合分离：`GraphRetriever` 只管 Cypher 取子图，`_synthesize` 只管 LLM 综合——单一职责（SRP）。
- 子图缓存按"种子 + as_of + 租户"键，同问题重复检索不重跑 Cypher / LLM。

### 7.3 ACL 防腐层（降级查询）

图投影滞后或节点缺失时，`GraphRetriever` 经 ACL 降级查询对应上下文只读 REST 补齐（与过点执行上下文"缓存未命中降级远程查询"同构，§5.1）：

```python
class ProcessManagementAclClient:
    """工艺管理上下文只读 ACL，强制 route_version。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_route_version(
        self, route_id: str, route_version: str, tenant: TenantContext
    ) -> RouteVersionView:
        if not route_version:
            raise ValueError("route_version 必填，禁止查询无版本工艺（§5.1）")
        resp = await self._http.get(
            f"/api/process-routes/{route_id}",
            params={"version": route_version},
            headers=tenant.headers(),
            timeout=2.0,
        )
        resp.raise_for_status()
        dto = RouteVersionDTO.model_validate(resp.json())
        if dto.status != "ACTIVE" and not dto.allow_historical:
            raise ValueError(f"工艺版本 {route_version} 非生效状态: {dto.status}")
        return RouteVersionMapper.to_view(dto)
```

- 外部 DTO 不进检索核心，只暴露 `RouteVersionView`——防腐层核心职责（CLAUDE.md ACL 约束）。
- 降级查询是兜底，不进过点主事务（§5.3），超时降级为低置信度而非阻塞。

### 7.4 版本一致性保证

版本一致性靠三道闸，不靠口头约束：

1. **投影闸**：`ProcessRouteActivated` 投影新 `RouteVersion{status=ACTIVATED}`，旧版本 `DEPRECATED` 不删（§5.4）。
2. **检索闸**：`GraphRetriever.expand_5m1e` 查工艺时，从 `CheckpointRecord.route_version` 取版本，`SNAPSHOT_OF_ROUTE` 边定位当时版本节点——不取"当前生效版"。`ProcessManagementAclClient` 降级查询强制 `route_version` 入参。
3. **输出闸**：`TraceAnswer` 的假设证据必须含 `route_version`，模型不得给无版本指向的工艺建议；Pydantic 校验失败重试。

---

## 8. 推荐包结构（Python src layout）

```text
rag_service/
  app/
    api/                       # FastAPI 路由层
      trace_router.py          # /rag/trace/query, /rag/trace/expand
      schemas.py               # Request / Response 模型
    application/               # 应用服务，编排检索
      trace_retrieval_service.py
      seed_resolver.py         # NL → 种子（规则优先 + LLM 兜底）
    domain/                    # RAG 领域模型
      subgraph.py              # TraceSubgraph / TraceNode / TraceEdge / FiveM1ECluster
      answer.py                # TraceAnswer / RootCauseHypothesis / FiveM1ECategory
      seed.py                  # Seed / SeedKind
      tenant.py                # TenantContext
      projection.py            # ProjectionHandler 协议 / GraphProjector
    infrastructure/
      neo4j/                   # 图库
        driver.py              # AsyncNeo4jDriver 封装
        retriever.py           # GraphRetriever（Cypher 5M1E 扩展）
        projections/           # 各上下文投影处理器
          work_order.py
          checkpoint.py
          material.py
          process_route.py
          quality.py
          asset.py
          rework.py
          ...
      rag/                     # LlamaIndex PropertyGraphIndex 封装（可选上层）
        graph_index.py
      embedding/               # bge-m3 客户端（SeedResolver 语义兜底）
        bge_client.py
      ai/                      # LLM 客户端
        llm_factory.py
      acl/                     # 降级查询各上下文只读 REST
        process_management.py
        work_order.py
        material.py
        device_data.py
      kafka/                   # aiokafka 消费者（按主题前缀分组）
        consumer_group.py
        listeners.py
      persistence/             # SQLAlchemy 模型 + Repository
        models.py
        idempotency_repo.py
        offset_repo.py
        subgraph_repo.py
      redis_/                   # 子图缓存
        subgraph_cache.py
      obs/                     # OTel exporter、prometheus 指标
        tracing.py
        metrics.py
    config.py                  # pydantic-settings
    main.py                    # FastAPI app 入口 + lifespan 启动断言
  tests/
  pyproject.toml
```

- `domain/projection.ProjectionHandler` 是协议（ISP），每个上下文实现自己的处理器，互不强迫实现无关方法。
- `infrastructure/neo4j/projections/` 是投影器落地，每个上下文一个文件，符合 SRP——新增上下文事件只加文件不改既有。
- `infrastructure/acl/` 是防腐层，降级查询外部 REST，外部 DTO 经 Mapper 转内部视图，不污染检索核心。

---

## 9. 关键代码骨架

### 9.1 投影处理器示例（过点执行上下文）

```python
# app/infrastructure/neo4j/projections/checkpoint.py
class CheckpointProjectionHandler:
    """过点执行上下文事件 → 图增量。处理 CheckpointReleased / TestResultStructured 等。"""

    bounded_context = "过点执行上下文"

    async def handle(self, event: DomainEvent, tx: AsyncGraphTransaction) -> None:
        if event.event_type == "CheckpointReleased":
            await self._on_released(event, tx)
        elif event.event_type == "TestResultStructured":
            await self._on_test_result(event, tx)
        # ... CheckpointBlocked / RoutingProgressed / UnitRoutedToRework

    async def _on_released(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        p = e.payload
        # MERGE 节点（按 node_id 幂等）+ MERGE 边（带 route_version 版本属性）
        await tx.run(
            """
            MERGE (cr:CheckpointRecord {node_id: $node_id})
              SET cr.sn = $sn, cr.work_order_id = $wo_id, cr.station_id = $station_id,
                  cr.equipment_id = $eq_id, cr.route_version = $rv, cr.decision = $decision,
                  cr.scanned_by = $scanned_by, cr.occurred_at = $at,
                  cr.tenant_scope = $tenant, cr.source_event_id = $eid,
                  cr.bounded_context = '过点执行上下文'
            WITH cr
            MERGE (w:WipUnit {sn: $sn})
            MERGE (cr)-[:FOR_UNIT]->(w)
            WITH cr
            MATCH (prev:CheckpointRecord {sn: $sn})
            WHERE prev.occurred_at < $at
            WITH cr, prev ORDER BY prev.occurred_at DESC LIMIT 1
            MERGE (prev)-[:NEXT]->(cr)
            """,
            node_id=f"CheckpointRecord:{p['checkpoint_id']}",
            sn=p["sn"], wo_id=p["work_order_id"], station_id=p["station_id"],
            eq_id=p.get("equipment_id"), rv=p["route_version"],
            decision=p["decision"], scanned_by=p.get("scanned_by"),
            at=p["occurred_at"], tenant=p["tenant_scope"], eid=e.event_id,
        )
        # 版本快照边：指向当时工艺版本（INV-09，不取当前生效版）
        if p.get("route_version"):
            await tx.run(
                """
                MATCH (cr:CheckpointRecord {node_id: $nid})
                MERGE (rv:RouteVersion {route_id: $rid, route_version: $rv})
                MERGE (cr)-[:SNAPSHOT_OF_ROUTE {route_version: $rv}]->(rv)
                """,
                nid=f"CheckpointRecord:{p['checkpoint_id']}",
                rid=p["route_id"], rv=p["route_version"],
            )
```

- `MERGE` 保证重复消费不产生重复节点/边（§5.3 双层幂等的第二层）。
- `SNAPSHOT_OF_ROUTE` 边携带 `route_version`，历史回放按当时版本（§4.4）。

### 9.2 图检索器（Cypher 5M1E 扩展）

```python
# app/infrastructure/neo4j/retriever.py
class GraphRetriever:
    """从种子节点扩展 5M1E 子图，带租户/版本/时间窗过滤。"""

    def __init__(self, graph: AsyncNeo4jDriver) -> None:
        self._graph = graph

    async def expand_5m1e(
        self, seed: Seed, as_of: datetime, tenant: TenantContext
    ) -> TraceSubgraph:
        if seed.kind == SeedKind.WIP_UNIT:
            return await self._expand_from_wip(seed, as_of, tenant)
        if seed.kind == SeedKind.INVENTORY_BATCH:
            return await self._expand_from_batch(seed, as_of, tenant)
        raise ValueError(f"不支持的种子类型: {seed.kind}")

    async def _expand_from_wip(
        self, seed: Seed, as_of: datetime, tenant: TenantContext
    ) -> TraceSubgraph:
        async with self._graph.session() as s:
            result = await s.run(
                self._wip_5m1e_cypher(),
                sn=seed.value,
                tenant_scopes=tenant.scopes(),
                as_of=as_of.isoformat(),
            )
            record = await result.single()
        return WipSubgraphMapper.to_subgraph(record, seed, as_of)

    def _wip_5m1e_cypher(self) -> str:
        # 即 §6.2 的 Cypher，按维度 collect(DISTINCT ...) 聚合
        return OPEN_CYPHER_WIP_5M1E  # 常量，见 §6.2
```

- 检索器只管取子图，不做 LLM 综合——SRP。综合在 `TraceRetrievalService._synthesize`。
- 租户过滤在 Cypher `WHERE` 前置，不达权限看不到节点（§1.2）。

---

### 9.3 种子解析器（NL → 种子）

```python
# app/application/seed_resolver.py
class SeedResolver:
    """自然语言 → 图种子。规则优先（SN/工单/批次正则），LLM 兜底，Embedding 做缺陷描述匹配。"""

    def __init__(self, llm: BaseChatModel, embedder: BgeClient, graph: AsyncNeo4jDriver) -> None:
        self._llm = llm
        self._embedder = embedder
        self._graph = graph

    async def resolve(self, question: str, tenant: TenantContext) -> Seed:
        # 1. 规则优先：SN / 工单号 / 批次号都有编码规则，正则命中即定种子
        if m := SN_PATTERN.search(question):
            return Seed(kind=SeedKind.WIP_UNIT, value=m.group(0))
        if m := WO_PATTERN.search(question):
            return Seed(kind=SeedKind.WORK_ORDER, value=m.group(0))
        if m := BATCH_PATTERN.search(question):
            return Seed(kind=SeedKind.INVENTORY_BATCH, value=m.group(0))
        # 2. 缺陷描述语义匹配：bge-m3 向量在 DefectCatalog 节点近邻检索
        if defect := await self._match_defect(question):
            return Seed(kind=SeedKind.DEFECT, value=defect.defect_code)
        # 3. LLM 兜底抽取（带结构化输出）
        return await self._llm.with_structured_output(Seed).ainvoke(
            f"从以下问题抽取追溯种子（sn / work_order_id / batch_no / defect_code）：\n{question}"
        )

    async def _match_defect(self, question: str) -> DefectCatalogView | None:
        vec = await self._embedder.embed(question)
        # Neo4j 5.x 原生向量索引，在 DefectCatalog.name 向量上近邻
        async with self._graph.session() as s:
            r = await s.run(
                "CALL db.index.vector.queryNodes('defect_name_idx', 1, $vec) "
                "YIELD node, score RETURN node.defect_code AS code, node.name AS name, score",
                vec=vec,
            )
            rec = await r.single()
        if rec and rec["score"] > 0.75:
            return DefectCatalogView(defect_code=rec["code"], name=rec["name"])
        return None
```

- 规则优先降低对模型的依赖，确定性高、成本低；只有规则命中不了才走 LLM。
- 缺陷描述走向量近邻（`DefectCatalog` 节点的 `name` 字段挂 bge-m3 向量），把"焊接不良"映射到 `defect_code=SW-001`。

### 9.4 启动断言（只读投影校验）

```python
# app/domain/projection.py
class ReadOnlyProjectionGate(Exception):
    """启动时发现非只读投影动作，拒绝启动。"""

# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.projection_registry
    # 启动断言：所有投影处理器只 MERGE 不 DELETE/SET-覆盖历史
    registry.assert_read_only()
    # 启动断言：消费者组只订语义主题，未误订 dc.* 原始流
    registry.assert_no_raw_data_topic()
    # 初始化 Neo4j / LLM / 消费者 ...
    async with app.state.kafka_consumer_groups as groups:
        for g in groups:
            asyncio.create_task(g.run())
        yield
```

- `assert_read_only` 扫描所有 `ProjectionHandler` 的 Cypher 模板，禁止出现 `DELETE` / `REMOVE` / 对历史节点的覆盖性 `SET`——只允许 `MERGE` 与新增——红线靠启动断言兜底，不靠口头约束（与 L1 Agent `ReadOnlyToolGate` 同思路）。
- `assert_no_raw_data_topic` 校验消费者组订阅列表里没有 `dc.equipment.data.raw` 这类原始报文主题，从启动期挡住"高频采集全量入图"。

### 9.5 FastAPI 入口

```python
# app/api/trace_router.py
router = APIRouter(prefix="/rag/trace", tags=["traceability-rag"])

@router.post("/query", response_model=TraceAnswer)
async def query(
    req: TraceQuery,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: TraceRetrievalService = Depends(get_trace_service),
) -> TraceAnswer:
    return await svc.query(req, tenant)

@router.post("/expand", response_model=TraceSubgraph)
async def expand(
    req: ExpandRequest,
    tenant: TenantContext = Depends(tenant_from_token),
    retriever: GraphRetriever = Depends(get_retriever),
) -> TraceSubgraph:
    """只取子图不综合，供 L1 Agent / 工程师 UI 直接消费。"""
    seed = Seed(req.kind, req.value)
    return await retriever.expand_5m1e(seed, req.as_of, tenant)
```

- 两个端点：`/query`（子图 + LLM 综合）给工程师问答；`/expand`（纯子图）给 L1 Agent 与 UI 直接消费结构化证据链。
- 租户上下文从 token 解析，注入检索链路全程。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `rag_projection_total` | 投影事件数（按 bounded_context / status label） |
| `rag_projection_lag_seconds` | 图投影滞后（事件 `occurred_at` 与投影完成时间差，Histogram） |
| `rag_projection_duplicate_total` | 幂等表挡住的重复事件数 |
| `rag_projection_error_total` | 投影失败次数（按 context label） |
| `rag_retrieval_total` | 检索次数（按 seed_kind label） |
| `rag_retrieval_latency_seconds` | 检索延迟（Cypher + LLM 综合，Histogram） |
| `rag_subgraph_cache_hit_total` | 子图缓存命中 |
| `rag_low_confidence_total` | 置信度 <0.6 转人工次数 |
| `rag_acl_fallback_total` | 降级查询各上下文 REST 次数（按 context） |
| `rag_projection_lag_high_total` | 投影滞后超阈值告警 |

### 10.2 trace 串联

- 每次检索一个 `trace_id`，OpenTelemetry 在 `GraphRetriever`、ACL 客户端、LLM 调用都注入 span，透传到下游 Java 服务（`traceparent` header）。
- `TraceAnswer.subgraph_ref` + 节点 `source_event_id` 让工程师从答案回溯到子图、再到原始领域事件 / 过点记录——证据链可点开回溯。

### 10.3 兜底

- **投影滞后兜底**：`projection_lag_ms` 超阈值（如 >30s）→ 检索置信度降权 + `needs_human_review`，并触发 ACL 降级查询补齐缺失节点。
- **置信度兜底**：`confidence < 0.6` → 不展示给操作工，只推工程师；与 MES 防错理念一致，宁可拦下让人判。
- **LLM 输出兜底**：`TraceAnswer` 经 Pydantic 校验，不符合 schema 判失败重试；重试仍失败转人工，不硬答。
- **图库故障兜底**：Neo4j 不可用时，`/rag/trace/query` 返回 503 + 降级提示，不阻塞 MES 生产；图可从 Kafka 事件回放重建（§4.5）。

---

## 11. 实现步骤

### 阶段一：骨架与最小图投影（2 周）

1. 搭 `rag_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 接 Neo4j 5.x async driver，建节点/边索引与 `defect_name_idx` 向量索引。
3. 实现 `GraphProjector` + 幂等表 + 位点表（§5.2/§5.3），接一个消费者组（`mes.*`）跑通。
4. 实现 `CheckpointProjectionHandler`（§9.1），验证 `CheckpointReleased` → 节点 + `SNAPSHOT_OF_ROUTE` 边入图。
5. 实现 `ReadOnlyProjectionGate` / `assert_no_raw_data_topic` 启动断言（§9.4）。

### 阶段二：全上下文投影（3 周）

6. 逐上下文实现 `ProjectionHandler`（§5.1 表），优先过点 / 工艺 / 物料 / 质量 / 台账 / 返修返工六个核心。
7. 按主题前缀起消费者组（§5.2），`rag-dc-semantic` 只订三个语义主题，验证不全量入图。
8. 验证幂等：重复投递事件不产生重复边（幂等表 + MERGE 双层）。
9. 验证版本：`ProcessRouteActivated` 投影新版本，旧版本 `DEPRECATED` 不删，历史 `SNAPSHOT_OF_ROUTE` 边不动。

### 阶段三：5M1E 检索与 LLM 综合（2 周）

10. 实现 `GraphRetriever.expand_5m1e`（§9.2，§6.2 Cypher），带租户/版本/时间窗过滤。
11. 实现 `SeedResolver`（§9.3）：规则优先 + bge-m3 缺陷向量匹配 + LLM 兜底。
12. 实现 `TraceRetrievalService` + LLM 综合（§7.2），`TraceAnswer` Pydantic 强约束 + 置信度阈值。
13. FastAPI 端点 `/rag/trace/query` / `/rag/trace/expand`（§9.5）。

### 阶段四：版本一致性与权限加固（2 周）

14. 工艺查询强制 `route_version` 入参，ACL 层校验 `ACTIVE` 状态（§7.3）。
15. 租户过滤在 Cypher 前置，权限不达标看不到节点（§1.2）。
16. 接 OpenTelemetry + prometheus 指标（§10.1），`projection_lag_ms` 降权兜底（§10.3）。
17. 子图缓存（redis）按种子 + as_of + 租户去重。

### 阶段五：加固、评测与 L1 对接（2 周）

18. 接 ACL 降级查询各上下文 REST（§7.3），图投影滞后时补齐。
19. 沉淀评测集（典型不良场景 + 预期 5M1E 假设），回归模型 / 提示词变更。
20. 对接 L1 Agent：`query_traceability_graph` 工具封装 `/rag/trace/query`（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §5.5）。
21. 灰度一条产线，收集工程师反馈，补全剩余上下文投影。

---

## 12. 约束落地检查清单

- [ ] 所有投影处理器只 `MERGE`，无 `DELETE`/`REMOVE`/历史覆盖性 `SET`，`ReadOnlyProjectionGate` 启动断言生效。
- [ ] 消费者组未订阅 `dc.equipment.data.raw` 等原始报文主题，`assert_no_raw_data_topic` 启动断言生效。
- [ ] `event_id + consumer_group` 幂等表 + Neo4j `MERGE` 双层去重，重复投递不产生重复边。
- [ ] 消费者位点落 MySQL，重启从断点续跑，投影事务成功后才 ack offset。
- [ ] `CheckpointRecord` 节点带 `route_version`，`SNAPSHOT_OF_ROUTE` 边指向当时版本；工艺变更只新增版本节点，历史边不动（INV-09）。
- [ ] 检索 `RouteVersion` 带 `route_version` / `status=ACTIVATED` 过滤；降级查询强制 `route_version` 入参（§7.3）。
- [ ] 租户 `tenant_scope` 在 Cypher `WHERE` 前置过滤，权限不达标看不到节点。
- [ ] RAG 服务不进过点主事务（§5.3），图投影秒级最终一致，过点 P99 ≤200ms 不受影响。
- [ ] 检索结果 `TraceSubgraph` 结构化，节点带 `source_event_id` 证据可回溯。
- [ ] LLM 输出经 Pydantic `TraceAnswer` 校验，5M1E 分类固化为 Enum，失败重试。
- [ ] `confidence < 0.6` 或投影滞后超阈值 → `needs_human_review`，不展示给操作工。
- [ ] 图库故障返回 503 不阻塞 MES 生产；图可从 Kafka 事件回放重建。
- [ ] 所有答案带 disclaimer：辅助假设，最终处置需工程师确认。

---

## 13. 面试防守 Q&A

**Q：追溯型 RAG 和通用 RAG 有什么本质区别？**
A：通用 RAG 是向量检索文档；追溯型 RAG 是 **GraphRAG + 领域事件流**，把物料批次 / 单件 / 设备 / 人员 / 工艺版本 / 过点记录之间的引用显式建成图边。输入"某单件焊接不良"，它能按 5M1E 自动串起这条单件的全链路。这套图谱建立在我已有的 14 个限界上下文和 `routeVersion`、`source_work_order_id`、`asset_id`、`batch_no` 这些跨上下文引用上——别人没有这套领域模型，抄不走（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.1）。

**Q：图怎么保证和 MES 的真实状态一致？**
A：图是领域事件的**只读投影**，不是事实源。事实源是各上下文的聚合根。图通过订阅 Kafka 事件增量更新，与过点执行上下文的 `ProcessRouteCache` 同构——都是事件投影的读模型。一致性靠三道闸：投影闸（`ProcessRouteActivated` 入新版本、旧版本不删）、检索闸（按 `CheckpointRecord.route_version` 取当时版本快照，不取当前生效版）、输出闸（证据必须含 `route_version`）。版本一致性不是 RAG 自己保证的，是从领域模型兜上来的。

**Q：会不会拖慢过点？**
A：不会进过点主事务。过点 P99 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1）是硬约束，图投影是异步消费事件，与过点判定完全解耦（§5.3 过点主事务零分布式事务）。图允许秒级最终一致——追溯型 RAG 是事后诊断工具，不是实时过点判定，秒级滞后可接受，滞后超阈值降置信度兜底。

**Q：图会不会越来越大，性能怎么办？**
A：一是高频采集不全量入图——设备原始 `DataPacket`（设计容量 ~1 万报文/秒）不进图，只把 `SerialNumberMarked` / `EquipmentRunHourAggregated` 这类语义事件投影进来，从订阅拓扑层面挡住（§5.2 `rag-dc-semantic` 只订三个语义主题）。二是历史节点不可变但可冷热分层——完工工单的子图可归档到冷存储，热图只留近期在制。三是检索是确定性的 Cypher 一跳/两跳扩展，不是全图遍历，配合索引与 `tenant_scope` 前置过滤，性能可控。

**Q：为什么不让 LLM 直接查 MES 数据库，要费劲建图？**
A：两个原因。一是 LLM 直接查原始表会绕过领域边界，权限和版本都兜不住——错给一条已失效工艺就批量不良。图把 14 个上下文的引用显式建成边，版本做成节点 + 快照边，检索带 `route_version` 过滤，从结构上杜绝失效工艺。二是图是事件投影的读模型，一次 Cypher 扩展就能取齐 5M1E，比 LLM 现场跨 5 个界面串快得多、准得多。LLM 只负责综合，不负责找路径。

**Q：追溯型 RAG 和 L1 诊断型 Agent 什么关系？是不是重复了？**
A：不重复，是分层。追溯型 RAG 是"图 + 一次检索综合"，给"这条单件 5M1E 全貌"；L1 Agent 是"多步 ReAct 工具调用"，给"根因要递进追问"的深度诊断（[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §2.2）。L1 的 `query_traceability_graph` 工具封装本文的检索 API，把图作为快路径，复杂场景再自己多步深挖。先有图、后有 Agent——图没建起来 L1 退化为纯工具循环，体验差。

**Q：高频采集为什么不进图？**
A：设备原始报文是高频持续流（设计容量 ~1 万报文/秒），进图会撑爆图库且与过点解耦（[领域总览.md](../../领域模型/领域总览.md) §5.3 设备级高频数据采集属于设备管理服务、与生产过点解耦）。所以只把已结构化、已瘦身的语义事件（`SerialNumberMarked` / `EquipmentRunHourAggregated` / `EquipmentAlarmRaised`）投影成 `EquipmentChannel` 节点与少量告警边，原始曲线 / 固件文件走对象存储，图里只挂 URI。这和采集链路"大文件不进主流"的决策一脉相承（[项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §2.5）。

**Q：不同车间能看的数据不一样怎么管？**
A：图节点带 `tenant_scope`（workshop/line），Cypher 查询 `WHERE w.tenant_scope IN $tenant_scopes` 前置过滤，权限不达标看不到节点，不是答完再裁剪（§1.2）。本 MES 的 14 个限界上下文边界本身就是天然的权限切分面，图按上下文分区，权限跟着上下文走。

**Q：图错了或漏了怎么办？**
A：图是只读投影，错了不影响 MES 生产——事实源在聚合根，图崩溃返回 503 不阻塞过点。漏了靠 ACL 降级查询补齐（§7.3，与过点"缓存未命中降级远程查询"同构）。重建靠事件回放——节点/边全不可变且由事件驱动，从 Kafka 位点重放即可完整重建图库，无需 MES 侧配合。所有答案带置信度，低置信度转人工，与 MES 防错理念一致。

**Q：上线了吗？**
A：这是设计阶段的引入规划，不是已落地。重点是图谱建模对齐 14 个限界上下文、图作为领域事件的只读投影、版本快照不可变这三条架构判断。落地需要先做文档型 RAG（路线 B）验证车间可用性，再建图——按"先 B 后 A"的顺序推进（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。诚实 + 体现架构判断力，比硬吹"已上线 GraphRAG"得分高。

---

## 14. 一句话定位

"追溯型 RAG 把 MES 已有的全链路追溯做成属性图——节点对齐 14 个限界上下文的聚合根、边把 `source_work_order_id`/`routeVersion`/`asset_id`/`batch_no` 这些跨上下文引用显式建出来，图本身是领域事件的只读投影、靠 `event_id` 幂等消费、靠 `SNAPSHOT_OF_ROUTE` 快照边锁死版本不可变。检索是一次 Cypher 5M1E 扩展 + LLM 综合，带租户前置过滤与 `route_version` 强制过滤，全程不进过点主事务、不回写 MES，低置信度转人工——这是建立在追溯护城河上的差异化能力，别人没有这套领域模型抄不走。"

