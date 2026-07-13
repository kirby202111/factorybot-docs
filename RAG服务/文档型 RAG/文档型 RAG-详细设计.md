# 文档型 RAG 详细设计（向量检索 + 版本治理）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.2 路线 B（文档型 RAG）的落地展开，输出**技术栈、文档领域建模、摄入与切分、版本治理与重索引、检索生成、与领域耦合点、关键设计决策与约束落地**。
> **技术栈**：Python（FastAPI + **PGVector** + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 Kafka 只读事件 + REST 只读查询解耦，互不侵入。
> **口径纪律**：文档型 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了文档 RAG"。MES 领域对错误答案零容忍（错给一条已失效 SOP 会直接导致批量不良），所以本文强调**向量是主体，但版本治理才是灵魂**--检索必须带 `route_version` / 生效状态过滤，且由领域事件驱动重索引，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 目标

把 MES 车间的**半结构化 / 非结构化知识**（SOP、作业指导书、IPC 标准、设备维修手册、检验标准、历史 8D 报告、培训资料）做成可被 LLM 检索 + 综合的**向量知识库**，让"SPI 报警怎么处置""首件检验流程是什么"这类问题不再依赖工程师翻文档 / 翻历史聊天，而是一次向量检索 + LLM 综合即给答案 + **可点开的来源引用**。

典型场景：

1. **操作工 / 线长**："SPI 3 号机报 solder_insufficient 怎么处置？" -> 检索 SPI 报警处置 SOP + 设备维修手册对应章节 -> 给一句话原因 + 一个动作 + 引用片段。
2. **工艺 / 质量工程师**："首件检验流程是什么？" -> 检索首件检验标准（绑定当前生效工艺版本）-> 给流程步骤 + 引用。
3. **设备工程师**："贴片机 M-200 报 E027 怎么修？" -> 检索该设备型号维修手册 + 历史同类维修记录 -> 给处置步骤 + 引用。
4. **与追溯型协同**：追溯型 RAG 给"这条单件用了哪批锡膏、哪台设备"（结构化事实链），本文档给"SPI 报警怎么处置"（处置知识）--两者版本过滤都对齐 `ProcessRouteActivated`。

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **只读 MES** | 文档库归 RAG 服务自有；RAG 对 MES 只读--订阅事件 + 只读 REST 查文档绑定关系，从不回写 MES | 文档元数据 / 向量存 PGVector（RAG 自有库）；订阅 `process.*` / `material.*` / `quality.*` 只读事件；降级查询只读 REST |
| **不进过点主事务** | 摄入 / 重索引异步消费事件，与过点判定完全解耦 | 过点 P99 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1）不受文档索引影响；文档检索容忍秒级延迟（[RAG服务引入路线.md](../RAG服务引入路线.md) §4） |
| **版本一致性** | 工艺 / BOM / 质量规则有版本生命周期，检索 SOP / 作业指导书必须带版本过滤，否则答出**已失效工艺** | 文档版本绑定 `route_version` / `bom_version` / `rule_version`；订阅 `ProcessRouteActivated` 等触发重索引；检索带 `version` + `status=PUBLISHED` 过滤（§5.4 / §6.2） |
| **权限隔离** | 不同车间 / 产线 / 角色能看的文档不同，检索层带租户 / 权限上下文 | 文档元数据带 `tenant_scope`，向量检索时**前置过滤**（PGVector pre-filter），不是答完再裁剪 |
| **可观测兜底** | 所有答案带来源引用 + 置信度，低置信度转人工 / 转规则引擎 | 检索结果结构化（chunk_id + 文档版本 + 页码/定位）+ 置信度阈值；与 MES 防错理念一致：宁可拦下让人判 |
| **部署形态** | 车间网络常与办公网隔离，需本地化向量库 / Embedding / 模型 | PGVector + bge-m3 本地化部署；LLM 视安全策略云端 API 或本地化模型二选一（§2.5） |

### 1.3 与追溯型 RAG、L1 Agent 的关系

文档型 RAG（路线 B）与追溯型 RAG（路线 A，[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md)）不是替代，是**互补分工**，二者共享同一套版本治理契约：

| | 追溯型 RAG（路线 A） | 文档型 RAG（路线 B，本文） |
|---|-------------------|--------------------------|
| 检索主体 | 属性图（GraphRAG + 领域事件流） | 向量库（PGVector） |
| 答的是 | "这条单件 5M1E 全貌"（结构化事实链） | "SPI 报警怎么处置"（处置知识） |
| 数据来源 | 14 上下文聚合根的事件投影 | SOP / 手册 / 标准 / 8D 等文档 |
| 版本一致性 | `SNAPSHOT_OF_ROUTE` 快照边锁当时版本 | 文档版本绑定 `route_version` + 重索引 |
| 协同点 | `TraceAnswer.suggested_action` 调本文 `search_docs(query, route_version_filter)` | 本文接收追溯型发布的 `rag.reindex.request` 事件触发重索引 |

- **L1 诊断型 Agent**（[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §2.2）把本文 `POST /rag/docs/query` 封装成 `search_docs` 工具，作为其多步推理的"处置知识"工具之一；与 `query_traceability_graph`（追溯型）并列。
- **落地顺序**：先 B 后 A（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。文档型 2–4 周可出 demo，先验证"车间到底有没有人用 RAG"；追溯型建立在领域事件和过点记录上，是护城河但建模重。

### 1.4 与 Java 技术栈的关系

- RAG 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只订阅 Kafka 只读事件、调只读 REST 查文档绑定关系。
- 跨语言的物理边界反而是好事：RAG 服务无法共享 Java 事务 / 内存，天然强制只读、不进过点主事务、不旁路应用服务写路径。
- 复用 [实现说明](../../实现说明/) 既有的 Kafka topic、REST 只读接口、领域事件 envelope（`event_id` / `event_type` / `event_version` / `occurred_at` / `source_service` / `trace_id` / `partition_key`，见 [消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §4.3）、消费侧幂等模式（§6 同事务幂等 + 手动 ack），不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 类型提示 + Pydantic 校验，AI 生态最成熟，与追溯型 RAG / L1 Agent 同栈复用 LLM 抽象 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI，与追溯型一致，适合做检索 HTTP 入口 |
| 向量存储 | **PGVector**（PostgreSQL 16 + pgvector 扩展） | 文档元数据（版本 / 生效状态 / 租户 / 绑定）与向量**同库存**，版本+权限组合过滤走 SQL `WHERE` 前置，正是文档型核心难点；可本地化、运维统一 |
| 向量存储（替代） | Milvus / Qdrant | 标量过滤需求简单、追求极致检索吞吐时备选，见 §2.3 取舍 |
| 检索编排 | **LlamaIndex** `VectorStoreIndex` | 文档摄入 / 切分 / 向量检索 / Rerank 的上层抽象，文档型 RAG 的主场 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 模型可插拔，配置切换 Claude / 通义千问 / DeepSeek / 本地化模型，与追溯型 / L1 一致 |
| Embedding | **bge-m3**（多语种，可本地化，1024 维） | SOP / 手册 / 问题的语义向量化，覆盖中英文，与追溯型同模型同维度 |
| Rerank | **bge-reranker-v2-m3**（可本地化） | 向量召回后精排，提升 top-k 相关性，中英文兼顾 |
| 数据校验 | **Pydantic v2** | 检索请求 / 文档视图 / 答案 DTO 的 schema 即类型 |
| HTTP 客户端 | **httpx**（异步） | 降级查询各上下文只读 REST（查文档与领域版本的绑定关系） |
| 消息 | **aiokafka** | 订阅领域事件（`process.*` / `material.*` / `quality.*`）触发重索引，异步非阻塞 |
| 元数据持久化 | **SQLAlchemy 2.0 (async) + asyncpg** | 文档元数据 / 向量 / 幂等 / 位点 / 检索审计同库（PGVector 即 PostgreSQL） |
| 对象存储 | **MinIO**（与既有 [MinIO配置说明.md](../../实现说明/基础设施/MinIO配置说明.md) 一致） | 原始文档文件（PDF / Word / 图片）存对象存储，向量库只存文本 chunk + 向量 + 定位 |
| 缓存 | **redis-py (async)** | 检索结果短期缓存（同 query + 版本 + 租户命中即用） |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 串联、指标告警 |
| 配置 | pydantic-settings | 环境变量 / 配置文件统一管理 |
| 部署 | 独立微服务 `rag-doc-service`（uvicorn + gunicorn worker） | 与三大服务同网格，K8s 部署 |

### 2.2 为什么是纯向量检索（而非 GraphRAG）

- 文档型 RAG 处理的是**非结构化文本**：SOP 的某段处置步骤、手册的某章故障代码说明，答案集中在少数段落，是"局部、事实性"查询（见 [基础问题.md](../问题归纳/基础问题.md) §二"传统 RAG 适用场景"）。
- 文档之间是**弱关联**（一份 SOP 和一份手册之间没有 `source_work_order_id` 这种显式引用），向量相似度足以召回，不需要图遍历。
- 这区别于追溯型 RAG（路线 A）：追溯链是结构化关系（`SN -> 过点记录 -> 设备 / 工艺版本 / 物料批次`），必须 GraphRAG 把引用建成图边（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §2.2）。
- **一句话**：答案藏在某段话里 -> 向量检索（本文）；答案靠"关系拼出来" -> GraphRAG（追溯型）。两者在 MES 里分工互补（[基础问题.md](../问题归纳/基础问题.md) §五）。

### 2.3 为什么选 PGVector（而非专用向量库）

文档型 RAG 的核心难点不是召回率，是**版本 + 权限 + 生效状态的组合过滤**--这正是 PGVector 相对 Milvus / Qdrant 的优势所在：

| 维度 | PGVector | Milvus / Qdrant |
|------|----------|-----------------|
| 标量过滤表达力 | 完整 SQL：`WHERE route_version=$rv AND status='PUBLISHED' AND tenant_scope IN (...) AND doc_type IN (...)` 任意组合 | 标量字段过滤，复杂组合（尤其 IN / 范围 / 多字段 AND）表达力弱于 SQL |
| 过滤时机 | **pre-filtering**（先 SQL 过滤再向量近邻），版本/权限不达标的文档根本不进候选 | 部分模式 pre-filter、部分 post-filter，过滤 + 近邻的交互易丢召回 |
| 元数据与向量一致性 | 同库同事务：文档元数据 upsert 与向量 upsert 原子提交，不会"元数据已 DEPRECATED 但向量仍可召回" | 元数据与向量常分库，跨库一致性需额外保证 |
| 运维 | 一个 PostgreSQL 实例，车间本地化部署成本低，与既有运维栈接近 | 独立集群，运维 + 资源开销更大 |
| 检索吞吐 | 数百万级 chunk 足够（文档库规模远小于追溯图） | 千万级以上向量 + 高 QPS 时更优 |

- 文档库规模（SOP / 手册 / 标准，数万文档、数百万 chunk 量级）远未到 PGVector 的性能瓶颈，而其 SQL 过滤表达力正好兜住"版本治理"这个核心难点。
- HNSW 索引（`pgvector` 0.6+）在百万级 chunk 上检索延迟可控制在数十毫秒，满足文档型"容忍秒级"的 SLA。
- 若未来文档量或 QPS 上到专用向量库的甜区，可替换为 Milvus / Qdrant，LlamaIndex `VectorStoreIndex` 抽象保证切换成本可控--但**版本过滤逻辑要从 SQL 改写为各库的标量过滤 API**，这是替换的主要工作量，需提前在检索层抽象（§6.2）。

### 2.4 为什么版本治理建立在领域事件之上

- 文档型 RAG 最危险的环节是**工艺变更后文档没跟上**：工艺路线从 v1 升到 v2，若 SOP 还停在 v1 的处置步骤，操作工按失效 SOP 操作 -> 批量不良。这是和通用 RAG 最大的区别（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.2 / §5 Q&A）。
- 版本一致性不是文档型 RAG 自己保证的，是**从领域模型兜上来的**--工艺版本有生命周期（`RouteVersionState`：DRAFT/SUBMITTED/ACTIVATED/DEPRECATED/ARCHIVED）、变更发 `ProcessRouteActivated` 事件、过点记录绑 `routeVersion`（[领域总览.md](../../领域模型/领域总览.md) §5.1）。文档型 RAG 只是严格遵循这套契约：
  1. 订阅 `ProcessRouteActivated` / `ProcessRouteDeprecated` / `BomActivated` / `QualityGateRuleActivated` 事件，触发关联文档重索引 / 重新绑定 / 状态流转（§5.4）。
  2. 检索时带 `route_version` + `status=PUBLISHED` 过滤，从结构上杜绝失效文档（§6.2）。
  3. 接收追溯型 RAG 发布的内部 `rag.reindex.request` 事件（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §5.4），与追溯型重索引对齐。
- 这套机制与追溯型 RAG 的"版本即节点 + 快照边"（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §4.4）是**同一套版本契约的两个投影面**：追溯型把版本做成图节点，本文把版本做成文档元数据过滤维度。两者都由 `ProcessRouteActivated` 驱动，保证"图里的工艺版本"与"文档绑定的工艺版本"一致。

### 2.5 部署形态（车间网隔离）

- 车间网络常与办公网隔离（[RAG服务引入路线.md](../RAG服务引入路线.md) §4 硬约束）。**建议**：向量库（PGVector）与 Embedding（bge-m3）/ Rerank（bge-reranker）本地化部署，LLM 视车间安全策略二选一--云端 API（质量高、需出网）或本地化模型（离线、质量折衷）。`BaseChatModel` 抽象保证两者切换零代码改动。
- 原始文档文件（PDF / Word / 图片）存 MinIO（与既有对象存储一致），向量库只存文本 chunk + 向量 + 定位（页码 / 段落偏移），避免大文件撑爆 PG。
- 车间网若严格离线，LLM 走本地化模型（如 Qwen / DeepSeek 本地部署），bge-m3 / bge-reranker 本地 GPU 或 CPU 推理均可（文档型对延迟不敏感，CPU 推理可接受）。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ rag-doc-service（独立微服务，Python + FastAPI + PGVector）          │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ DocumentRetrievalService                  │  │
│  │ /rag/docs/*  │  │  query改写 -> 向量检索+过滤 -> rerank -> LLM │  │
│  └──────────────┘  └─────────────────┬────────────────────────┘  │
│                                      │                            │
│              ┌───────────────────────┼───────────────────────┐    │
│              ▼                       ▼                       ▼    │
│  ┌───────────────────┐  ┌─────────────────────┐  ┌──────────┐ │
│  │ DocumentIngestion │  │ VectorRetriever     │  │ LLM      │ │
│  │ Service           │  │ (PGVector pre-filter)│  │ Synth    │ │
│  │ 解析->切分->向量化 │  └──────────┬──────────┘  └──────────┘ │
│  └────────┬──────────┘             │                          │
│           │                        ▼                            │
│  ┌────────▼────────┐        ┌──────────────────┐                │
│  │ ReindexCoordinator│       │ MinIO            │                │
│  │ 事件 -> 重索引/绑定 │       │ (原始文档文件)    │                │
│  └────────┬────────┘        └──────────────────┘                │
│           │                                                        │
│  ┌────────▼──────────────────────────────────────┐               │
│  │ PostgreSQL + PGVector                          │               │
│  │  knowledge_document / document_version /       │               │
│  │  document_chunk(+embedding) / document_binding │               │
│  │  + index_idempotency / index_offset            │               │
│  └───────────────────────────────────────────────┘               │
└───────────────────────────────────┼──────────────────────────────┘
                                    │ 订阅领域事件（只读，触发重索引）
                          ┌─────────▼──────────┐
                          │ aiokafka Consumer   │
                          │ process.* material.*│
                          │ quality.*           │
                          │ + rag.reindex.*     │← 追溯型 RAG 重索引请求
                          └─────────────────────┘
                                    ▲
                                    │ 各上下文 Outbox 投递（至少一次）
              ┌─────────────────────┴─────────────────────┐
              │  制造资源 / 设备管理 / 生产执行 三大服务    │
              │  （Java/Spring，事实源）                   │
              └───────────────────────────────────────────┘
```

### 3.1 关键设计决策

- **文档库即 RAG 自有读模型**：`knowledge_document` / `document_version` / `document_chunk` 归 RAG 服务自有，是文档领域事件的投影。事实源是文档本身（MinIO 原始文件）+ 领域版本（MES 聚合根）；文档库崩溃不影响生产，重建即可（文档文件 + 事件回放）。
- **摄入与检索分离**：`DocumentIngestionService`（写：解析+切分+向量化+持久化）与 `VectorRetriever`（读：向量检索+过滤）解耦，摄入滞后不阻塞检索--检索带 `version` + `status=PUBLISHED` 过滤，未生效的新版本天然不召回。
- **版本治理三道闸**：① 摄入闸（文档版本绑定领域版本，状态机管控生效）；② 重索引闸（领域事件驱动文档状态流转 / 重新绑定）；③ 检索闸（向量检索带 `version` + `status` 前置过滤）。不靠口头约束（§5.4 / §6.2）。
- **元数据与向量同库**：PGVector 让文档版本 / 生效状态 / 租户 / 绑定关系与向量同库存，组合过滤走 SQL `WHERE` 前置--这是版本治理能兜住的技术根基（§2.3）。
- **ACL 防腐层**：降级查询各上下文只读 REST（查文档与领域版本的绑定关系）经 ACL 适配，外部 DTO -> 内部视图（`DocumentVersionView` / `BindingView`），外部 schema 变化不污染检索核心。符合 CLAUDE.md 的低耦合 / ACL 约束。

---

## 4. 文档领域建模

文档型 RAG 自有一个轻量**文档子域**（RAG 服务内部，非 MES 14 上下文之一）。建模遵循 CLAUDE.md 的 OOD / DDD 约束：聚合根管一致性，值对象管不变量，领域服务编排跨实体逻辑。

### 4.1 文档类型分类（按是否绑定领域版本）

文档按"是否随领域版本变化"分三类，直接决定版本治理策略：

| 类别 | 文档类型 | 是否绑定领域版本 | 版本治理策略 |
|------|---------|----------------|-------------|
| **工艺绑定型** | SOP、作业指导书、检验标准 | ✅ 绑定 `route_version` / `rule_version` | 随工艺版本生命周期重索引；检索必须带版本过滤 |
| **设备绑定型** | 设备维修手册、设备操作手册 | ✅ 绑定 `asset_id` / 设备型号 | 按文档自身版本管理，不随工艺版本变；检索按设备 / 型号过滤 |
| **通用知识型** | IPC 标准、培训资料、8D 报告 | ✗ 不绑定具体领域版本 | 按文档自身版本 + 生效状态管理；检索按主题 / 标签过滤 |

> 🔴 **8D 报告是否纳入 MVP**：8D 是历史处置案例（"曾经怎么处理的"），与 SOP / 手册（"应该怎么做"）性质不同--前者是案例检索，后者是规范检索。是否在 MVP 阶段纳入 8D、是否单独做案例型检索（可能需不同切分 / 提示词策略）交还用户决策。本文 MVP 先不纳入，放 §9 扩展。

### 4.2 聚合根 / 实体 / 值对象

```text
KnowledgeDocument（聚合根）
  ├─ document_id            文档唯一标识
  ├─ doc_type               文档类型（SOP / MANUAL / STANDARD / ...）
  ├─ title                  标题
  ├─ category               类别（工艺绑定型 / 设备绑定型 / 通用知识型）
  ├─ tenant_scope           租户范围（workshop/line，权限过滤）
  └─ DocumentVersion（实体，版本集合）
       ├─ version_id            版本唯一标识
       ├─ document_id           归属文档
       ├─ version_no            版本号
       ├─ state                 版本状态（DRAFT/SUBMITTED/PUBLISHED/DEPRECATED/ARCHIVED）
       ├─ source                DocumentSource（值对象：上传 / 同步 / 来源系统）
       ├─ file_ref              原始文件 MinIO URI
       ├─ effective_at          生效时间
       ├─ deprecated_at         废弃时间
       ├─ DocumentBinding（值对象，绑定关系集合）
       │    ├─ binding_type     绑定维度（ROUTE_VERSION / BOM_VERSION / RULE_VERSION / ASSET / ASSET_MODEL）
       │    ├─ target_ref       绑定目标（如 route_id+route_version / asset_id）
       │    └─ inherited        是否继承自上一版本（§4.4）
       └─ DocumentChunk（实体，分块集合）
            ├─ chunk_id             分块唯一标识
            ├─ version_id           归属版本
            ├─ ordinal              分块序号
            ├─ text                 分块文本
            ├─ embedding            向量（bge-m3, 1024 维）
            ├─ locator              ChunkLocator（值对象：页码 / 段落偏移 / 标题路径）
            └─ section_type         章节类型（步骤 / 故障代码 / 参数 / 注意事项 / ...）
```

- **`KnowledgeDocument` 是聚合根**：管文档元信息 + 版本集合的一致性。一个文档可有多个版本，但同一时间**同类绑定唯一 PUBLISHED**（对齐工艺 INV-02"同 route 唯一 ACTIVE"、物料 INV-02"同 product 同 bom_type 唯一 ACTIVE"）。
- **`DocumentVersion` 是聚合内实体**：版本是文档的不可变快照，一旦 PUBLISHED 后内容不可改（对齐 `CheckpointRecord` INV-12 不可变）。改内容只能发新版本。
- **`DocumentChunk` 是版本内实体**：分块属于某个版本，随版本生命周期。向量挂在 chunk 上。
- **`DocumentBinding` 是值对象**：绑定关系是不可变的，描述"这个文档版本对齐哪个领域版本"。`inherited` 标记绑定是否从上一版本继承（§4.4）。
- **领域服务**：
  - `DocumentIngestionService`：编排摄入（解析 -> 切分 -> 向量化 -> 持久化 + 发 `DocumentVersionPublished` 事件），单一职责（SRP）。
  - `ReindexCoordinator`：响应领域事件（`ProcessRouteActivated` 等），协调文档版本状态流转 / 重新绑定 / 重索引，跨多文档版本逻辑放服务而非实体（CLAUDE.md 领域服务约束）。
  - `DocumentRetrievalService`：编排检索（query 改写 -> 向量检索 + 过滤 -> rerank -> LLM 综合），SRP。

### 4.3 文档版本生命周期

文档版本状态机对齐工艺路线版本生命周期（`RouteVersionState`）：

```mermaid
stateDiagram-v2
    [*] --> DRAFT: 新建/上传
    DRAFT --> SUBMITTED: 提交审核
    SUBMITTED --> PUBLISHED: 审核通过(生效)
    SUBMITTED --> DRAFT: 驳回
    PUBLISHED --> DEPRECATED: 新版本PUBLISHED / 关联领域版本DEPRECATED
    DEPRECATED --> ARCHIVED: 归档(不再默认检索,历史引用可追溯)
    ARCHIVED --> [*]
```

- **PUBLISHED** = 可被默认检索（对应工艺 `ACTIVATED`）。
- **DEPRECATED** = 不再被默认检索，但**已发布的引用仍可追溯**（历史答案里引用过的 SOP 片段仍能点开）--不删除，对应工艺版本 `DEPRECATED` 不删（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §4.4）。
- **新版本 PUBLISHED 时旧版本自动 DEPRECATED**：同文档同类绑定唯一 PUBLISHED（§4.2），与工艺 / BOM 同构。
- 🔴 **状态机是否完全对齐工艺路线状态机**：工艺有 `SUBMITTED` 审核态，文档是否也需要独立的文档审核流（与工艺审核解耦）？还是文档随工艺 `ProcessRouteActivated` 联动 PUBLISHED（工艺生效即文档生效）？两种策略影响文档发布的责任归属（文档 owner vs 工艺 owner），交还用户决策。本文默认文档有独立审核流，工艺生效事件触发的是"重新绑定 / 重索引"而非"直接 PUBLISHED"。

### 4.4 文档与领域版本的绑定（🔴 核心决策点）

这是文档型 RAG 最关键的设计点，直接决定重索引行为。工艺从 v1 升到 v2 时，绑定的 SOP 怎么变？三种方案：

| 方案 | 绑定粒度 | 工艺 v1->v2 时文档行为 | 优缺点 |
|------|---------|---------------------|--------|
| **A. 1:1 绑定 routeVersion** | 文档版本绑定具体 `route_version` | 必须为新工艺 v2 发新 SOP 版本并绑定 | 强一致，但工艺微调（SOP 内容无需变）也要重发文档，开销大 |
| **B. 文档独立版本化 + 绑定关系（推荐）** | 文档版本独立版本化，通过 `DocumentBinding` 多对多关联 `route_version` | 若 SOP 内容不变，绑定 `inherited=true` 继承到 v2；若内容变则发新文档版本绑定 v2 | 灵活，SOP 不随工艺微调重发；但需"内容是否需变"的判断（人工 or 比对） |
| **C. 绑定 route（工艺路线）而非 routeVersion** | 文档绑定 `route_id`，检索时按当前生效版本取关联文档 | 工艺升版文档不动，检索自动取当前生效工艺对应的文档 | 最省事，但**无法回溯历史**--查历史单件时取不到当时对应的 SOP（违背版本一致性） |

- 🔴 **本文推荐方案 B**，理由：① 不因工艺微调就重发 SOP（避免无效文档版本膨胀）；② 保留历史可回溯（绑定关系带 `route_version`，查历史单件按当时版本取 SOP）；③ `inherited` 标记区分"主动绑定"与"继承绑定"，继承绑定时若内容需变可告警提示人工确认。
- 🔴 但"工艺升版时 SOP 内容是否需要变更"的判断逻辑（自动比对 vs 人工确认 vs 工艺工程师声明）交还用户决策。本文 MVP 默认：工艺升版时关联文档进入"待确认"状态（`PENDING_REBIND`），由文档 owner 人工确认是否发新版本或继承绑定--宁可让人确认，不可自动假设 SOP 不变（与 MES 防错理念一致）。
- **设备绑定型 / 通用知识型**不绑 `route_version`，按自身文档版本 + `asset_id` / 标签管理，不受工艺变更事件影响。

### 4.5 与 14 个限界上下文的关系

文档型 RAG 不投影 14 个上下文的聚合根（那是追溯型 RAG 的活），而是通过**文档绑定关系**与领域版本对齐：

| 文档类别 | 绑定的领域对象 | 对齐的上下文 | 驱动重索引的事件 |
|---------|--------------|-------------|----------------|
| 工艺绑定型 | `route_id` + `route_version` | 工艺管理 | `ProcessRouteActivated` / `ProcessRouteDeprecated` |
| 工艺绑定型（检验标准） | `rule_id` + `rule_version` | 质量 | `QualityGateRuleActivated` |
| 工艺绑定型（BOM 相关） | `bom_id` + `bom_version` | 物料 | `BomActivated` |
| 设备绑定型 | `asset_id` / 设备型号 | 设备工装台账 | `AssetCommissioned`（型号变更时）/ 一般不随事件变 |
| 通用知识型 | 无领域版本绑定 | - | 无（按自身版本管理） |

- 文档型 RAG 是 MES 领域的**只读消费者**：只订阅上述事件做重索引 / 重新绑定，不回写任何上下文。
- 与追溯型 RAG 的边界：追溯型投影聚合根实例（`WipUnit` / `CheckpointRecord` ...）成图节点；文档型不投影聚合根，只把"文档版本绑定哪个领域版本"作为元数据过滤维度。两者通过 `rag.reindex.request` 事件协同（§5.4）。

---

## 5. 文档摄入与索引构建

### 5.1 摄入管线

```text
原始文档(PDF/Word/HTML/MD) ──▶ MinIO(file_ref)
        │
        ▼
DocumentIngestionService
  ├─ 1. 加载：从 MinIO 拉原始文件
  ├─ 2. 解析：unstructured / pypdf / python-docx -> 结构化文本(带标题层级)
  ├─ 3. 切分：LlamaIndex NodeParser（按文档类型选策略，§5.2）
  ├─ 4. 向量化：bge-m3 批量 embed 每个 chunk（1024 维）
  ├─ 5. 持久化：knowledge_document / document_version / document_chunk(+embedding) 同 PG 事务
  └─ 6. 发事件：DocumentVersionPublished（供检索侧 / 审计消费）
```

- **解析保留结构**：PDF / Word 的标题层级、表格、步骤编号是切分的关键依据，解析阶段要保留（`unstructured` 库对结构化解析支持好）。
- **批量向量化**：bge-m3 支持批量 embed，按 batch（如 32）批量推理，降低单文档摄入延迟。
- **同事务持久化**：文档元数据 + 分块 + 向量同 PG 事务提交，保证"要么都入库要么都不入"--不会出现元数据已写但向量缺失的半成品。
- **幂等摄入**：同一文件（按内容 hash）重复摄入不产生重复 chunk--`file_content_hash` 唯一约束兜底（§5.5）。

### 5.2 文档切分策略

切分质量直接决定召回质量，按文档类型选不同 `NodeParser`：

| 文档类型 | 切分策略 | 理由 |
|---------|---------|------|
| SOP / 作业指导书 | **按工序步骤切**（`RouteStep` 对齐）+ 步骤内按段落 | 检索"某站怎么操作"能命中整步骤，不切断步骤上下文 |
| 设备维修手册 | **按故障代码 / 章节切**（标题层级感知） | 检索"E027 怎么修"能命中完整故障代码章节 |
| 检验标准 | **按检验项切**（参数表保留为表格） | 检索"某项标准值"能命中参数表行 |
| IPC 标准 | **按章节 + 固定长度兜底**（语义 + 长度混合） | 标准文档结构规整，章节切分 + 长度上限兼顾 |
| 培训资料 | **语义切分**（SentenceSplitter） | 弱结构，按语义边界切 |

- **chunk 大小**：SOP / 手册建议 256–512 token（中文约 200–400 字），重叠 10%--太大切不准、太小丢上下文。🔴 具体 chunk size / overlap 需按真实文档评测调优，交还用户在评测阶段确认。
- **`section_type` 标注**：切分时识别章节类型（步骤 / 故障代码 / 参数 / 注意事项），作为元数据存入 chunk，检索时可按 `section_type` 过滤（如"只查故障代码章节"）。
- **`locator` 定位**：每个 chunk 带页码 / 段落偏移 / 标题路径，答案引用能定位到原文档具体位置，可点开回溯。

### 5.3 向量化与元数据

每个 chunk 的向量 + 元数据结构（PGVector 表）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `chunk_id` | uuid | 分块唯一标识 |
| `version_id` | uuid | 归属文档版本 |
| `document_id` | uuid | 归属文档 |
| `doc_type` | varchar | 文档类型 |
| `category` | varchar | 类别（工艺/设备/通用） |
| `state` | varchar | 版本状态（PUBLISHED/DEPRECATED/...） |
| `tenant_scope` | varchar | 租户范围 |
| `text` | text | 分块文本 |
| `embedding` | vector(1024) | bge-m3 向量 |
| `section_type` | varchar | 章节类型 |
| `locator` | jsonb | 定位（页码/偏移/标题路径） |
| `binding_route_version` | varchar | 绑定的工艺版本（工艺绑定型，冗余以便过滤） |
| `binding_route_id` | varchar | 绑定的工艺路线 |
| `binding_asset_id` | varchar | 绑定的设备（设备绑定型） |
| `file_content_hash` | varchar | 源文件内容 hash（幂等） |

- **绑定字段冗余到 chunk**：`binding_route_version` 等从 `DocumentBinding` 冗余到 chunk 表，避免检索时 join--向量检索的 `WHERE` 直接在 chunk 表前置过滤，性能最优。
- **HNSW 索引**：`embedding` 列建 HNSW 索引（`vector_cosine_ops`），百万级 chunk 检索延迟数十毫秒。
- **过滤字段建 B-tree 索引**：`state` / `tenant_scope` / `doc_type` / `binding_route_version` / `binding_route_id` / `binding_asset_id` 建普通索引，配合向量检索的 pre-filter。

### 5.4 版本治理与重索引（事件驱动）

工艺版本变更是文档型 RAG 最危险的环节。本文从事件驱动 + 状态流转 + 检索过滤三层面兜住：

**A. 重索引事件订阅**

| 领域事件 | 主题 | 重索引动作 |
|---------|------|-----------|
| `ProcessRouteActivated` | `process.route.lifecycle` | ① 旧版本关联文档若仅绑此 route 则进入 `PENDING_REBIND`；② 提示文档 owner 确认 SOP 是否需发新版本（§4.4 方案 B）；③ 新版本若已有继承绑定，标记可检索 |
| `ProcessRouteDeprecated` | `process.route.lifecycle` | 关联文档版本 `state` -> `DEPRECATED`（不删），历史引用仍可追溯 |
| `BomActivated` | `material.bom.lifecycle` | 同上，BOM 相关文档重绑定 |
| `QualityGateRuleActivated` | `quality.gate.lifecycle` | 检验标准文档重绑定 |
| `rag.reindex.request` | `rag.reindex.*`（内部） | 追溯型 RAG 发布的重索引请求（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §5.4），按 `route_id` + `route_version` 重索引关联文档 |
| `DocumentVersionPublished` | `rag.doc.lifecycle`（内部） | 文档版本发布，刷新检索可见性 |

**B. 状态流转闸**

- `ProcessRouteActivated` 触发后，关联文档**不自动 PUBLISHED**（🔴 §4.3：文档有独立审核流），而是进入 `PENDING_REBIND`，由文档 owner 人工确认：发新版本（内容变）或继承绑定（内容不变）。
- 旧 `route_version` 关联文档若仅此绑定，`ProcessRouteDeprecated` 后 `state` -> `DEPRECATED`，**不删除**--历史答案引用仍可点开（对齐追溯型"版本即节点不删"）。

**C. 检索过滤闸**

- 检索向量时 `WHERE state='PUBLISHED'` + 版本过滤（§6.2），`DEPRECATED` / `PENDING_REBIND` 文档天然不召回。
- 查历史单件 SOP 时带 `binding_route_version` = 当时版本（与追溯型 `SNAPSHOT_OF_ROUTE` 同构），不取"当前生效版"。

> **版本一致性不是文档型 RAG 自己保证的，是从领域模型兜上来的**--工艺版本有生命周期、变更发 `ProcessRouteActivated`、文档绑定对齐 `route_version`。RAG 只是严格遵循这套契约，不另搞一套版本管理。这与追溯型 RAG 的版本契约（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §5.4）是同一套，两个投影面。

### 5.5 幂等与去重

事件经各上下文 Transactional Outbox **至少一次**投递（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §1），RAG 侧必须幂等消费，否则重复投递会产生重复重索引 / 重复 chunk。

- **重索引幂等**：`event_id + consumer_group` 幂等表（与追溯型同构），重复投递的重索引事件被挡住。
- **摄入幂等**：`file_content_hash` 唯一约束--同一文件重复摄入不产生重复 chunk。
- **chunk 幂等**：`version_id + ordinal` 唯一约束，重复写入被约束挡住。
- **位点上移**：幂等记录与位点更新同 PG 事务，保证"已处理 ⇒ 已 ack"。

---

## 6. 检索与生成

### 6.1 检索入口

检索从用户问题出发，可选带领域上下文（`route_version` / `asset_id` / `station_id`）做版本 / 设备过滤：

```text
用户问题："SPI 报 solder_insufficient 怎么处置？" + 上下文：{station_id: SPI-3, route_version: v2}
        │
        ▼
DocumentRetrievalService
  ├─ 1. query 改写（可选）：补全术语 / 扩展同义词
  ├─ 2. 向量化：bge-m3 embed query
  ├─ 3. 向量检索 + 元数据过滤（PGVector pre-filter，§6.2）
  ├─ 4. Rerank：bge-reranker 精排 top-k -> top-n
  ├─ 5. LLM 综合：question + chunks -> 答案 + 引用
  └─ 6. 置信度判断：低置信度转人工
```

- **领域上下文注入**：调用方（工位屏幕 / 工程师 UI / L1 Agent）可带 `route_version` / `asset_id` 等上下文，检索时前置过滤--如过点拦截辅助场景带当前 `route_version`，保证召回的 SOP 与生产执行侧工艺版本一致。
- **query 改写可选**：术语补全（"SPI" -> "Solder Paste Inspection"）提升召回，但增加延迟；MVP 可先不做，按评测决定。

### 6.2 向量检索 + 元数据过滤（PGVector pre-filter）

```sql
-- 参数：$query_vec(bge-m3 1024维), $tenant_scopes, $route_version, $asset_id, $top_k
SELECT
    c.chunk_id, c.text, c.section_type, c.locator,
    c.document_id, c.version_id, c.binding_route_version,
    dv.version_no, kd.title, kd.doc_type,
    1 - (c.embedding <=> $query_vec) AS similarity
FROM document_chunk c
JOIN document_version dv ON dv.version_id = c.version_id
JOIN knowledge_document kd ON kd.document_id = c.document_id
WHERE c.state = 'PUBLISHED'                              -- ① 只检索已发布
  AND c.tenant_scope = ANY($tenant_scopes)               -- ② 租户前置过滤
  AND ($route_version IS NULL                            -- ③ 版本过滤：带则精确，不带则按当前生效
       OR c.binding_route_version = $route_version)
  AND ($asset_id IS NULL                                 -- ④ 设备过滤（设备绑定型）
       OR c.binding_asset_id = $asset_id)
ORDER BY c.embedding <=> $query_vec                      -- ⑤ cosine 近邻
LIMIT $top_k;
```

- **`state='PUBLISHED'` 前置**：`DEPRECATED` / `PENDING_REBIND` / `ARCHIVED` 文档根本不进候选，从结构上杜绝失效文档（§5.4 检索闸）。
- **版本过滤**：带 `route_version` 则精确定位当时版本 SOP（历史回溯）；不带则按当前生效（`PUBLISHED` 即当前生效，因同类绑定唯一 PUBLISHED）。
- **租户前置过滤**：`tenant_scope = ANY($tenant_scopes)` 在向量近邻前裁剪，权限不达标看不到 chunk，不是答完再裁剪（§1.2）。
- **PGVector pre-filter**：先 SQL `WHERE` 过滤再 `<=>` 近邻，版本 / 权限 / 设备组合过滤全在向量检索前完成--这是选 PGVector 的核心收益（§2.3）。

### 6.3 Rerank

向量召回 top-k（如 20）后，用 bge-reranker-v2-m3 精排到 top-n（如 5）：

- 向量检索重召回、轻精度（cosine 近邻快但粗）；rerank 重精度（cross-encoder 逐对打分准但慢）。
- 两段式是文档型 RAG 标配，bge-reranker 与 bge-m3 同系列，中英文兼顾，可本地化。
- 🔴 top-k / top-n 参数需按真实查询评测调优，交还用户在评测阶段确认。

### 6.4 LLM 综合与引用

`DocumentRetrievalService` 把 question + rerank 后的 chunks 交给 LLM，产出带引用的答案：

```python
class DocCitation(BaseModel):
    chunk_id: str
    document_id: str
    version_no: str
    title: str
    locator: dict            # 页码/偏移/标题路径，可点开回溯
    quoted_text: str         # 引用的原文片段

class DocAnswer(BaseModel):
    answer: str
    citations: list[DocCitation]     # 强制引用，无引用的答案判失败重试
    confidence: float                # 0.0 ~ 1.0
    route_version_filter: str | None # 本次检索的版本过滤（溯源用）
    disclaimer: str = "本答案来自文档型 RAG，处置需按现行 SOP 确认"
    needs_human_review: bool = False
```

- **强制引用 `chunk_id`**：每个答案必须引用至少一个 chunk，无引用的答案判失败重试--与追溯型 RAG"证据强制引用 node_id"同思路（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §6.4），杜绝 LLM 编造。
- **置信度阈值**：`confidence < 0.6` 或无引用 -> `needs_human_review=True`，不展示给操作工，只推工程师；与 MES 防错理念一致，宁可拦下让人判。
- **系统提示词约束**：明确告诉模型"只能基于提供的 chunks 回答，不得编造未提供的文档内容；若 chunks 无关则回答'未找到相关文档'而非编造；输出严格遵循 DocAnswer 结构"。

### 6.5 检索结果结构化

检索的 chunks 与答案都是**结构化 DTO**（Pydantic 强约束），既给 LLM 综合做上下文，也给 UI 直接渲染引用卡片：

- 每个 citation 带 `locator`（页码 / 偏移 / 标题路径），工程师点击能跳到原文档（MinIO 拉取 + 定位）具体位置。
- `route_version_filter` 记录本次检索的版本过滤，答案可溯源"是基于哪个工艺版本的 SOP 答的"。

### 6.6 与追溯型 RAG、L1 Agent 的协同

- **与追溯型 RAG 协同**：追溯型 `TraceAnswer.suggested_action` 可调本文 `search_docs(query, route_version_filter)` 补 SOP 片段（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §6.5）--追溯型给"哪批锡膏、哪台设备"（事实链），本文给"SPI 报警怎么处置"（处置知识），两者版本过滤都对齐 `ProcessRouteActivated`。
- **与 L1 Agent 协同**：L1 的 `search_docs` 工具封装本文 `POST /rag/docs/query`（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)）；L1 在追溯型图检索基础上，按需要调本文补充处置知识。
- **不互相替代**：追溯型答"是什么、为什么"（事实链），本文答"怎么办"（处置知识）。简单处置用本文够了，复杂根因诊断用追溯型 + L1。

---

## 7. 实现方案（概要）

本文是设计层全景。**实现层落地**见 [文档型 RAG-实现方案.md](./文档型 RAG-实现方案.md)，聚焦 MVP 一刀（SOP + 设备维修手册 + 检验标准，对齐 [RAG服务引入路线.md](../RAG服务引入路线.md) §3 起步建议），补全：

- 依赖清单（`pyproject.toml`）与 PGVector DDL；
- 摄入管线代码（解析 / 切分 / 向量化 / 持久化）；
- 重索引消费者代码（订阅 `ProcessRouteActivated` 等）；
- 检索服务代码（向量检索 + 过滤 + rerank + LLM 综合）；
- ACL 只读 REST 契约（查文档绑定关系）；
- Docker 部署与测试策略。

其余文档类型（IPC 标准 / 8D / 培训）按 §4.1 相同范式扩展，MVP 不展开。

---

## 8. 可观测性与兜底

### 8.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `rag_doc_ingest_total` | 文档摄入数（按 doc_type label） |
| `rag_doc_ingest_latency_seconds` | 摄入延迟（解析+切分+向量化，Histogram） |
| `rag_doc_ingest_error_total` | 摄入失败次数 |
| `rag_doc_chunk_total` | chunk 总数（按 doc_type / state label） |
| `rag_doc_reindex_total` | 重索引事件数（按 event_type label） |
| `rag_doc_reindex_lag_seconds` | 重索引滞后（事件 `occurred_at` 与处理完成时间差） |
| `rag_doc_retrieval_total` | 检索次数 |
| `rag_doc_retrieval_latency_seconds` | 检索延迟（向量+rerank+LLM，Histogram） |
| `rag_doc_retrieval_cache_hit_total` | 检索缓存命中 |
| `rag_doc_low_confidence_total` | 置信度 <0.6 转人工次数 |
| `rag_doc_no_citation_total` | 无引用判失败重试次数 |
| `rag_doc_acl_fallback_total` | 降级查询各上下文 REST 次数 |
| `rag_doc_deprecated_leak_total` | 检索结果误含 DEPRECATED 文档（应为 0，告警） |

### 8.2 trace 串联

- 每次检索一个 `trace_id`，OpenTelemetry 在 `VectorRetriever`、rerank、LLM 调用都注入 span，透传到下游 Java 服务（`traceparent` header）。
- `DocAnswer.citations` 的 `chunk_id` + `locator` 让工程师从答案回溯到原文档具体位置--证据链可点开回溯。

### 8.3 兜底

- **重索引滞后兜底**：`rag_doc_reindex_lag_seconds` 超阈值 -> 告警 + 检索置信度降权，提示"文档可能未与最新工艺同步"。
- **置信度兜底**：`confidence < 0.6` 或无引用 -> `needs_human_review`，不展示给操作工，只推工程师；与 MES 防错理念一致，宁可拦下让人判。
- **LLM 输出兜底**：`DocAnswer` 经 Pydantic 校验，不符合 schema（无引用 / 字段缺失）判失败重试；重试仍失败转人工，不硬答。
- **DEPRECATED 泄漏兜底**：`rag_doc_deprecated_leak_total` 应为 0--检索 SQL 强制 `state='PUBLISHED'`，若结果误含 DEPRECATED 文档即告警，说明过滤逻辑有漏洞。
- **向量库故障兜底**：PGVector 不可用时，`/rag/docs/query` 返回 503 + 降级提示，不阻塞 MES 生产；文档库可从 MinIO 原始文件 + 事件回放重建。

---

## 9. 实现步骤

### 阶段一：骨架与最小摄入检索（2 周）

1. 搭 `rag-doc-service` 骨架（FastAPI + uvicorn），对齐实现方案包结构。
2. 接 PGVector（PostgreSQL + pgvector），建文档元数据 / chunk / 向量表与 HNSW 索引。
3. 实现摄入管线（解析 -> 切分 -> bge-m3 向量化 -> 持久化），跑通一份 SOP 摄入。
4. 实现向量检索 + 元数据过滤（§6.2 SQL），带 `state` + `tenant_scope` + `route_version` 前置过滤。
5. 实现 LLM 综合（§6.4），`DocAnswer` Pydantic 强约束 + 强制引用 + 置信度阈值。

### 阶段二：版本治理与重索引（2 周）

6. 实现文档版本状态机（DRAFT/SUBMITTED/PUBLISHED/DEPRECATED/ARCHIVED，§4.3）。
7. 实现文档与领域版本绑定（§4.4 方案 B，含 `inherited` 继承绑定）。
8. 实现重索引消费者：订阅 `ProcessRouteActivated` / `ProcessRouteDeprecated` / `BomActivated` / `QualityGateRuleActivated`，触发 `PENDING_REBIND` / `DEPRECATED` 状态流转（§5.4）。
9. 接收追溯型 `rag.reindex.request` 事件，按 `route_id` + `route_version` 重索引关联文档。
10. 实现幂等表 + 位点表 + 手动 ack（§5.5）。

### 阶段三：Rerank、权限加固与可观测（1–2 周）

11. 接 bge-reranker-v2-m3 精排（§6.3），评测调优 top-k / top-n。
12. 租户 / 权限过滤在 SQL 前置，验证 `tenant_scope` 不达标看不到 chunk。
13. 接 OpenTelemetry + prometheus 指标（§8.1），`rag_doc_deprecated_leak_total` 告警兜底。
14. 检索结果缓存（redis）按 query + 版本 + 租户去重。

### 阶段四：ACL 降级、评测与协同对接（1–2 周）

15. 接 ACL 降级查询各上下文只读 REST（查文档绑定关系，§7 实现方案）。
16. 沉淀评测集（典型 SOP / 手册问答 + 预期引用），回归模型 / 提示词 / 切分变更。
17. 对接追溯型 RAG：`TraceAnswer.suggested_action` 调 `search_docs(query, route_version_filter)`。
18. 对接 L1 Agent：`search_docs` 工具封装 `/rag/docs/query`。
19. 灰度一条产线（设备维修手册 + SOP + 检验标准），收集工程师 / 操作工反馈。

### 阶段五：扩展（按需）

20. 扩展 IPC 标准 / 培训资料（通用知识型，按自身版本管理）。
21. 🔴 8D 报告案例型检索（若纳入，需独立切分 / 提示词策略）。
22. 🔴 工艺升版时 SOP 内容变更自动比对（若做自动判断，替代人工 `PENDING_REBIND` 确认）。

---

## 10. 约束落地检查清单

- [ ] 文档元数据 / chunk / 向量同库存于 PGVector，版本 / 权限 / 生效状态组合过滤走 SQL `WHERE` 前置（§2.3）。
- [ ] 检索 SQL 强制 `state='PUBLISHED'`，`DEPRECATED` / `PENDING_REBIND` / `ARCHIVED` 不进候选；`rag_doc_deprecated_leak_total` 应为 0。
- [ ] 文档版本绑定 `route_version` / `bom_version` / `rule_version`；检索带版本过滤（带则精确历史版本，不带则当前生效）。
- [ ] 订阅 `ProcessRouteActivated` / `ProcessRouteDeprecated` / `BomActivated` / `QualityGateRuleActivated` 触发重索引 / 状态流转（§5.4）。
- [ ] 接收追溯型 `rag.reindex.request` 事件，与追溯型重索引对齐。
- [ ] 文档版本状态机对齐工艺版本生命周期；新版本 PUBLISHED 时旧版本 DEPRECATED 不删（§4.3）。
- [ ] 工艺升版关联文档进入 `PENDING_REBIND` 人工确认，不自动 PUBLISHED（🔴 §4.3 / §4.4）。
- [ ] 租户 `tenant_scope` 在向量检索 SQL 前置过滤，权限不达标看不到 chunk。
- [ ] `event_id + consumer_group` 幂等表 + `file_content_hash` / `version_id+ordinal` 唯一约束，重复投递 / 重复摄入不产生重复 chunk。
- [ ] 消费者位点落 PG，重启从断点续跑，处理事务成功后才 ack offset。
- [ ] RAG 服务不进过点主事务，文档摄入 / 重索引秒级最终一致，过点 P99 ≤200ms 不受影响。
- [ ] 检索结果 `DocAnswer` 结构化，citations 强制引用 `chunk_id` + `locator`，无引用判失败重试。
- [ ] LLM 输出经 Pydantic `DocAnswer` 校验，失败重试。
- [ ] `confidence < 0.6` 或无引用 -> `needs_human_review`，不展示给操作工。
- [ ] 向量库故障返回 503 不阻塞 MES 生产；文档库可从 MinIO 原始文件 + 事件回放重建。
- [ ] 所有答案带 disclaimer：来自文档型 RAG，处置需按现行 SOP 确认。

---

## 11. 面试防守 Q&A

**Q：文档型 RAG 和通用 RAG 有什么本质区别？**
A：不是检索模型差异，是**版本治理**。通用 RAG 检索文档不分版本；MES 的工艺路线有版本生命周期（[领域总览.md](../../领域模型/领域总览.md) §5.1），SOP / 作业指导书绑定 `route_version`。如果 RAG 检索到一条已失效工艺对应的 SOP 给操作工，会直接导致批量不良。所以我的文档型 RAG 做了三件事：① 文档版本绑定领域版本、有状态机（PUBLISHED/DEPRECATED）；② 订阅 `ProcessRouteActivated` 等事件触发重索引 / 重新绑定；③ 检索时 SQL 强制 `state='PUBLISHED'` + `route_version` 过滤前置。版本一致性不是 RAG 保证的，是从领域模型兜上来的（[RAG服务引入路线.md](../RAG服务引入路线.md) §5 Q&A）。

**Q：为什么选 PGVector 而不是 Milvus / Qdrant？**
A：文档型 RAG 的核心难点不是召回率，是**版本 + 权限 + 生效状态的组合过滤**。PGVector 让文档元数据和向量同库存，组合过滤走完整 SQL `WHERE`（`state='PUBLISHED' AND tenant_scope IN (...) AND binding_route_version=$rv`）前置，pre-filter 后再向量近邻。Milvus / Qdrant 的标量过滤表达力弱于 SQL，复杂组合过滤易丢召回，且元数据与向量常分库、跨库一致性难保证。文档库规模（数百万 chunk）远未到 PGVector 瓶颈，HNSW 索引检索延迟数十毫秒，满足文档型"容忍秒级"的 SLA。真到专用向量库甜区再换，LlamaIndex 抽象兜住切换成本（§2.3）。

**Q：工艺升版了，SOP 怎么跟着变？**
A：订阅 `ProcessRouteActivated` 事件，关联文档进入 `PENDING_REBIND` 待确认状态，由文档 owner 人工判断：SOP 内容若需变则发新版本绑定新 `route_version`；若内容不变则继承绑定（`inherited=true`）。**不自动 PUBLISHED**--因为文档有独立审核流，且"SOP 是否需变"不能靠机器假设，宁可让人确认（§4.4）。旧版本文档 DEPRECATED 不删，历史答案引用仍可点开回溯。这是和通用 RAG 最大的区别：通用 RAG 覆盖更新就丢了历史，本文版本即不可变快照。

**Q：会不会拖慢过点？**
A：不会进过点主事务。过点 P99 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1）是硬约束，文档摄入 / 重索引是异步消费事件，与过点判定完全解耦。文档检索容忍秒级延迟（[RAG服务引入路线.md](../RAG服务引入路线.md) §4）--它是事后 / 旁边问答工具，不是实时过点判定。

**Q：文档型 RAG 和追溯型 RAG 是不是重复了？**
A：不重复，是互补分工（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.1/§2.2）。追溯型是 GraphRAG + 领域事件流，答"这条单件 5M1E 全貌"（哪批锡膏、哪台设备），处理结构化关系；文档型是向量检索，答"SPI 报警怎么处置"（处置知识），处理非结构化文档。两者共享同一套版本契约--都由 `ProcessRouteActivated` 驱动，追溯型把版本做成图节点，文档型把版本做成文档元数据过滤维度。追溯型的 `suggested_action` 可调文档型补 SOP 片段，协同闭环（§6.6）。

**Q：操作工看不懂 LLM 长答案怎么办？**
A：工位场景要"一句话原因 + 一个动作 + 一个引用"，不是长文。系统提示词约束 LLM 输出精简结构化答案 + 强制引用 chunk；低置信度不硬答，转人工 / 转规则引擎--和 MES 防错理念一致，宁可拦下让人判（§6.4）。

**Q：不同车间能看的文档不一样怎么管？**
A：文档元数据带 `tenant_scope`（workshop/line），向量检索 SQL `WHERE tenant_scope = ANY($tenant_scopes)` 前置过滤，权限不达标根本不进候选，不是答完再裁剪（§6.2）。本 MES 的限界上下文边界本身就是天然的权限切分面，文档按车间 / 产线分域管理。

**Q：LLM 编造文档里没有的内容怎么办？**
A：两道防线。一是系统提示词约束"只能基于提供的 chunks 回答，无相关文档则回答'未找到'"；二是 `DocAnswer` 强制引用 `chunk_id`，无引用的答案 Pydantic 校验失败、判失败重试（§6.4）。每个引用带 `locator`（页码/偏移），工程师能点开原文核对。低置信度转人工，不硬答。

**Q：文档库错了或漏了怎么办？**
A：文档库归 RAG 自有，错了不影响 MES 生产--事实源是 MinIO 原始文件 + 领域版本，向量库崩溃返回 503 不阻塞过点。重建靠 MinIO 原始文件 + 事件回放，无需 MES 配合。所有答案带引用 + 置信度，低置信度转人工，与 MES 防错理念一致。

**Q：上线了吗？**
A：这是设计阶段的引入规划，不是已落地。重点是版本治理对齐工艺生命周期、PGVector 选型用 SQL 前置过滤兜版本+权限、重索引由领域事件驱动这三条架构判断。落地按"先 B 后 A"推进（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）--文档型 2–4 周可出 demo，先验证车间可用性。诚实 + 体现架构判断力，比硬吹"已上线文档 RAG"得分高。

---

## 12. 一句话定位

"文档型 RAG 把车间的 SOP / 手册 / 标准做成向量知识库--向量是主体，但版本治理才是灵魂：文档版本绑定 `route_version`、状态机对齐工艺生命周期、订阅 `ProcessRouteActivated` 驱动重索引、检索 SQL 强制 `state='PUBLISHED'` + 版本前置过滤，从结构上杜绝答出已失效 SOP 导致批量不良。选 PGVector 让版本/权限/生效状态组合过滤走 SQL `WHERE` 前置，与追溯型 RAG 共享同一套版本契约、互补分工--追溯型答'是什么、为什么'，文档型答'怎么办'，全程只读 MES、不进过点主事务，低置信度转人工，让 RAG 从旁边问答变成能进车间的处置知识副驾。"
