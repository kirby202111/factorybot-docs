# 追溯型 RAG 实现方案（Python 技术栈：核心 4 上下文 MVP）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.1 路线 A（追溯型 RAG）的**实现层落地**，与 [追溯型 RAG-详细设计.md](./追溯型 RAG-详细设计.md) 的关系：
> - **详细设计**是全 14 上下文的**设计层**（广）——图谱建模、投影规则、检索流程的全景；
> - **本文**是核心 4 上下文（在制品执行 + 工艺管理 + 物料 + 质量）的**实现层**（深）——把详细设计的骨架补全到可落地的 MVP，新增**依赖清单、Kafka topic 订阅清单、只读 REST 契约、Neo4j DDL、Docker 部署、测试策略**等实现层内容，并对个别事件契约按各上下文事件风暴落地口径细化（如物料消耗明细，§5.1 🔴）。
> 其余 10 上下文按 §11 相同范式扩展，MVP 不展开。
>
> **技术栈**：Python（FastAPI + Neo4j + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 Kafka 只读事件 + REST 只读查询解耦，互不侵入。
> **口径纪律**：追溯型 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律——说"规划方向 / 设计取舍"，不说"我们已经做了 GraphRAG"。MES 领域对错误答案零容忍（错给一条已失效工艺会直接导致批量不良），所以本文强调**图是领域事件的只读投影 + 版本快照不可变 + 可观测兜底**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 目标（4 上下文 MVP）

把 MES 已有的**全链路追溯**（[领域总览.md](../../领域模型/领域总览.md) §5 一致性与追溯原则）做成可被 LLM 检索 + 推理的**属性图**，让"某单件出现焊接不良，根因是什么"这类问题不再依赖工程师跨界面手动串，而是一次图谱检索 + LLM 综合即按 **5M1E** 给出根因假设 + 证据链。

**MVP 范围**：聚焦核心 4 上下文，跑通一条 SN 的 5M1E 闭环——

| 5M1E 维度 | MVP 覆盖 | 数据来源上下文 |
|-----------|---------|---------------|
| **Man（人）** | ✅ | 在制品执行（`CheckpointRecord.scanned_by`） |
| **Material（料）** | ✅ | 物料（`InventoryBatch` / `Supplier` / `SubstituteRule` / `Bom`） |
| **Method（法）** | ✅ | 工艺管理（`RouteVersion` 快照 / `RouteStep` / `QualityGateRule`） |
| **Measurement（测）** | ✅ | 在制品执行（`TestResult`） + 质量（`QualityVerdict` / `DefectCatalog`） |
| **Machine（机）** | ⏳ 后续 | 设备工装台账 / 维修 / 点检保养 / 计量检定（§11 扩展） |
| **Environment（环）** | ⏳ 后续 | 设备数据接入语义事件（§11 扩展） |

> 追溯骨架的必经节点 `WipUnit`（在制品执行）与 `WorkOrder`（工单管理）由在制品执行投影**附带建立**（薄投影，§4.1），不作为 MVP 重点展开——它们是 5M1E 串联的种子与归属节点，但不独立做厚投影处理器。

典型场景："单件 SN-001 焊接不良" -> 系统自动按 5M1E 串起：

1. **Man**：该单件每站过点记录的 `scanned_by`（在制品执行上下文 `CheckpointRecord`）
2. **Material**：该单件过点时消耗的锡膏批次 / 元件批次（物料上下文 `InventoryBatch`）、供应商、替代料规则、工单绑定的 `Bom` 版本
3. **Method**：该单件锁定的 `route_version` 快照（§5.1 版本一致性）、焊接站 `RouteStep`、质量门禁规则版本
4. **Measurement**：该单件 `TestResult`（AOI/SPI/FCT 原始判定）、`QualityVerdict` 业务判定、缺陷记录

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地（4 上下文具体动作） |
|------|------|----------------------|
| **只读投影** | 图是领域事件的**只读投影**，不是事实源；事实源是各上下文的聚合根 | 仅订阅 Kafka 只读事件 + REST 只读降级查询；图库归 RAG 服务自有，从不回写 MES；`ReadOnlyProjectionGate` 启动断言禁止 `DELETE`/`REMOVE`/历史覆盖性 `SET`（§9.7） |
| **不进过点主事务** | 索引构建异步消费事件，与过点判定完全解耦 | 过点 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1，设备实时数据 REST 查询）不受图索引影响；图允许秒级最终一致 |
| **版本快照不可变** | 历史过点记录锁定的 `routeVersion` 不随工艺变更改变 | `CheckpointRecord` 节点带 `route_version` 属性 + `[:SNAPSHOT_OF_ROUTE]` 边指向当时版本；工艺变更只新增版本节点、旧版本 `DEPRECATED` 不删，历史边不动（INV-CX-02） |
| **权限隔离** | 检索前按车间 / 产线 / 角色过滤，不是答完再裁剪 | 图节点带 `tenant_scope`（workshop/line），Cypher `WHERE` 前置过滤 |
| **可观测兜底** | 每个答案带证据链（节点/事件引用）+ 置信度，低置信度转人工 | 检索结果结构化落库 + 置信度阈值；与 MES 防错理念一致：宁可拦下让人判 |
| **高频采集不全量入图** | 设备原始报文不进图 | MVP 不订 `dc.*` 原始流；`assert_no_raw_data_topic` 启动断言兜底（§9.7） |

### 1.3 与详细设计、L1 Agent 的关系

- **与详细设计**：详细设计给 14 上下文全景建模与投影规则；本文把其中 4 个核心上下文的投影处理器、检索 Cypher、ACL 契约补全到可落地代码，并新增实现层内容（依赖、DDL、Docker、测试）。两者互补，不互相替代——详细设计是"地图"，本文是"核心城区施工图"。
- **与 L1 诊断型 Agent**（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)）：L1 是追溯型 RAG 的**多步推理升级**。L1 的 `query_traceability_graph` 工具封装本文 `POST /rag/trace/query`（§9.8）；图是 L1 的快路径，图没建起来时 L1 退化为纯工具循环。**先有图、后有 Agent**。

### 1.4 与 Java 技术栈的关系

- RAG 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈——只订阅 Kafka 只读事件、调只读 REST。
- 跨语言的物理边界反而是好事：RAG 服务无法共享 Java 事务 / 内存，天然强制只读、不进过点主事务、不旁路应用服务写路径。
- 复用 [实现说明](../../实现说明/) 既有的 Kafka topic、领域事件 envelope（`event_id` / `event_type` / `event_version` / `occurred_at` / `source_service` / `trace_id` / `partition_key`，见 [消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §4.3）、消费侧幂等模式（§6 同事务幂等 + 手动 ack），不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 版本 | 选型理由 |
|------|------|------|---------|
| 语言 | Python | 3.11+ | 类型提示 + Pydantic 校验，AI 生态最成熟，与 L1 Agent 同栈复用 LLM 抽象 |
| Web 框架 | **FastAPI** | 0.110+ | 异步、原生 OpenAPI，与 L1 Agent 一致，适合做检索 HTTP 入口 |
| 图存储 | **Neo4j** | 5.x | 属性图 + Cypher + GDS 图算法 + **原生向量索引**（语义节点直接挂向量，免二套库） |
| 图驱动 | **neo4j-python-driver** | 5.x（async） | 官方异步驱动，`AsyncGraphDatabase` |
| 检索编排 | **LlamaIndex** `PropertyGraphIndex` | 0.10+ | 图检索 + LLM 综合的上层抽象（可选，MVP 可先用裸 Cypher） |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 0.2+ | 模型可插拔，配置切换 Claude / 通义千问 / DeepSeek / 本地化模型，与 L1 一致 |
| Embedding | **bge-m3**（多语种，可本地化） | 1.0+ | 缺陷描述 / SOP 片段 / 自然语言问题的语义向量化，覆盖中英文，1024 维 |
| 数据校验 | **Pydantic** | v2 | 检索请求 / 子图视图 / 报告 DTO 的 schema 即类型 |
| HTTP 客户端 | **httpx** | 0.27+（异步） | 降级查询各上下文只读 REST |
| 消息 | **aiokafka** | 0.10+ | 订阅领域事件，异步非阻塞 |
| 元数据持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 2.0+ | 索引位点、`event_id` 幂等表、检索审计 |
| 缓存 | **redis-py (async)** | 5.0+ | 子图结果短期缓存（同种子重复检索去重） |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | — | trace 串联、指标告警 |
| 配置 | pydantic-settings | 2.0+ | 环境变量 / 配置文件统一管理 |
| 部署 | 独立微服务 `rag-service`（uvicorn + gunicorn worker） | — | 与三大服务同网格，K8s 部署；MVP 可 docker-compose 本地起 |

### 2.2 为什么是 GraphRAG 而非纯向量检索

- 本 MES 的追溯链是**结构化关系**：`SN -> 过点记录 -> 设备 / 工艺版本 / 物料批次 / 测试结果`，这些是显式引用（`source_work_order_id`、`routeVersion`、`asset_id`、`batch_no`），不是文本相似度。向量检索只能找"语义相近的文档"，找不到"这条单件用了哪批锡膏、那批锡膏还进了哪些单件"。
- GraphRAG 把跨上下文引用**显式建成图边**，5M1E 串联回退为一跳/两跳的 Cypher 扩展，准确率远高于向量近似匹配。
- 向量在本文里只承担**语义入口**：把自然语言问题解析成图种子（SN / 批次 / 工单 / 设备），以及缺陷描述相似度——是图的补充，不是主体。这区别于 [RAG服务引入路线.md](../RAG服务引入路线.md) 路线 B（文档型 RAG，向量是主体）。

### 2.3 为什么图谱建立在领域事件流之上

- 图不是凭空建模的，是各上下文**领域事件的投影**——与在制品执行上下文的 `ProcessRouteCache` / `EquipmentAvailabilityCache` 同构（[在制品执行上下文.md](../../领域模型/生产执行服务/事件风暴/在制品执行上下文.md) §1.2），只是投影目标是属性图而非键值缓存。
- 三个红利：① **天然只读**——投影只消费事件，不回写；② **天然解耦**——不进过点主事务（[领域总览.md](../../领域模型/领域总览.md) §5.3），允许秒级最终一致；③ **版本一致性能兜住**——工艺变更事件 `ProcessRouteActivated` 触发新版本节点入图，历史边不动，与 §5.1 的"过点记录绑定 `routeVersion`"一脉相承。
- 投影可靠性复用既有消息基础设施：事件经各上下文 Transactional Outbox 至少一次投递（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §1），RAG 侧 `event_id` 幂等消费（§5.3）。

### 2.4 部署形态（车间网隔离）

- 车间网络常与办公网隔离（[RAG服务引入路线.md](../RAG服务引入路线.md) §4 硬约束）。**建议**：图库（Neo4j）与 Embedding（bge-m3）本地化部署，LLM 视车间安全策略二选一——云端 API（质量高、需出网）或本地化模型（离线、质量折衷）。`BaseChatModel` 抽象保证两者切换零代码改动。
- MVP 用 `docker-compose` 本地起 Neo4j + MySQL + Redis + rag-service（§9.9），验证闭环后再上 K8s。

### 2.5 依赖清单（pyproject.toml 片段）

```toml
[project]
name = "rag-service"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "gunicorn>=21.2",
  "pydantic>=2.6",
  "pydantic-settings>=2.2",
  "neo4j>=5.20",
  "llama-index>=0.10",
  "langchain-core>=0.2",
  "httpx>=0.27",
  "aiokafka>=0.10",
  "sqlalchemy[asyncio]>=2.0",
  "asyncmy>=0.2.9",
  "redis>=5.0",
  "opentelemetry-api>=1.24",
  "opentelemetry-instrumentation-fastapi>=0.45b",
  "prometheus-client>=0.20",
  # bge-m3 本地化推理（可选，也可走独立 embedding 服务）
  "sentence-transformers>=3.0",
  "FlagEmbedding>=1.2",
]
```

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ rag-service（独立微服务，Python + FastAPI + Neo4j）                │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ TraceRetrievalService                      │  │
│  │ /rag/trace/* │  │  种子解析 -> 5M1E 子图扩展 -> LLM 综合       │  │
│  └──────────────┘  └─────────────────┬────────────────────────┘  │
│                                      │                            │
│              ┌───────────────────────┼───────────────────────┐    │
│              ▼                       ▼                       ▼    │
│  ┌───────────────────┐  ┌─────────────────────┐  ┌──────────┐ │
│  │ GraphProjector    │  │ GraphRetriever      │  │ LLM      │ │
│  │ 事件 -> 图增量写入  │  │ Cypher 5M1E 扩展    │  │ Synth    │ │
│  │ (4 上下文投影器)   │  └──────────┬──────────┘  └──────────┘ │
│  └────────┬──────────┘             │                            │
│           │                        ▼                            │
│  ┌────────▼────────┐        ┌──────────────────┐                │
│  │ Neo4j Graph     │        │ SeedResolver     │                │
│  │ (追溯图投影)     │◀──────▶│ (NL->种子, 向量)   │                │
│  └────────┬────────┘        └──────────────────┘                │
│           │ event_id 幂等 / 位点                              │
│  ┌────────▼────────┐  ┌─────────────────────┐  ┌───────────┐   │
│  │ Idempotency     │  │ consumer offset     │  │ ACL 降级   │   │
│  │ Table (MySQL)   │  │ (MySQL)             │  │ httpx->REST│   │
│  └─────────────────┘  └─────────────────────┘  └───────────┘   │
└───────────────────────────────────┼──────────────────────────────┘
                                    │ 订阅领域事件（只读）
                          ┌─────────▼──────────┐
                          │ aiokafka Consumer   │
                          │ 4 上下文 topic:      │
                          │ mes.* process.*     │
                          │ material.* quality.*│
                          └─────────────────────┘
                                    ▲
                                    │ 各上下文 Outbox 投递（至少一次）
              ┌─────────────────────┴─────────────────────┐
              │  生产执行服务(过点) / 制造资源服务(工艺/物料/质量) │
              │  （Java/Spring，事实源）                       │
              └─────────────────────────────────────────────┘
```

### 3.1 关键设计决策

- **图即投影（CQRS 读模型）**：`GraphProjector` 是各上下文事件的读模型投影器，与在制品执行上下文的本地缓存同构。事实源永远是 MES 服务的聚合根，图库崩溃不影响生产，重建即可（事件回放）。
- **事件驱动增量 + 位点管理**：消费者维护 `consumer offset` 落 MySQL，重启从断点续跑；`event_id` 幂等表保证重复投递不产生重复边（§5.3）。
- **检索与投影分离**：`GraphProjector`（写图）与 `GraphRetriever`（读图）解耦，投影滞后不阻塞检索——检索带 `as_of` 时间窗，滞后时段内低置信度兜底（§10.3）。
- **ACL 防腐层**：降级查询各上下文 REST 时经 ACL 适配，外部 DTO -> 内部视图（`TraceNode` / `ProcessVersionSnapshot`），外部 schema 变化不污染检索核心。符合 CLAUDE.md 的低耦合 / ACL 约束。
- **投影器按上下文隔离**：每个上下文一个 `ProjectionHandler`，互不干涉——与在制品执行上下文 §2.7"只消费不重发、按主题前缀隔离"完全同构（ISP/SRP）
- **多租户隔离权衡**：MVP 单库 + `tenant_scope` 前置过滤；多车间规模扩大后可演进为 Neo4j 多 database 分库（物理隔离），MVP 单库 + 预留按 tenant 路由的 DB 命名/Projector 扩展点，切换分库时机按上线后车间数与数据量观测定（详见 [详细设计](./追溯型 RAG-详细设计.md) §3.1）。。

---

## 4. 图谱建模：核心 4 上下文

图谱的节点 = 各上下文的**聚合根实例**，边 = 聚合之间的**跨上下文引用** + **时序流转**。本节聚焦 MVP 4 上下文 + 追溯骨架节点，全 14 上下文建模见 [详细设计](./追溯型 RAG-详细设计.md) §4。

每个节点带统一属性：`node_id`（上下文内唯一）、`bounded_context`、`tenant_scope`（workshop/line，权限过滤用，取自事件 envelope metadata（workshop/line 由发布事件的聚合根在 outbox 投递时填入，随事件持久化，图回放重建不丢；覆盖生产类与台账类所有节点，投影免反查工单））、`source_event_id`（创建该节点的事件，溯源用）、`occurred_at`、`version`（仅版本化聚合）。

### 4.1 节点类型（MVP）

| 限界上下文 | 节点标签 | 源聚合根 | 关键属性 | 版本化 |
|-----------|---------|---------|---------|--------|
| 在制品执行 | `CheckpointRecord` | CheckpointRecord | sn, work_order_id, station_id, equipment_id(Scanned), **route_version**(Released), decision(Released/Blocked), scanned_by(Scanned) | ✗（携带版本快照） |
| 在制品执行 | `TestResult` | TestResult | test_id, sn, station_id, test_type, raw_verdict, measured_items | ✗ |
| 在制品执行 | `RoutingProgress` | RoutingProgress | sn, current_step, **route_version**, status | ✗（可变状态快照） |
| 工艺管理 | `RouteVersion` | RouteVersion | route_id, **route_version**, route_type, status | ✓ |
| 工艺管理 | `RouteStep` | RouteStep（实体） | step_no, operation_id, station_type, is_reentry_point | 随路线版本 |
| 工艺管理 | `Operation` | Operation | operation_id | ✗ |
| 物料 | `Material` | Material | part_no | ✗ |
| 物料 | `Bom` | Bom | bom_id, **bom_version**, bom_type, status | ✓ |
| 物料 | `InventoryBatch` | Inventory | part_no, batch_no/lot_no, location, supplier_id, available_qty | ✗ |
| 物料 | `SubstituteRule` | SubstituteRule | rule_id, primary_part_no, substitute_part_nos | ✗ |
| 物料 | `Supplier` | Supplier | supplier_id | ✗ |
| 质量 | `QualityVerdict` | QualityVerdict | verdict_id, sn, station_id, business_verdict, defect_records, **rule_version** | ✗（带规则版本） |
| 质量 | `DefectCatalog` | DefectCatalog | defect_code, name, severity, **name_embedding**(向量) | ✗ |
| 质量 | `QualityGateRule` | QualityGateRule | rule_id, **rule_version**, status | ✓ |
| 追溯骨架 | `WipUnit` | WipUnit | sn, work_order_id, **route_version**, status, position | ✗ |
| 追溯骨架 | `WorkOrder` | WorkOrder | work_order_id, status, target_qty, bom_id, route_id | ✗（绑定快照） |

> **追溯骨架节点**：`WipUnit` / `WorkOrder` 不是 MVP 4 上下文之一，但它们是 5M1E 的种子与归属节点，由在制品执行投影**附带 MERGE** 建立（`CheckpointRecord.FOR_UNIT -> WipUnit`、`WipUnit.BELONGS_TO -> WorkOrder`），不单独建厚投影处理器。`BINDS_BOM` / `BINDS_ROUTE` 边由工单管理投影建立（§11 扩展），MVP 阶段若未投影则 §6.2 对应 `OPTIONAL MATCH` 返回空，BOM/工艺绑定维度靠 ACL 降级补齐，不影响 Man/Material(批次)/Method(快照)/Measurement 四维闭环。在制品执行 / 工单管理的完整投影在 §11 扩展。

### 4.2 边类型（MVP）

**A. 引用边（聚合间显式引用，建图主干）**

| 边类型 | 起点 -> 终点 | 源字段 | 带版本属性 |
|--------|------------|--------|-----------|
| `BELONGS_TO` | WipUnit -> WorkOrder | work_order_id | — |
| `BINDS_BOM` | WorkOrder -> Bom | bom_binding.bom_id | bom_version |
| `BINDS_ROUTE` | WorkOrder -> RouteVersion | route_binding.route_id | route_version |
| `FOR_UNIT` | CheckpointRecord -> WipUnit | sn + work_order_id | — |
| `SNAPSHOT_OF_ROUTE` | CheckpointRecord -> RouteVersion | route_version | route_version（INV-CX-02） |
| `PRODUCED_TESTRESULT` | CheckpointRecord -> TestResult | 同事务 | — |
| `JUDGED_BY` | TestResult -> QualityVerdict | source_test_result_id | — |
| `CITES_DEFECT` | QualityVerdict -> DefectCatalog | defect_records[].defect_code | — |
| `UNDER_RULE` | QualityVerdict -> QualityGateRule | rule_id | rule_version |
| `CONSUMED_BATCH` | WipUnit -> InventoryBatch | 过点消耗（🔴 契约待对齐，§5.1） | — |
| `SUPPLIED_BY` | InventoryBatch -> Supplier | supplier_id | — |
| `SUBSTITUTE_OF` | InventoryBatch -> SubstituteRule | 替代料命中 | — |

**B. 时序边（追溯链骨干）**

| 边类型 | 起点 -> 终点 | 说明 |
|--------|------------|------|
| `NEXT`（逻辑时序，**不物化为边**） | CheckpointRecord -> CheckpointRecord | 同一 SN 按 `occurred_at` 排序的过点序列；检索时动态 ORDER BY（partition_key=record_id 非 sn，同 SN 跨站事件不保序，物化会断裂） |

**C. 配置绑定边（工艺/质量规则，版本化）**

| 边类型 | 起点 -> 终点 | 源字段 | 带版本属性 |
|--------|------------|--------|-----------|
| `HAS_STEP` | RouteVersion -> RouteStep | 路线步骤序列 | route_version |
| `USES_OPERATION` | RouteStep -> Operation | operation_id | — |
| `ENFORCES_GATE` | RouteStep -> QualityGateRule | quality_gate_rule_id | rule_version |
| `HAS_BOM_ITEM` | Bom -> Material | BomItem.part_no | bom_version |

> **所有边不可变**：边一旦写入不修改、不删除（与 `CheckpointRecord` INV-12 不可变一致）。工艺版本变更只新增 `RouteVersion` 节点与新 `BINDS_ROUTE` 边，**不改动历史 `SNAPSHOT_OF_ROUTE` 边**——这是版本一致性能兜住的根因（§4.4）。

### 4.3 5M1E 维度建模（MVP 覆盖 Man/Material/Method/Measurement）

从种子节点（通常是 `WipUnit{sn}`）出发，5M1E 每个维度对应一组确定的一跳/两跳扩展，不需要 LLM 猜路径：

```mermaid
graph LR
    SEED(("种子<br/>WipUnit{sn}"))

    SEED -->|FOR_UNIT| CR[CheckpointRecord]
    SEED -->|BELONGS_TO| WO[WorkOrder]
    SEED -->|CONSUMED_BATCH| IB[InventoryBatch]

    CR -->|SNAPSHOT_OF_ROUTE / route_version| RV[RouteVersion]
    CR -->|PRODUCED_TESTRESULT| TR[TestResult]
    CR -.->|NEXT 不物化·查询排序| CR2[按 occurred_at]

    RV -->|HAS_STEP| RS[RouteStep]
    RS -->|ENFORCES_GATE| QGR[QualityGateRule]

    TR -->|JUDGED_BY| QV[QualityVerdict]
    QV -->|CITES_DEFECT| DC[DefectCatalog]
    QV -->|UNDER_RULE| QGR

    IB -->|SUPPLIED_BY| SUP[Supplier]
    IB -->|SUBSTITUTE_OF| SR[SubstituteRule]

    WO -->|BINDS_BOM| BOM[Bom]
    WO -->|BINDS_ROUTE| RV
    BOM -->|HAS_BOM_ITEM| MAT[Material]

    classDef man fill:#ffe0e6,stroke:#d63384;
    classDef material fill:#fff4e6,stroke:#f08c00;
    classDef method fill:#e6ffe6,stroke:#2f9e44;
    classDef measure fill:#f3e6ff,stroke:#7048e8;
    class CR,CR2,WO:::man;
    class IB,SUP,SR,BOM,MAT:::material;
    class RV,RS,QGR:::method;
    class TR,QV,DC:::measure;
```

| 5M1E | MVP 扩展路径（从种子 SN） | 命中上下文 | 状态 |
|------|---------------------|-----------|------|
| **Man** | `WipUnit -> CheckpointRecord.scanned_by`、过点序列（按 `occurred_at` 排序，不物化 NEXT） | 在制品执行 | ✅ |
| **Material** | `WipUnit -> CONSUMED_BATCH -> InventoryBatch -> SUPPLIED_BY/SUBSTITUTE_OF`；`WorkOrder -> BINDS_BOM -> Bom -> HAS_BOM_ITEM -> Material` | 物料 | ✅（CONSUMED_BATCH 🔴） |
| **Method** | `CheckpointRecord -> SNAPSHOT_OF_ROUTE{route_version} -> RouteVersion -> HAS_STEP -> RouteStep -> ENFORCES_GATE` | 工艺管理 / 质量 | ✅ |
| **Measurement** | `CheckpointRecord -> PRODUCED_TESTRESULT -> TestResult -> JUDGED_BY -> QualityVerdict -> CITES_DEFECT/UNDER_RULE` | 在制品执行 / 质量 | ✅ |
| **Machine** | （`CheckpointRecord -> USED_EQUIPMENT -> Asset` 等，依赖台账/维修上下文） | 设备相关 | ⏳ §11 |
| **Environment** | （`EquipmentChannel` 语义采样，依赖设备数据接入） | 设备数据接入 | ⏳ §11 |

### 4.4 版本快照节点（route_version / bom_version / rule_version）

本 MES 的工艺路线、BOM、质量门禁规则都有**版本生命周期**（[领域总览.md](../../领域模型/领域总览.md) §5.1；工艺管理 `RouteVersionState`：DRAFT/SUBMITTED/ACTIVATED/DEPRECATED/ARCHIVED；物料 `BomActivated` 生效后旧版本 Deprecate，BIZ-02 同 product 同 bom_type 唯一 ACTIVE；质量 `QualityGateRuleActivated` 同理）。图谱对版本的处理是**版本即节点**，不覆盖：

- `RouteVersion{route_id, route_version}` 是独立节点，`status` 属性标记 `ACTIVATED`/`DEPRECATED`。新版本生效（`ProcessRouteActivated`）-> 新增节点，旧节点 `status` 改 `DEPRECATED` 但**不删除**（工艺管理 BIZ-02）。
- `CheckpointRecord` 通过 `[:SNAPSHOT_OF_ROUTE {route_version}]` 边指向**当时生产用的版本**（INV-CX-02）。工艺变更后，旧过点记录的边仍指向旧版本节点——历史追溯按当时版本回放，不受新版本影响。
- `WorkOrder` 的 `BINDS_ROUTE` / `BINDS_BOM` 边携带 `route_version` / `bom_version` 属性，锁定的版本在工单下达时固化。
- `QualityVerdict` 的 `UNDER_RULE` 边携带 `rule_version`，保证判定结果可回溯到当时生效的规则（INV-CX-01）。

> 这是和通用 RAG 最大的区别：通用 RAG 检索文档不分版本，可能答出已失效工艺；本文的图把版本做成显式节点 + 快照边，检索时带 `route_version` 过滤（§6.2），从结构上杜绝"错给一条已失效工艺导致批量不良"。

### 4.5 索引与约束（Neo4j DDL）

MVP 启动时由 `SchemaInitializer`（§9.7）执行以下 DDL，幂等（`IF NOT EXISTS`）：

```cypher
// 唯一约束：按 node_id 去重（MERGE 幂等的第二层保障）
CREATE CONSTRAINT checkpoint_node_id IF NOT EXISTS
  FOR (n:CheckpointRecord) REQUIRE n.node_id IS UNIQUE;
CREATE CONSTRAINT wipunit_sn IF NOT EXISTS
  FOR (n:WipUnit) REQUIRE n.sn IS UNIQUE;
CREATE CONSTRAINT routeversion_unique IF NOT EXISTS
  FOR (n:RouteVersion) REQUIRE (n.route_id, n.route_version) IS UNIQUE;
CREATE CONSTRAINT bom_unique IF NOT EXISTS
  FOR (n:Bom) REQUIRE (n.bom_id, n.bom_version) IS UNIQUE;
CREATE CONSTRAINT qualitygaterule_unique IF NOT EXISTS
  FOR (n:QualityGateRule) REQUIRE (n.rule_id, n.rule_version) IS UNIQUE;
CREATE CONSTRAINT inventorybatch_unique IF NOT EXISTS
  FOR (n:InventoryBatch) REQUIRE n.batch_no IS UNIQUE;
CREATE CONSTRAINT defectcatalog_code IF NOT EXISTS
  FOR (n:DefectCatalog) REQUIRE n.defect_code IS UNIQUE;

// 普通索引：检索常用过滤字段
CREATE INDEX checkpoint_sn IF NOT EXISTS FOR (n:CheckpointRecord) ON (n.sn);
CREATE INDEX checkpoint_wo IF NOT EXISTS FOR (n:CheckpointRecord) ON (n.work_order_id);
CREATE INDEX routeversion_status IF NOT EXISTS FOR (n:RouteVersion) ON (n.status);
CREATE INDEX inventorybatch_partno IF NOT EXISTS FOR (n:InventoryBatch) ON (n.part_no);

// 向量索引：缺陷描述语义近邻（SeedResolver 缺陷匹配用，bge-m3 1024 维 cosine）
CREATE VECTOR INDEX defect_name_idx IF NOT EXISTS
  FOR (d:DefectCatalog) ON (d.name_embedding)
  OPTIONS {
    indexConfig: {
      `vector.dimensions`: 1024,
      `vector.similarity_function`: 'cosine'
    }
  };
```

---

## 5. 索引构建：事件流驱动（4 上下文）

图的写入完全由领域事件驱动，`GraphProjector` 是各上下文事件的**读模型投影器**。本节定义"事件 -> 图增量"的投影规则，主题名严格对齐各上下文事件风暴的对外契约。

### 5.1 事件 -> 图增量更新（投影规则）

| 领域事件 | 主题 | 投影动作（节点 + 边） | 幂等键 |
|---------|------|----------------------|--------|
| `CheckpointScanned` / `CheckpointReleased` | `mes.checkpoint.lifecycle` | 合投同一 `CheckpointRecord`（MERGE by node_id）：Scanned 补 `equipment_id`/`scanned_by`、Released 补 `route_version`/`decision=PASS`；建 `FOR_UNIT` -> `WipUnit`、`BELONGS_TO` -> `WorkOrder`、`SNAPSHOT_OF_ROUTE{route_version}` -> `RouteVersion`；**不建 NEXT 边**（查询时排序，§4.2）。🔴 `SNAPSHOT_OF_ROUTE` 需 `route_id`，事件 payload 仅 `route_version_id` 无 `route_id`，待在制品执行上下文补 | event_id |
| `CheckpointBlocked` | `mes.checkpoint.lifecycle` | 合投同一 `CheckpointRecord`：补 `decision=BLOCK`/`blocking_reason`；建 `FOR_UNIT` -> `WipUnit`（拦截也入图，供追溯） | event_id |
| `TestResultStructured` | `mes.testresult.structured` | upsert `TestResult{raw_verdict}`；建 `PRODUCED_TESTRESULT` ← `CheckpointRecord`（按 sn + station + source_ts 关联） | event_id |
| `RoutingProgressed` | `mes.routing.progress` | upsert `RoutingProgress{current_step, route_version}`；建 `AT_STEP` -> `RouteStep` | event_id |
| `ProcessRouteActivated` | `process.route.lifecycle` | upsert `RouteVersion{route_version, status=ACTIVATED}`；旧版本节点 `status=DEPRECATED`（不删）；建 `HAS_STEP` -> `RouteStep`、`ENFORCES_GATE` -> `QualityGateRule` | event_id |
| `ProcessRouteDeprecated` | `process.route.lifecycle` | 旧 `RouteVersion` 节点 `status=DEPRECATED`（不删历史 `SNAPSHOT_OF_ROUTE` 边） | event_id |
| `BomActivated` | `material.bom.lifecycle` | upsert `Bom{bom_version, status=ACTIVATED}`；旧版本 `DEPRECATED`（不删）；建 `HAS_BOM_ITEM` -> `Material` | event_id |
| `InventoryChanged` | `material.inventory.changed` | upsert `InventoryBatch{available_qty}`；建 `SUPPLIED_BY` -> `Supplier` | event_id |
| `SubstituteRuleActivated` | `material.substitute.lifecycle` | upsert `SubstituteRule`；建 `SUBSTITUTE_OF` ← `InventoryBatch`（命中时） | event_id |
| `QualityVerdictIssued` | `quality.inspection.verdict` | upsert `QualityVerdict{rule_version}`；建 `JUDGED_BY` ← `TestResult`、`CITES_DEFECT` -> `DefectCatalog`、`UNDER_RULE{rule_version}` -> `QualityGateRule` | event_id |
| `QualityGateRuleActivated` | `quality.gate.lifecycle` | upsert `QualityGateRule{rule_version, status=ACTIVATED}`；旧版本 `DEPRECATED`（不删） | event_id |
| `BatchQualityAnomalyDetected` | `quality.anomaly.batch` | upsert `BatchQualityAnomaly`；建 `AFFECTS` -> `WipUnit`（按 affected_sn_list） | event_id |
| `DefectCatalogDefined` | `quality.defect.catalog` | upsert `DefectCatalog{name, name_embedding}`（embedding 由 bge-m3 生成） | event_id |

> 🔴 **契约待对齐：`CONSUMED_BATCH` 边的来源**。5M1E Material 维度需要"某 SN 过点时消耗了哪批料"。但物料上下文消费 `mes.checkpoint.lifecycle`(CheckpointReleased) 做的是 `ConsumeInventory`（按 BOM `consumption_rule` 扣减，BIZ-04 `(sn, work_order_id, source_event_id)` 去重），对外发布 `material.inventory.changed`(InventoryChanged，不含 sn↔batch 明细) 与 `MaterialConsumed`(含 `sn` 但**无 `lot_no`**)。**sn↔batch 映射在事件契约缺失**（`Inventory` 聚合根有 `lot_no` 但未进 payload；§2.6 热点为防错读模型来源与返工回收，非此 gap）。建议推动物料上下文将 `lot_no` 加入 `MaterialConsumed` payload。
>
> MVP 处理：① 订阅 `material.inventory.changed` 维护 `InventoryBatch` 节点；② `CONSUMED_BATCH` 边**降级查询**物料上下文只读 REST `GET /api/material/consumption?sn=&work_order_id=` 补齐（§7.3）；③ 待物料上下文明确消耗明细事件后，改为事件投影。这条 gap 在 §7.3 ACL 与 §15 Q&A 都会讲到。

### 5.2 订阅拓扑与位点管理

按服务前缀分消费者组，避免单组拉全部主题导致积压。MVP 4 上下文涉及：

| 消费者组 | 订阅主题 | 归属上下文 |
|---------|---------|-----------|
| `rag-mes` | `mes.checkpoint.lifecycle`, `mes.testresult.structured`, `mes.routing.progress` | 在制品执行 |
| `rag-process` | `process.route.lifecycle` | 工艺管理 |
| `rag-material` | `material.bom.lifecycle`, `material.inventory.changed`, `material.substitute.lifecycle`, `material.master.lifecycle`, `material.supplier.lifecycle` | 物料 |
| `rag-quality` | `quality.inspection.verdict`, `quality.gate.lifecycle`, `quality.defect.catalog`, `quality.anomaly.batch` | 质量 |

- **位点落 MySQL**：每消费者组维护 `consumer_offset`（topic + partition + offset）落 `index_offset` 表，重启从断点续跑，不依赖 Kafka 自动提交——崩溃窗口可回退到上次成功位点重放（幂等兜住重复）。与 [消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §6 的"业务处理与幂等记录同事务、手动 ack"一致。
- **手动 ack**：投影事务（图 upsert + 幂等记录 + 位点更新）成功后才 ack offset。
- **`enable.auto.commit=false`**：严禁自动提交，避免"已 ack 未投影"丢事件（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §12.2）。
- **不订 `dc.*`**：MVP 消费者组不含任何 `dc.*` 主题，`assert_no_raw_data_topic` 启动断言兜底（§9.7）。

### 5.3 幂等与去重

事件经各上下文 Transactional Outbox **至少一次**投递（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §1），RAG 侧必须幂等消费，否则重复投递会产生重复边。

```sql
-- 幂等表：event_id + consumer_group 唯一键（对齐消息处理说明 §6 consumed_event 模式）
CREATE TABLE index_idempotency (
  event_id        VARCHAR(64)  NOT NULL,
  consumer_group  VARCHAR(64)  NOT NULL,
  topic           VARCHAR(128) NOT NULL,
  projected_at    DATETIME(3)  NOT NULL,
  PRIMARY KEY (event_id, consumer_group)
);

-- 位点表
CREATE TABLE index_offset (
  consumer_group  VARCHAR(64)  NOT NULL,
  topic           VARCHAR(128) NOT NULL,
  partition_no    INT          NOT NULL,
  offset_no       BIGINT       NOT NULL,
  updated_at      DATETIME(3)  NOT NULL,
  PRIMARY KEY (consumer_group, topic, partition_no)
);
```

- **投影事务**：`图 upsert` + `INSERT index_idempotency` 在 Neo4j 与 MySQL 间**非分布式**——Neo4j 写图后写幂等表，若幂等键冲突说明已投影，跳过图写入并 ack。崩溃重放时重复投递被幂等表挡住，图不产生重复边。
- **MERGE 幂等**：Neo4j 侧所有节点/边写入用 `MERGE`（按 `node_id` / 边端点 + `source_event_id` 去重），即使幂等表漏挡，图层面也不重复（双层幂等）。
- **位点上移**：幂等记录与位点更新同 MySQL 事务，保证"已投影 ⇒ 已 ack"。

### 5.4 版本失效与重索引

工艺版本变更是 MES 上 RAG 最危险的环节——检索到已失效工艺会直接导致批量不良。本文从三个层面兜住：

1. **图层面（节点不删）**：`ProcessRouteActivated` 投影时，新 `RouteVersion{route_version, status=ACTIVATED}` 入图，旧版本节点 `status` 改 `DEPRECATED` 但**不删除**——历史 `SNAPSHOT_OF_ROUTE` 边仍指向旧版本，历史追溯不受影响（INV-CX-02）。`BomActivated` / `QualityGateRuleActivated` 同理。
2. **检索层面（版本过滤）**：检索 `RouteVersion` 时带 `status=ACTIVATED` 过滤；查历史单件时按 `CheckpointRecord.route_version` 精确定位当时版本，不取"当前生效版"——§6.2 强制版本入参。
3. **文档层面（重索引）**：`ProcessRouteActivated` 同时触发文档型 RAG（路线 B）重索引关联的 SOP / 作业指导书，保证文档检索结果与生产执行侧工艺缓存版本一致（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.2）。本文通过发布内部 `rag.reindex.request` 事件通知路线 B。

> **版本一致性不是 RAG 自己保证的，是从领域模型兜上来的**——过点记录绑 `routeVersion`（INV-CX-02）、工艺/BOM/规则版本有生命周期、变更事件驱动重索引。RAG 只是严格遵循这套契约，不另搞一套版本管理。

---

## 6. 检索与推理：5M1E 自动串联

### 6.1 检索入口（种子解析）

检索从**种子节点**出发。种子可由用户直接给（`sn` / `batch_no` / `work_order_id`），也可由自然语言问题经 `SeedResolver` 解析得到：

```text
用户问题："SN-001 昨天焊接不良，根因？"
        │
        ▼
SeedResolver（NL -> 种子）
  ├─ 实体抽取：SN-001（正则命中 SN 规则）
  ├─ 意图识别：根因诊断（5M1E 扩展）
  └─ 时间窗：昨天 -> as_of = 今日 00:00
        │
        ▼
seed = {kind: "WipUnit", sn: "SN-001", as_of: <今日00:00>}
```

- **实体抽取**优先规则匹配（SN / 工单号 / 批次号都有编码规则，正则即可），命中不了再走 LLM 抽取——降低对模型的依赖，提升确定性。
- **语义兜底**：缺陷描述（"焊接不良"-> `defect_code=SW-001`）走 bge-m3 在 `DefectCatalog` 节点的 `name_embedding` 向量索引上近邻检索。
- **多种子**：问题可能涉及批次（"B-77 这批锡膏进了哪些单件"），种子是 `InventoryBatch`，扩展方向反转（`CONSUMED_BATCH` 反向）。

### 6.2 5M1E 子图扩展（Cypher，聚焦 MVP 4 上下文）

种子确定后，5M1E 扩展是确定性的 Cypher 一跳/两跳查询，不需要 LLM 猜路径。以下是从 `WipUnit{sn}` 出发的 MVP 扩展（Man/Material/Method/Measurement）：

```cypher
// 参数：$sn, $tenant_scopes, $as_of
MATCH (w:WipUnit {sn: $sn})
WHERE w.tenant_scope IN $tenant_scopes
  AND w.occurred_at <= $as_of
// Man：过点序列（5M1E 骨架）
OPTIONAL MATCH (cr:CheckpointRecord)-[:FOR_UNIT]->(w)
  WHERE cr.occurred_at <= $as_of
// Measurement：测试结果 + 质量判定（在制品执行 + 质量上下文）
OPTIONAL MATCH (cr)-[:PRODUCED_TESTRESULT]->(t:TestResult)
OPTIONAL MATCH (t)-[:JUDGED_BY]->(qv:QualityVerdict)
OPTIONAL MATCH (qv)-[:CITES_DEFECT]->(dc:DefectCatalog)
OPTIONAL MATCH (qv)-[ur:UNDER_RULE]->(qgr:QualityGateRule)
// Method：当时工艺版本快照 + 步骤 + 门禁（版本一致性核心）
OPTIONAL MATCH (cr)-[sr:SNAPSHOT_OF_ROUTE]->(rvSnap:RouteVersion)
OPTIONAL MATCH (rvSnap)-[:HAS_STEP]->(rs:RouteStep)
OPTIONAL MATCH (rs)-[eg:ENFORCES_GATE]->(qgrStep:QualityGateRule)
// Material：消耗批次 + 供应商 + 替代料；工单绑定 BOM
OPTIONAL MATCH (w)-[:CONSUMED_BATCH]->(ib:InventoryBatch)
OPTIONAL MATCH (ib)-[:SUPPLIED_BY]->(sup:Supplier)
OPTIONAL MATCH (ib)-[:SUBSTITUTE_OF]->(sub:SubstituteRule)
OPTIONAL MATCH (w)-[:BELONGS_TO]->(wo:WorkOrder)
OPTIONAL MATCH (wo)-[bb:BINDS_BOM]->(bom:Bom)
OPTIONAL MATCH (bom)-[:HAS_BOM_ITEM]->(mat:Material)
RETURN w,
       collect(DISTINCT cr { .node_id, .station_id, .scanned_by, .decision, .occurred_at, .route_version }) AS man_checkpoints,
       collect(DISTINCT t { .test_id, .test_type, .raw_verdict }) AS measurements,
       collect(DISTINCT qv { .verdict_id, .business_verdict }) + collect(DISTINCT dc { .defect_code, .severity }) AS measurement_verdicts,
       collect(DISTINCT rvSnap { .route_id, .route_version, .status }) AS method_route,
       collect(DISTINCT rs { .step_no, .operation_id }) AS method_steps,
       collect(DISTINCT ib { .batch_no, .location, .available_qty }) + collect(DISTINCT sup { .supplier_id }) AS materials,
       collect(DISTINCT bom { .bom_id, .bom_version, .status }) AS method_bom
```

> **⚠️ 性能：避免 OPTIONAL MATCH 笛卡尔积**。上述多组 `OPTIONAL MATCH` 挂同一 `cr` 上，数据量大时中间结果集膨胀。生产环境用 Neo4j 5.x `CALL {}` 子查询按维度隔离、各维度独立 `collect`（改写示例见 [详细设计](./追溯型 RAG-详细设计.md) §6.2）。

- **`route_version` 锁定**：`SNAPSHOT_OF_ROUTE` 边的 `route_version` 来自 `CheckpointRecord` 本身（INV-CX-02），不取"当前生效版"——保证历史单件按当时工艺回放。若用户问"当前工艺"则另走 `status=ACTIVATED` 过滤的查询。
- **`tenant_scope` 前置过滤**：`WHERE w.tenant_scope IN $tenant_scopes` 在扩展前裁剪，权限不达标看不到节点，不是答完再裁剪（§1.2）。
- **`as_of` 时间窗**：所有节点按 `occurred_at <= $as_of` 过滤，支持"截至昨天"复盘。
- **Machine/Environment 维度**：MVP 不含（`USED_EQUIPMENT` / `EquipmentChannel` 边随 §11 扩展加入，Cypher 加 `OPTIONAL MATCH (cr)-[:USED_EQUIPMENT]->(eq:Asset)` 即可，向后兼容）。

### 6.3 检索结果结构化（TraceSubgraph）

检索返回的子图不是自由文本，而是**结构化 DTO**（Pydantic 强约束），既给 LLM 做综合的上下文，也给工程师 UI 直接渲染证据链：

```python
class TraceNode(BaseModel):
    label: str                       # "CheckpointRecord" / "Bom" ...
    bounded_context: str             # "在制品执行上下文"
    node_id: str
    props: dict[str, Any]            # 节点属性
    source_event_id: str             # 创建该节点的事件（证据回溯）

class TraceEdge(BaseModel):
    rel: str                         # "SNAPSHOT_OF_ROUTE" / "CONSUMED_BATCH"
    from_id: str
    to_id: str
    version: str | None = None       # route_version / bom_version / rule_version

class FiveM1ECluster(BaseModel):
    man: list[TraceNode]             # 过点记录
    material: list[TraceNode]        # 批次、供应商、替代料、BOM
    method: list[TraceNode]          # 工艺版本快照、步骤、门禁
    measurement: list[TraceNode]     # TestResult、QualityVerdict、缺陷
    machine: list[TraceNode] = []    # MVP 空，§11 扩展
    environment: list[TraceNode] = []# MVP 空，§11 扩展

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
- **置信度阈值**：`confidence < 0.6` 或 `projection_lag_ms` 超阈值 -> `needs_human_review=True`，不展示给操作工，只推工程师。
- **系统提示词约束**：明确告诉模型"只能基于提供的 TraceSubgraph 推理，不得编造未在子图中出现的节点；查工艺必须带 `route_version`；输出严格遵循 TraceAnswer 结构"。

### 6.5 与文档型 RAG、L1 Agent 的协同

- **与路线 B（文档型 RAG）协同**：追溯型 RAG 给"结构化事实链"（哪批锡膏、哪台设备），文档型 RAG 给"处置知识"（SPI 报警怎么处置、IPC 标准）。`TraceAnswer` 的 `suggested_action` 可调路线 B 的 `search_docs(query, route_version_filter)` 补 SOP 片段——两者版本过滤都对齐 `ProcessRouteActivated`（§5.4）。
- **与 L1 Agent 协同**：L1 的 `query_traceability_graph` 工具封装本文 `POST /rag/trace/query`；L1 在图检索基础上做**多步递进追问**（图给全貌，Agent 深挖某一维），本文是其快路径。
- **不互相替代**：图检索是"一次性给齐 5M1E"，Agent 是"一步步问下去"。简单根因用图够了，复杂跨上下文递进用 Agent。

---

## 7. 实现方案

### 7.1 索引构建管线（GraphProjector）

`GraphProjector` 是事件 -> 图增量的投影器，按主题前缀分派到各上下文的 `ProjectionHandler`：

```python
class ProjectionHandler(Protocol):
    """一个上下文一个投影处理器，处理本上下文事件 -> 图增量。"""
    bounded_context: str
    async def handle(self, event: DomainEvent, tx: AsyncGraphTransaction) -> None: ...
```

- 处理器内部用 Cypher `MERGE` 写节点/边，按 `node_id` 与边端点 + `source_event_id` 去重——即使幂等表漏挡，图层面也不重复（§5.3 双层幂等）。
- 处理器按上下文隔离，新增上下文事件只需新增一个 `ProjectionHandler`，不改动检索侧（OCP）。完整 `GraphProjector.consume` 见 §9.1。

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
    ) -> None: ...

    async def query(self, request: TraceQuery, tenant: TenantContext) -> TraceAnswer:
        # 1. 种子解析（NL -> seed，或直接用 request.seed）
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

### 7.3 ACL 防腐层（4 上下文只读 REST 契约 + 降级）

图投影滞后或节点缺失时，`GraphRetriever` 经 ACL 降级查询对应上下文只读 REST 补齐（与在制品执行上下文"缓存未命中降级远程查询"同构，[领域总览.md](../../领域模型/领域总览.md) §5.1）。MVP 4 上下文只读 REST 契约：

| 上下文 | 只读 REST | 用途 | 版本校验 |
|--------|----------|------|---------|
| 工艺管理 | `GET /api/process-routes/{route_id}?version={route_version}` | 降级取工艺版本详情 | 强制 `version` 入参，校验 `status` |
| 物料 | `GET /api/material/bom/{bom_id}?version={bom_version}` | 降级取 BOM 详情 | 强制 `version` 入参 |
| 物料 | `GET /api/material/consumption?sn=&work_order_id=` | 补齐 `CONSUMED_BATCH` 边（🔴 §5.1） | — |
| 物料 | `GET /api/material/batches/{batch_no}` | 降级取批次/供应商 | — |
| 质量 | `GET /api/quality/verdicts?sn=` | 降级取质量判定 | — |
| 在制品执行 | `GET /api/checkpoints?sn=` | 降级取过点记录 | — |

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


class MaterialAclClient:
    """物料上下文只读 ACL。含 CONSUMED_BATCH 降级补齐（🔴 §5.1）。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_consumption(
        self, sn: str, work_order_id: str, tenant: TenantContext
    ) -> list[ConsumptionView]:
        """降级查 sn↔batch 消耗明细，补齐 CONSUMED_BATCH 边。"""
        resp = await self._http.get(
            "/api/material/consumption",
            params={"sn": sn, "work_order_id": work_order_id},
            headers=tenant.headers(),
            timeout=2.0,
        )
        resp.raise_for_status()
        return [ConsumptionMapper.to_view(d) for d in resp.json()]
```

- 外部 DTO 不进检索核心，只暴露 `RouteVersionView` / `ConsumptionView`——防腐层核心职责（CLAUDE.md ACL 约束）。
- 降级查询是兜底，不进过点主事务（§5.3），超时降级为低置信度而非阻塞。
- **降级查询带 `as_of` 时间窗**：consumption/batches/verdicts 等降级查回"当时状态"而非"当前状态"，否则破坏版本快照（§4.4）。`as_of` 历史查询不走各上下文 REST（REST 仅补当前态），改由 RAG 侧消费事件时落本地快照表--`MaterialConsumed`（消耗明细，🔴 仍缺 `lot_no` 仅 sn 级）、`QualityVerdictIssued`（判定）、`InventoryChanged`（批次/供应商）；检索带 `as_of` 查本地表取当时快照，免改各上下文只读 REST。表结构 DDL 后续补。

### 7.4 版本一致性保证

版本一致性靠三道闸，不靠口头约束：

1. **投影闸**：`ProcessRouteActivated` / `BomActivated` / `QualityGateRuleActivated` 投影新版本 `{status=ACTIVATED}`，旧版本 `DEPRECATED` 不删（§5.4）。
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
      seed_resolver.py         # NL -> 种子（规则优先 + LLM 兜底）
    domain/                    # RAG 领域模型
      subgraph.py              # TraceSubgraph / TraceNode / TraceEdge / FiveM1ECluster
      answer.py                # TraceAnswer / RootCauseHypothesis / FiveM1ECategory
      seed.py                  # Seed / SeedKind
      tenant.py                # TenantContext
      projection.py            # ProjectionHandler 协议 / GraphProjector / ReadOnlyProjectionGate
    infrastructure/
      neo4j/                   # 图库
        driver.py              # AsyncNeo4jDriver 封装
        schema.py              # SchemaInitializer（DDL，§4.5）
        retriever.py           # GraphRetriever（Cypher 5M1E 扩展）
        projections/           # 各上下文投影处理器（MVP 4 个）
          checkpoint.py        # 在制品执行
          process_route.py     # 工艺管理
          material.py          # 物料
          quality.py           # 质量
      rag/                     # LlamaIndex PropertyGraphIndex 封装（可选上层）
        graph_index.py
      embedding/               # bge-m3 客户端（SeedResolver 语义兜底 + DefectCatalog 向量）
        bge_client.py
      ai/                      # LLM 客户端
        llm_factory.py
      acl/                     # 降级查询各上下文只读 REST
        process_management.py
        material.py
        quality.py
        checkpoint.py
      kafka/                   # aiokafka 消费者（按主题前缀分组）
        consumer_group.py
        listeners.py
      persistence/             # SQLAlchemy 模型 + Repository
        models.py              # index_idempotency / index_offset / subgraph_audit
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
  Dockerfile
  docker-compose.yml
```

- `domain/projection.ProjectionHandler` 是协议（ISP），每个上下文实现自己的处理器，互不强迫实现无关方法。
- `infrastructure/neo4j/projections/` 是投影器落地，MVP 4 个文件，符合 SRP——新增上下文事件只加文件不改既有（§11）。
- `infrastructure/acl/` 是防腐层，降级查询外部 REST，外部 DTO 经 Mapper 转内部视图，不污染检索核心。

---

## 9. 关键代码骨架

### 9.1 投影管线（GraphProjector）

```python
# app/domain/projection.py
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
            self._metrics.projection_duplicate.inc(group)
            return
        # 2. 路由到对应上下文处理器（按主题前缀）
        prefix = msg.topic.split(".")[0]
        handler = self._handlers.get(prefix)
        if handler is None:
            self._metrics.unknown_topic.inc(msg.topic)
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        # 3. 图事务内投影（MERGE 保证幂等）
        async with self._graph.session() as session:
            await session.execute_write(lambda tx: handler.handle(event, tx))
        # 4. 幂等记录 + 位点推进（同 MySQL 事务）
        await self._idem.record(event.event_id, group, msg.topic)
        await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
        self._metrics.projected.inc(handler.bounded_context)
```

### 9.2 投影处理器（在制品执行上下文）

```python
# app/infrastructure/neo4j/projections/checkpoint.py
class CheckpointProjectionHandler:
    """在制品执行上下文事件 -> 图增量。处理 mes.checkpoint.lifecycle / mes.testresult.structured / mes.routing.progress。"""

    bounded_context = "在制品执行上下文"

    async def handle(self, event: DomainEvent, tx: AsyncGraphTransaction) -> None:
        if event.event_type == "CheckpointScanned":
            await self._on_scanned(event, tx)  # 补 equipment_id / scanned_by
        elif event.event_type == "CheckpointReleased":
            await self._on_checkpoint(event, tx, decision="PASS")
        elif event.event_type == "CheckpointBlocked":
            await self._on_checkpoint(event, tx, decision="BLOCK")
        elif event.event_type == "TestResultStructured":
            await self._on_test_result(event, tx)
        elif event.event_type == "RoutingProgressed":
            await self._on_routing(event, tx)

    async def _on_checkpoint(self, e: DomainEvent, tx: AsyncGraphTransaction, decision: str) -> None:
        p = e.payload
        node_id = f"CheckpointRecord:{p['checkpoint_id']}"
        # MERGE 节点（按 node_id 幂等）+ 追溯骨架节点 + 引用边
        await tx.run(
            """
            MERGE (cr:CheckpointRecord {node_id: $node_id})
              SET cr.sn = $sn, cr.work_order_id = $wo_id, cr.station_id = $station_id,
                  cr.route_version = $rv, cr.decision = $decision,  # 🔴 scanned_by/equipment_id 由 Scanned 补，Released/Blocked 不 SET（避免覆盖）
                  cr.occurred_at = $at, cr.tenant_scope = $tenant,
                  cr.source_event_id = $eid, cr.bounded_context = '在制品执行上下文'
            WITH cr
            MERGE (w:WipUnit {sn: $sn})
              SET w.work_order_id = $wo_id, w.route_version = $rv,
                  w.tenant_scope = $tenant, w.bounded_context = '在制品执行上下文'
            MERGE (cr)-[:FOR_UNIT]->(w)
            WITH cr
            MERGE (wo:WorkOrder {work_order_id: $wo_id})
              SET wo.tenant_scope = $tenant, wo.bounded_context = '工单管理上下文'
            MERGE (w)-[:BELONGS_TO]->(wo)
            // NEXT 不物化（§4.2）：partition_key=record_id 非 sn，同 SN 跨站事件不保序，
            // 物化会断裂；过点序列查询时按 occurred_at 动态排序。NEXT 边构建已移除。
            """,
            node_id=node_id, sn=p["sn"], wo_id=p["work_order_id"],
            station_id=p["station_id"], rv=p["route_version"], decision=decision,
            scanned_by=p.get("scanned_by"), at=p["occurred_at"],
            tenant=p["tenant_scope"], eid=e.event_id,
        )
        # 版本快照边：指向当时工艺版本（INV-CX-02，不取当前生效版）。🔴 route_id 待在制品执行上下文补入 CheckpointReleased payload（同 CONSUMED_BATCH 的 lot_no，详见详细设计 §5.1）；未补前 route_id 缺失则跳过此边，补后自动建。
        if p.get("route_version") and p.get("route_id"):
            await tx.run(
                """
                MATCH (cr:CheckpointRecord {node_id: $nid})
                MERGE (rv:RouteVersion {route_id: $rid, route_version: $rv})
                MERGE (cr)-[:SNAPSHOT_OF_ROUTE {route_version: $rv}]->(rv)
                """,
                nid=node_id, rid=p["route_id"], rv=p["route_version"],
            )

    async def _on_test_result(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        p = e.payload
        await tx.run(
            """
            MERGE (t:TestResult {node_id: $node_id})
              SET t.test_id = $test_id, t.sn = $sn, t.station_id = $station_id,
                  t.test_type = $test_type, t.raw_verdict = $raw_verdict,
                  t.measured_items = $items, t.occurred_at = $at,
                  t.source_event_id = $eid, t.bounded_context = '在制品执行上下文'
            WITH t
            MATCH (cr:CheckpointRecord {sn: $sn})
            WHERE cr.station_id = $station_id
            WITH t, cr ORDER BY cr.occurred_at DESC LIMIT 1
            MERGE (cr)-[:PRODUCED_TESTRESULT]->(t)
            """,
            node_id=f"TestResult:{p['test_id']}", test_id=p["test_id"],
            sn=p["sn"], station_id=p["station_id"], test_type=p["test_type"],
            raw_verdict=p["raw_verdict"], items=p.get("measured_items", []),
            at=p["source_ts"], eid=e.event_id,
        )
```

### 9.3 投影处理器（工艺管理上下文）

```python
# app/infrastructure/neo4j/projections/process_route.py
class ProcessRouteProjectionHandler:
    """工艺管理上下文事件 -> 图增量。处理 process.route.lifecycle。"""

    bounded_context = "工艺管理上下文"

    async def handle(self, event: DomainEvent, tx: AsyncGraphTransaction) -> None:
        if event.event_type == "ProcessRouteActivated":
            await self._on_activated(event, tx)
        elif event.event_type == "ProcessRouteDeprecated":
            await self._on_deprecated(event, tx)

    async def _on_activated(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        p = e.payload
        # 新版本入图 + 旧版本 DEPRECATED（不删，BIZ-02 同 route 唯一 ACTIVE）
        await tx.run(
            """
            // 旧版本置 DEPRECATED（历史 SNAPSHOT_OF_ROUTE 边不动）
            MATCH (old:RouteVersion {route_id: $rid})
            WHERE old.route_version <> $rv AND old.status = 'ACTIVATED'
            SET old.status = 'DEPRECATED'
            WITH old
            // 新版本入图
            MERGE (rv:RouteVersion {route_id: $rid, route_version: $rv})
              SET rv.route_type = $rtype, rv.status = 'ACTIVATED',
                  rv.activated_at = $at, rv.source_event_id = $eid,
                  rv.bounded_context = '工艺管理上下文'
            """,
            rid=p["route_id"], rv=p["route_version"], rtype=p.get("route_type"),
            at=p["effective_at"], eid=e.event_id,
        )
        # 步骤 + 门禁绑定边（载荷若含 steps 摘要）
        for step in p.get("steps", []):
            await tx.run(
                """
                MATCH (rv:RouteVersion {route_id: $rid, route_version: $rv})
                MERGE (rs:RouteStep {route_id: $rid, route_version: $rv, step_no: $step_no})
                  SET rs.operation_id = $op, rs.station_type = $st,
                      rs.is_reentry_point = $reentry
                MERGE (rv)-[:HAS_STEP {route_version: $rv}]->(rs)
                WITH rs
                MERGE (op:Operation {operation_id: $op})
                MERGE (rs)-[:USES_OPERATION]->(op)
                """,
                rid=p["route_id"], rv=p["route_version"], step_no=step["step_no"],
                op=step["operation_id"], st=step.get("station_type"),
                reentry=step.get("is_reentry_point", False),
            )
            if step.get("quality_gate_rule_id"):
                await tx.run(
                    """
                    MATCH (rs:RouteStep {route_id: $rid, route_version: $rv, step_no: $step_no})
                    MERGE (qgr:QualityGateRule {rule_id: $rule_id, rule_version: $rule_version})
                    MERGE (rs)-[:ENFORCES_GATE {rule_version: $rule_version}]->(qgr)
                    """,
                    rid=p["route_id"], rv=p["route_version"], step_no=step["step_no"],
                    rule_id=step["quality_gate_rule_id"],
                    rule_version=step.get("rule_version"),
                )

    async def _on_deprecated(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        p = e.payload
        await tx.run(
            "MATCH (rv:RouteVersion {route_id: $rid, route_version: $rv}) "
            "SET rv.status = 'DEPRECATED'",
            rid=p["route_id"], rv=p["route_version"],
        )
```

### 9.4 投影处理器（物料上下文）

```python
# app/infrastructure/neo4j/projections/material.py
class MaterialProjectionHandler:
    """物料上下文事件 -> 图增量。处理 material.bom.lifecycle / material.inventory.changed / material.substitute.lifecycle。"""

    bounded_context = "物料上下文"

    async def handle(self, event: DomainEvent, tx: AsyncGraphTransaction) -> None:
        if event.event_type == "BomActivated":
            await self._on_bom_activated(event, tx)
        elif event.event_type == "InventoryChanged":
            await self._on_inventory_changed(event, tx)
        elif event.event_type == "SubstituteRuleActivated":
            await self._on_substitute(event, tx)

    async def _on_bom_activated(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        p = e.payload
        # 旧版本 DEPRECATED（不删，BIZ-02 同 product 同 bom_type 唯一 ACTIVE）
        await tx.run(
            """
            MATCH (old:Bom {bom_id: $bom_id})
            WHERE old.bom_version <> $bv AND old.status = 'ACTIVATED'
            SET old.status = 'DEPRECATED'
            WITH old
            MERGE (bom:Bom {bom_id: $bom_id, bom_version: $bv})
              SET bom.bom_type = $btype, bom.status = 'ACTIVATED',
                  bom.activated_at = $at, bom.source_event_id = $eid,
                  bom.bounded_context = '物料上下文'
            """,
            bom_id=p["bom_id"], bv=p["bom_version"], btype=p.get("bom_type"),
            at=p["effective_at"], eid=e.event_id,
        )
        for item in p.get("items", []):
            await tx.run(
                """
                MATCH (bom:Bom {bom_id: $bom_id, bom_version: $bv})
                MERGE (mat:Material {part_no: $part_no})
                  SET mat.bounded_context = '物料上下文'
                MERGE (bom)-[:HAS_BOM_ITEM {bom_version: $bv}]->(mat)
                """,
                bom_id=p["bom_id"], bv=p["bom_version"], part_no=item["part_no"],
            )

    async def _on_inventory_changed(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        p = e.payload
        await tx.run(
            """
            MERGE (ib:InventoryBatch {batch_no: $batch_no})
              SET ib.part_no = $part_no, ib.location = $loc,
                  ib.available_qty = $qty, ib.occurred_at = $at,
                  ib.source_event_id = $eid, ib.bounded_context = '物料上下文'
            WITH ib
            FOREACH (_ IN CASE WHEN $supplier_id IS NOT NULL THEN [1] ELSE [] END |
              MERGE (sup:Supplier {supplier_id: $supplier_id})
              MERGE (ib)-[:SUPPLIED_BY]->(sup))
            """,
            batch_no=p["batch_no"], part_no=p["part_no"], loc=p.get("location"),
            qty=p.get("available_qty"), at=p["occurred_at"], eid=e.event_id,
            supplier_id=p.get("supplier_id"),
        )
```

> 🔴 `CONSUMED_BATCH` 边：物料上下文消费 `mes.checkpoint.lifecycle`(CheckpointReleased) 做 `ConsumeInventory`，对外只发 `material.inventory.changed`（不含 sn↔batch 明细）。MVP 不在此投影 `CONSUMED_BATCH`，改由检索时降级查询 `MaterialAclClient.fetch_consumption` 补齐（§7.3）。待物料上下文明确消耗明细事件后，在此处理器补 `MERGE (w:WipUnit {sn:$sn})-[:CONSUMED_BATCH]->(ib)`。

### 9.5 投影处理器（质量上下文）

```python
# app/infrastructure/neo4j/projections/quality.py
class QualityProjectionHandler:
    """质量上下文事件 -> 图增量。处理 quality.inspection.verdict / quality.gate.lifecycle / quality.defect.catalog。"""

    bounded_context = "质量上下文"

    async def handle(self, event: DomainEvent, tx: AsyncGraphTransaction) -> None:
        if event.event_type == "QualityVerdictIssued":
            await self._on_verdict(event, tx)
        elif event.event_type == "QualityGateRuleActivated":
            await self._on_gate_activated(event, tx)
        elif event.event_type == "DefectCatalogDefined":
            await self._on_defect_catalog(event, tx)

    async def _on_verdict(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        p = e.payload
        await tx.run(
            """
            MERGE (qv:QualityVerdict {node_id: $node_id})
              SET qv.verdict_id = $vid, qv.sn = $sn, qv.station_id = $station_id,
                  qv.business_verdict = $bv, qv.defect_records = $defects,
                  qv.rule_version = $rule_version, qv.occurred_at = $at,
                  qv.source_event_id = $eid, qv.bounded_context = '质量上下文'
            WITH qv
            // JUDGED_BY：指向源 TestResult（source_test_result_id 去重，INV-11）
            MATCH (t:TestResult {test_id: $test_id})
            MERGE (t)-[:JUDGED_BY]->(qv)
            // UNDER_RULE：带 rule_version，可回溯当时生效规则
            FOREACH (_ IN CASE WHEN $rule_id IS NOT NULL THEN [1] ELSE [] END |
              MERGE (qgr:QualityGateRule {rule_id: $rule_id, rule_version: $rule_version})
              MERGE (qv)-[:UNDER_RULE {rule_version: $rule_version}]->(qgr))
            """,
            node_id=f"QualityVerdict:{p['verdict_id']}", vid=p["verdict_id"],
            sn=p["sn"], station_id=p["station_id"], bv=p["business_verdict"],
            defects=p.get("defect_records", []), rule_version=p.get("rule_version"),
            at=p["occurred_at"], eid=e.event_id,
            test_id=p.get("source_test_result_id"), rule_id=p.get("rule_id"),
        )
        # CITES_DEFECT：按 defect_records[].defect_code
        for d in p.get("defect_records", []):
            await tx.run(
                """
                MATCH (qv:QualityVerdict {node_id: $nid})
                MERGE (dc:DefectCatalog {defect_code: $code})
                MERGE (qv)-[:CITES_DEFECT]->(dc)
                """,
                nid=f"QualityVerdict:{p['verdict_id']}", code=d["defect_code"],
            )

    async def _on_defect_catalog(self, e: DomainEvent, tx: AsyncGraphTransaction) -> None:
        """缺陷字典入图，bge-m3 生成 name_embedding 供 SeedResolver 语义近邻。"""
        p = e.payload
        embedding = await self._embedder.embed(p["name"])  # bge-m3, 1024 维
        await tx.run(
            """
            MERGE (dc:DefectCatalog {defect_code: $code})
              SET dc.name = $name, dc.severity = $severity,
                  dc.name_embedding = $embedding, dc.bounded_context = '质量上下文'
            """,
            code=p["defect_code"], name=p["name"],
            severity=p.get("severity"), embedding=embedding,
        )
```

### 9.6 图检索器与种子解析器

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
                WIP_5M1E_CYPHER,  # 即 §6.2 的 Cypher 常量
                sn=seed.value, tenant_scopes=tenant.scopes(),
                as_of=as_of.isoformat(),
            )
            record = await result.single()
        return WipSubgraphMapper.to_subgraph(record, seed, as_of)
```

```python
# app/application/seed_resolver.py
import re
SN_PATTERN = re.compile(r"SN[-_]?[A-Z0-9]+")
WO_PATTERN = re.compile(r"WO[-_]?[A-Z0-9]+")
BATCH_PATTERN = re.compile(r"[Bb][-_]?[0-9A-Z]{4,}")

class SeedResolver:
    """自然语言 -> 图种子。规则优先（SN/工单/批次正则），LLM 兜底，Embedding 做缺陷描述匹配。"""

    def __init__(self, llm: BaseChatModel, embedder: BgeClient, graph: AsyncNeo4jDriver) -> None:
        self._llm = llm; self._embedder = embedder; self._graph = graph

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

### 9.7 启动断言（只读投影校验 + Schema 初始化）

```python
# app/domain/projection.py
class ReadOnlyProjectionGate(Exception):
    """启动时发现非只读投影动作，拒绝启动。"""
class RawDataTopicGate(Exception):
    """启动时发现消费者组误订原始报文主题，拒绝启动。"""

# app/infrastructure/neo4j/schema.py
class SchemaInitializer:
    """启动时幂等执行 §4.5 的约束/索引/向量索引 DDL。"""
    async def init(self, graph: AsyncNeo4jDriver) -> None: ...

# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.projection_registry
    # 启动断言：所有投影处理器只 MERGE 不 DELETE/SET-覆盖历史
    registry.assert_read_only()           # 扫描 Cypher 模板，禁止 DELETE/REMOVE/历史覆盖 SET
    # 启动断言：消费者组只订 4 上下文语义主题，未误订 dc.* 原始流
    registry.assert_no_raw_data_topic()   # 校验订阅列表无 dc.equipment.data.raw 等
    # Schema 初始化（约束/索引/向量索引）
    await app.state.schema_initializer.init(app.state.graph)
    # 初始化消费者组 ...
    async with app.state.kafka_consumer_groups as groups:
        for g in groups:
            asyncio.create_task(g.run())
        yield
```

- `assert_read_only` 扫描所有 `ProjectionHandler` 的 Cypher 模板，禁止出现 `DELETE` / `REMOVE` / 对历史节点的覆盖性 `SET`——只允许 `MERGE` 与新增——红线靠启动断言兜底（与 L1 Agent `ReadOnlyToolGate` 同思路）。
- `assert_no_raw_data_topic` 校验消费者组订阅列表里没有 `dc.*` 原始报文主题，从启动期挡住"高频采集全量入图"。

### 9.8 FastAPI 入口

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

### 9.9 配置与部署

```python
# app/config.py
class Settings(BaseSettings):
    # Neo4j
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str
    # Kafka
    kafka_bootstrap: str = "kafka:9092"
    kafka_topics_mes: list[str] = [
        "mes.checkpoint.lifecycle", "mes.testresult.structured", "mes.routing.progress"]
    kafka_topics_process: list[str] = ["process.route.lifecycle"]
    kafka_topics_material: list[str] = [
        "material.bom.lifecycle", "material.inventory.changed",
        "material.substitute.lifecycle", "material.master.lifecycle",
        "material.supplier.lifecycle"]
    kafka_topics_quality: list[str] = [
        "quality.inspection.verdict", "quality.gate.lifecycle",
        "quality.defect.catalog", "quality.anomaly.batch"]
    # MySQL / Redis
    mysql_dsn: str
    redis_url: str = "redis://redis:6379/0"
    # LLM（可插拔：云端 / 本地化）
    llm_provider: str = "deepseek"   # deepseek / qwen / claude / ollama
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    # Embedding
    embedding_model: str = "BAAI/bge-m3"
    embedding_local: bool = True
    # 兜底阈值
    confidence_threshold: float = 0.6
    projection_lag_threshold_ms: int = 30_000
    model_config = SettingsConfigModel(env_file=".env")
```

```dockerfile
# Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv pip install --system .
COPY app/ ./app/
EXPOSE 8000
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", "-b", "0.0.0.0:8000"]
```

```yaml
# docker-compose.yml（MVP 本地起）
services:
  neo4j:
    image: neo4j:5.20
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD}
      NEO4J_PLUGINS: '["apoc"]'
    ports: ["7687:7687", "7474:7474"]
    volumes: ["neo4j-data:/data"]
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: rag_service
    ports: ["3306:3306"]
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  rag-service:
    build: .
    environment:
      NEO4J_URI: bolt://neo4j:7687
      MYSQL_DSN: mysql+asyncmy://root:${MYSQL_ROOT_PASSWORD}@mysql/rag_service
      KAFKA_BOOTSTRAP: kafka:9092
    ports: ["8000:8000"]
    depends_on: [neo4j, mysql, redis]
volumes: { neo4j-data: {} }
```

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

- **投影滞后兜底**：`projection_lag_ms` 超阈值（如 >30s）-> 检索置信度降权 + `needs_human_review`，并触发 ACL 降级查询补齐缺失节点（降级须带 `as_of`，§7.3；REST 不支持历史查询则取回当前状态、置信度额外降权）。
- **置信度兜底**：`confidence < 0.6` -> 不展示给操作工，只推工程师；与 MES 防错理念一致，宁可拦下让人判。
- **LLM 输出兜底**：`TraceAnswer` 经 Pydantic 校验，不符合 schema 判失败重试；重试仍失败转人工，不硬答。
- **图库故障兜底**：Neo4j 不可用时，`/rag/trace/query` 返回 503 + 降级提示，不阻塞 MES 生产；图可从 Kafka 事件回放重建（节点/边全不可变且由事件驱动）；⚠️ Kafka 有保留期，重建远期图需事件归档（对象存储/S3）作为长期回放源。

---

## 11. 其余 10 上下文的扩展方式

MVP 4 上下文跑通后，其余 10 上下文按**相同范式**扩展，无需改动检索核心：

| 上下文 | 扩展动作 | 补全的 5M1E 维度 |
|--------|---------|----------------|
| 设备工装台账 | 新增 `Asset` / `AssetSpecProfile` 节点；订阅 `eam.*`；过点投影补 `USED_EQUIPMENT` 边；台账投影 `EQUIPPED_WITH_FIXTURE`（equipment->fixture，过点事件无 fixture_id） | Machine |
| 设备数据接入 | 新增 `EquipmentChannel` 节点；**只订** `dc.identity.sn.minted` / `dc.equipment.runtime` / `dc.equipment.alarm.raw` 三个语义主题，不订原始流 | Environment |
| 维修 | 新增 `RepairOrder` 节点；订阅 `repair.*`；建 `REPAIRS` -> `Asset` | Machine |
| 点检保养 | 新增 `MaintenanceTask` 节点；订阅 `pm.*`；建 `INSPECTS` -> `Asset` | Machine |
| 计量检定 | 新增 `CalibrationCert` 节点；订阅 `calibration.*`；建 `CERTIFIES` -> `Asset` | Machine |
| 在制品执行 | `WipUnit` 投影加厚；订阅 `wip.*`；建 `KitStatus` | Man（齐套） |
| 工单管理 | `WorkOrder` 投影加厚；订阅 `wo.*`；`WorkOrderProgress` 节点 | Man |
| 首件处理 | 新增 `FirstArticle` 节点；订阅 `fai.*` | Man |
| 返修 | 新增 `ReworkTask` 节点；订阅 `rework.*`；建 `ROUTES_FROM` / `REENTERS_AT` | Man |
| 返工 | 新增 `BatchReworkOrder` 节点；订阅 `brework.*`；建 `REWORKS_FROM` -> `WorkOrder` | Man |

**扩展步骤统一为四步**（OCP，不改检索核心）：
1. `infrastructure/neo4j/projections/` 新增一个 `ProjectionHandler` 文件；
2. `kafka/consumer_group.py` 新增对应主题前缀的消费者组；
3. `config.py` 新增主题清单；
4. `neo4j/schema.py` 新增节点约束/索引。

检索侧 `GraphRetriever` 的 Cypher 只需在对应维度加 `OPTIONAL MATCH`（向后兼容，空结果不影响 MVP 维度）。

---

## 12. 测试策略

### 12.1 投影幂等测试

- **重复投递不产生重复边**：同一 `CheckpointReleased` 事件投递 2 次，断言图里 `CheckpointRecord` 节点数 = 1、`SNAPSHOT_OF_ROUTE` 边数 = 1（幂等表 + MERGE 双层）。
- **崩溃重放**：模拟位点回退，重放窗口内事件，断言图状态幂等。
- **乱序到达**：`TestResultStructured` 先于 `CheckpointReleased` 到达（跨主题），断言 `PRODUCED_TESTRESULT` 边最终建立（`MERGE` 容忍顺序）。

### 12.2 版本一致性测试

- **工艺变更后历史追溯**：构造 SN-001 在 v1 下过点 -> 工艺变更为 v2 -> 断言 SN-001 的 `SNAPSHOT_OF_ROUTE` 边仍指向 v1，检索 `method_route` 返回 v1（不取当前 v2）。
- **旧版本 DEPRECATED 不删**：`ProcessRouteActivated(v2)` 后，断言 v1 节点 `status=DEPRECATED` 仍存在，历史边不动（INV-CX-02）。
- **降级查询强制版本**：`ProcessManagementAclClient.fetch_route_version(route_version=None)` 抛 `ValueError`。

### 12.3 检索准确性评测集

- 沉淀典型不良场景评测集（每条含：种子 SN + 预期 5M1E 命中节点 + 预期根因类别）。
- pytest + 评测脚本回归：模型 / 提示词 / Cypher 变更后跑评测集，断言 `hypotheses` 覆盖预期类别、`evidence` 引用预期 `node_id`。
- **置信度校准**：评测集上 `confidence` 与人工标注的"根因正确性"做相关性，校准 `<0.6` 阈值。

---

## 13. 实现步骤（聚焦 4 上下文 MVP）

### 阶段一：骨架与最小图投影（2 周）

1. 搭 `rag_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构；`docker-compose` 起 Neo4j + MySQL + Redis。
2. 接 Neo4j 5.x async driver，`SchemaInitializer` 执行 §4.5 DDL（约束/索引/向量索引）。
3. 实现 `GraphProjector` + 幂等表 + 位点表（§5.3），接一个消费者组（`rag-mes`）跑通。
4. 实现 `CheckpointProjectionHandler`（§9.2），验证 `CheckpointReleased` -> 节点 + `SNAPSHOT_OF_ROUTE` 边入图。
5. 实现 `ReadOnlyProjectionGate` / `assert_no_raw_data_topic` 启动断言（§9.7）。

### 阶段二：4 上下文投影（2 周）

6. 实现 `ProcessRouteProjectionHandler`（§9.3）、`MaterialProjectionHandler`（§9.4）、`QualityProjectionHandler`（§9.5）。
7. 按主题前缀起 4 个消费者组（§5.2），验证主题订阅清单无 `dc.*`。
8. 验证幂等：重复投递事件不产生重复边（幂等表 + MERGE 双层，§12.1）。
9. 验证版本：`ProcessRouteActivated` 投影新版本，旧版本 `DEPRECATED` 不删，历史 `SNAPSHOT_OF_ROUTE` 边不动（§12.2）。

### 阶段三：5M1E 检索与 LLM 综合（2 周）

10. 实现 `GraphRetriever.expand_5m1e`（§9.6，§6.2 Cypher），带租户/版本/时间窗过滤。
11. 实现 `SeedResolver`（§9.6）：规则优先 + bge-m3 缺陷向量匹配 + LLM 兜底。
12. 实现 `TraceRetrievalService` + LLM 综合（§7.2），`TraceAnswer` Pydantic 强约束 + 置信度阈值。
13. FastAPI 端点 `/rag/trace/query` / `/rag/trace/expand`（§9.8）；同步沉淀**金标准评测集**（典型不良场景 + 预期 5M1E 假设 + 证据节点）作为检索/提示词迭代锚点--不待阶段五。

### 阶段四：版本一致性、ACL 与权限加固（2 周）

14. 工艺/BOM/规则查询强制版本入参，ACL 层校验 `ACTIVE` 状态（§7.3）。
15. `CONSUMED_BATCH` 降级查询 `MaterialAclClient.fetch_consumption` 补齐（🔴 §5.1）。
16. 租户过滤在 Cypher 前置，权限不达标看不到节点（§1.2）。
17. 接 OpenTelemetry + prometheus 指标（§10.1），`projection_lag_ms` 降权兜底（§10.3）。
18. 子图缓存（redis）按种子 + as_of + 租户去重。

### 阶段五：测试、评测与 L1 对接（2 周）

19. 跑投影幂等测试 + 版本一致性测试（§12.1/§12.2）。
20. 扩充评测集（阶段三金标准之上补全边缘场景），回归模型 / 提示词 / Cypher / 裁剪策略变更（§12.3）。
21. 对接 L1 Agent：`query_traceability_graph` 工具封装 `/rag/trace/query`（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §5.5）。
22. 灰度一条产线，收集工程师反馈，按 §11 扩展 Machine/Environment 维度。

---

## 14. 约束落地检查清单

- [ ] 所有投影处理器只 `MERGE`，无 `DELETE`/`REMOVE`/历史覆盖性 `SET`，`ReadOnlyProjectionGate` 启动断言生效。
- [ ] 消费者组未订阅 `dc.*` 等原始报文主题，`assert_no_raw_data_topic` 启动断言生效。
- [ ] `event_id + consumer_group` 幂等表 + Neo4j `MERGE` 双层去重，重复投递不产生重复边。
- [ ] 消费者位点落 MySQL，重启从断点续跑，投影事务成功后才 ack offset；`enable.auto.commit=false`。
- [ ] `CheckpointRecord` 节点带 `route_version`，`SNAPSHOT_OF_ROUTE` 边指向当时版本；工艺变更只新增版本节点、旧版本 `DEPRECATED` 不删，历史边不动（INV-CX-02）。
- [ ] 检索 `RouteVersion` 带 `route_version` / `status=ACTIVATED` 过滤；降级查询强制 `route_version` 入参（§7.3）。
- [ ] `BomActivated` / `QualityGateRuleActivated` 同理：新版本入图、旧版本 `DEPRECATED` 不删。
- [ ] 租户 `tenant_scope` 在 Cypher `WHERE` 前置过滤，权限不达标看不到节点。
- [ ] RAG 服务不进过点主事务（[领域总览.md](../../领域模型/领域总览.md) §5.3），图投影秒级最终一致，过点 ≤200ms（§4.1 设备实时数据 REST 查询）不受影响。
- [ ] 检索结果 `TraceSubgraph` 结构化，节点带 `source_event_id` 证据可回溯。
- [ ] LLM 输出经 Pydantic `TraceAnswer` 校验，5M1E 分类固化为 Enum，失败重试。
- [ ] `confidence < 0.6` 或投影滞后超阈值 -> `needs_human_review`，不展示给操作工。
- [ ] 图库故障返回 503 不阻塞 MES 生产；图可从 Kafka 事件回放重建。
- [ ] `CONSUMED_BATCH` 边的契约 gap（🔴 §5.1）已登记：`MaterialConsumed` 含 `sn` 无 `lot_no`，MVP 用降级查询兜底，待物料上下文将 `lot_no` 加入 payload 后改为事件投影。
- [ ] 所有答案带 disclaimer：辅助假设，最终处置需工程师确认。

---

## 15. 面试防守 Q&A

**Q：追溯型 RAG 和通用 RAG 有什么本质区别？**
A：通用 RAG 是向量检索文档；追溯型 RAG 是 **GraphRAG + 领域事件流**，把物料批次 / 单件 / 工艺版本 / 过点记录 / 测试结果之间的引用显式建成图边。输入"某单件焊接不良"，它能按 5M1E 自动串起这条单件的全链路。这套图谱建立在我已有的 14 个限界上下文（13 个入图）和 `routeVersion`、`source_work_order_id`、`asset_id`、`batch_no` 这些跨上下文引用上——别人没有这套领域模型，抄不走（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.1）。

**Q：图怎么保证和 MES 的真实状态一致？**
A：图是领域事件的**只读投影**，不是事实源。事实源是各上下文的聚合根。图通过订阅 Kafka 事件增量更新，与在制品执行上下文的 `ProcessRouteCache` 同构——都是事件投影的读模型。一致性靠三道闸：投影闸（`ProcessRouteActivated` 入新版本、旧版本 `DEPRECATED` 不删）、检索闸（按 `CheckpointRecord.route_version` 取当时版本快照，不取当前生效版）、输出闸（证据必须含 `route_version`）。版本一致性不是 RAG 自己保证的，是从领域模型兜上来的。

**Q：MVP 为什么只做 4 个上下文，能跑通 5M1E 吗？**
A：4 个上下文（在制品执行 + 工艺管理 + 物料 + 质量）覆盖了 5M1E 的 Man/Material/Method/Measurement 四维，是一条 SN 追溯闭环的核心。Machine（设备台账/维修/点检/计量）和 Environment（设备数据接入语义采样）依赖设备相关上下文，按相同范式扩展（§11），检索 Cypher 加 `OPTIONAL MATCH` 即可向后兼容。先 4 上下文跑通闭环验证价值，再按需扩展——这是按"价值/依赖"排的，不是拍脑袋。

**Q：物料消耗的 sn↔batch 明细怎么来？**
A：这是个真实的契约 gap。物料上下文消费 `mes.checkpoint.lifecycle`(CheckpointReleased) 做 `ConsumeInventory`（按 BOM `consumption_rule` 扣减，BIZ-04），对外发 `material.inventory.changed`（库存变更，不含 sn↔batch 明细）与 `MaterialConsumed`（含 `sn` 但无 `lot_no`）--sn↔batch 映射在事件契约缺失。MVP 用降级查询物料上下文只读 REST `GET /api/material/consumption?sn=&work_order_id=` 补齐 `CONSUMED_BATCH` 边（§7.3），待物料上下文明确消耗明细事件后改为事件投影。这种"识别 gap -> 降级兜底 -> 推动契约闭环"的处理方式，比假装没有 gap 更负责任。

**Q：会不会拖慢过点？**
A：不会进过点主事务。过点 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1，设备实时数据 REST 查询）是硬约束，图投影是异步消费事件，与过点判定完全解耦（§5.3 过点主事务零分布式事务）。图允许秒级最终一致——追溯型 RAG 是事后诊断工具，不是实时过点判定，秒级滞后可接受，滞后超阈值降置信度兜底。

**Q：图会不会越来越大，性能怎么办？**
A：一是高频采集不全量入图——设备原始 `DataPacket` 不进图，MVP 消费者组不含任何 `dc.*` 主题，`assert_no_raw_data_topic` 启动断言兜底。二是历史节点不可变但可冷热分层——完工工单的子图可归档到冷存储，热图只留近期在制。三是检索是确定性的 Cypher 一跳/两跳扩展，不是全图遍历，配合 §4.5 索引与 `tenant_scope` 前置过滤，性能可控。

**Q：为什么不让 LLM 直接查 MES 数据库，要费劲建图？**
A：两个原因。一是 LLM 直接查原始表会绕过领域边界，权限和版本都兜不住——错给一条已失效工艺就批量不良。图把跨上下文引用显式建成边，版本做成节点 + 快照边，检索带 `route_version` 过滤，从结构上杜绝失效工艺。二是图是事件投影的读模型，一次 Cypher 扩展就能取齐 5M1E，比 LLM 现场跨界面串快得多、准得多。LLM 只负责综合，不负责找路径。

**Q：追溯型 RAG 和 L1 诊断型 Agent 什么关系？是不是重复了？**
A：不重复，是分层。追溯型 RAG 是"图 + 一次检索综合"，给"这条单件 5M1E 全貌"；L1 Agent 是"多步 ReAct 工具调用"，给"根因要递进追问"的深度诊断。L1 的 `query_traceability_graph` 工具封装本文 `/rag/trace/query`，把图作为快路径，复杂场景再自己多步深挖。先有图、后有 Agent——图没建起来 L1 退化为纯工具循环，体验差。

**Q：图错了或漏了怎么办？**
A：图是只读投影，错了不影响 MES 生产——事实源在聚合根，图崩溃返回 503 不阻塞过点。漏了靠 ACL 降级查询补齐（§7.3，与过点"缓存未命中降级远程查询"同构）。重建靠事件回放——节点/边全不可变且由事件驱动，从 Kafka 位点重放即可完整重建图库，无需 MES 侧配合。所有答案带置信度，低置信度转人工，与 MES 防错理念一致。

**Q：上线了吗？**
A：这是设计阶段的引入规划，不是已落地。重点是图谱建模对齐 14 个限界上下文（13 个入图）、图作为领域事件的只读投影、版本快照不可变这三条架构判断；本文进一步把核心 4 上下文落到依赖清单、Kafka topic 清单、REST 契约、Docker、测试策略。落地需要先做文档型 RAG（路线 B）验证车间可用性，再建图——按"先 B 后 A"的顺序推进。诚实 + 体现架构判断力，比硬吹"已上线 GraphRAG"得分高。

---

## 16. 一句话定位

"追溯型 RAG 用 Python + Neo4j 把 MES 已有的全链路追溯做成属性图——节点对齐 14 个限界上下文（13 个入图）的聚合根、边把 `routeVersion`/`source_work_order_id`/`batch_no` 这些跨上下文引用显式建出来，图本身是领域事件的只读投影、靠 `event_id` 幂等消费、靠 `SNAPSHOT_OF_ROUTE` 快照边锁死版本不可变。本文把核心 4 上下文（在制品执行 + 工艺管理 + 物料 + 质量）落到 4 个投影处理器 + 5M1E Cypher + ACL 降级 + Docker 部署 + 测试策略，跑通 Man/Material/Method/Measurement 四维闭环，Machine/Environment 按相同范式向后扩展——全程不进过点主事务、不回写 MES，低置信度转人工，是建立在追溯护城河上的差异化能力，别人没有这套领域模型抄不走。"
