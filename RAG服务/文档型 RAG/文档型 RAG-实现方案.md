# 文档型 RAG 实现方案（Python 技术栈：SOP + 手册 + 检验标准 MVP）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.2 路线 B（文档型 RAG）的**实现层落地**，与 [文档型 RAG-详细设计.md](./文档型 RAG-详细设计.md) 的关系：
> - **详细设计**是全文档类型的**设计层**（广）--文档领域建模、版本治理设计、检索生成流程的全景；
> - **本文**是 MVP 一刀（**SOP + 设备维修手册 + 检验标准**，对齐 [RAG服务引入路线.md](../RAG服务引入路线.md) §3 起步建议）的**实现层**（深）--把详细设计的骨架补全到可落地的 MVP，新增**依赖清单、PGVector DDL、摄入管线代码、重索引消费者、检索 SQL、ACL 只读 REST 契约、Docker 部署、测试策略**等实现层内容。
> 其余文档类型（IPC 标准 / 8D / 培训）按 §11 相同范式扩展，MVP 不展开。
>
> **技术栈**：Python（FastAPI + **PGVector** + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 Kafka 只读事件 + REST 只读查询解耦，互不侵入。
> **口径纪律**：文档型 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了文档 RAG"。MES 领域对错误答案零容忍（错给一条已失效 SOP 会直接导致批量不良），所以本文强调**向量是主体、版本治理是灵魂 + 重索引由领域事件驱动 + 可观测兜底**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 目标（MVP 一刀）

把车间 **SOP / 设备维修手册 / 检验标准** 三类文档做成可被 LLM 检索 + 综合的向量知识库，让"SPI 报警怎么处置""贴片机 E027 怎么修""首件检验流程是什么"这类问题一次向量检索 + LLM 综合即给答案 + 可点开引用，且**检索结果与生产执行侧工艺版本一致**。

**MVP 范围**：聚焦三类文档，跑通"摄入 -> 版本治理 -> 检索 -> 答案"闭环--

| 文档类型 | 类别 | MVP 绑定 | 版本治理 |
|---------|------|---------|---------|
| **SOP / 作业指导书** | 工艺绑定型 | `route_id` + `route_version` | 订阅 `ProcessRouteActivated` 重索引 |
| **设备维修手册** | 设备绑定型 | `asset_id` / 设备型号 | 按文档自身版本管理 |
| **检验标准** | 工艺绑定型 | `rule_id` + `rule_version`（MVP 先按 `route_version` 归属） | 订阅 `ProcessRouteActivated` 重索引 |

> 🔴 **检验标准的绑定维度**：检验标准理论上绑定 `QualityGateRuleActivated` 的 `rule_version`，但 MVP 阶段检验标准常随工艺路线版本一起发布，先按 `route_version` 归属简化。是否需要独立 `rule_version` 绑定 + 订阅 `quality.gate.lifecycle`，待 MVP 评测后确认。

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地（MVP 具体动作） |
|------|------|----------------------|
| **只读 MES** | 文档库归 RAG 自有；对 MES 只读 | 文档元数据 / 向量存 PGVector（RAG 自有 PG）；订阅 `process.*` 只读事件；降级查询只读 REST；`ReadOnlyIngestionGate` 启动断言禁止写 MES（§9.7） |
| **不进过点主事务** | 摄入 / 重索引异步消费事件 | 过点 P99 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1）不受影响；文档检索容忍秒级 |
| **版本一致性** | 检索 SOP 必须带版本过滤 | 文档版本绑定 `route_version`；订阅 `ProcessRouteActivated` 触发重索引；检索 SQL 强制 `state='PUBLISHED'` + `route_version` 过滤（§6.2） |
| **权限隔离** | 检索前按车间 / 产线过滤 | chunk 表冗余 `tenant_scope`，SQL `WHERE` 前置过滤 |
| **可观测兜底** | 答案带引用 + 置信度，低置信度转人工 | `DocAnswer` 强制引用 `chunk_id` + `locator`；`confidence < 0.6` 转人工 |
| **DEPRECATED 不泄漏** | 失效文档不得召回 | 检索 SQL 强制 `state='PUBLISHED'`；`rag_doc_deprecated_leak_total` 应为 0，告警兜底（§9.7） |

### 1.3 与详细设计、追溯型 RAG、L1 Agent 的关系

- **与详细设计**：详细设计给全文档类型全景建模与版本治理设计；本文把其中三类文档的摄入管线、重索引消费者、检索 SQL、ACL 契约补全到可落地代码，并新增实现层内容（依赖、DDL、Docker、测试）。两者互补--详细设计是"地图"，本文是"起步城区施工图"。
- **与追溯型 RAG**（[追溯型 RAG-实现方案.md](../追溯型 RAG/追溯型 RAG-实现方案.md)）：共享同一套版本契约（`ProcessRouteActivated` 驱动）。追溯型把版本做成 Neo4j 图节点 + `SNAPSHOT_OF_ROUTE` 边；本文把版本做成 PGVector 文档元数据过滤维度。追溯型 §5.4 发布内部 `rag.reindex.request` 事件，本文接收并处理（§5.4）。
- **与 L1 诊断型 Agent**（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)）：L1 的 `search_docs` 工具封装本文 `POST /rag/docs/query`（§9.8）。

### 1.4 与 Java 技术栈的关系

- RAG 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只订阅 Kafka 只读事件、调只读 REST 查文档绑定关系。
- 跨语言的物理边界反而是好事：RAG 服务无法共享 Java 事务 / 内存，天然强制只读、不进过点主事务、不旁路应用服务写路径。
- 复用 [实现说明](../../实现说明/) 既有的 Kafka topic、领域事件 envelope（`event_id` / `event_type` / `event_version` / `occurred_at` / `source_service` / `trace_id` / `partition_key`，见 [消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §4.3）、消费侧幂等模式（§6 同事务幂等 + 手动 ack），不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 版本 | 选型理由 |
|------|------|------|---------|
| 语言 | Python | 3.11+ | 类型提示 + Pydantic 校验，与追溯型 / L1 同栈复用 LLM 抽象 |
| Web 框架 | **FastAPI** | 0.110+ | 异步、原生 OpenAPI，与追溯型一致 |
| 向量存储 | **PGVector** | PostgreSQL 16 + pgvector 0.7+ | 文档元数据与向量同库存，版本+权限组合过滤走 SQL `WHERE` 前置（§2.3） |
| PG 驱动 | **asyncpg** + SQLAlchemy 2.0 (async) | - | 异步 PG 访问，`pgvector` Python 包对接向量类型 |
| 检索编排 | **LlamaIndex** `VectorStoreIndex` | 0.10+ | 摄入 / 切分 / 向量检索 / Rerank 上层抽象 |
| 文档解析 | **unstructured** + pypdf + python-docx | - | PDF / Word / HTML 结构化解析，保留标题层级 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 0.2+ | 模型可插拔，与追溯型 / L1 一致 |
| Embedding | **bge-m3** | 1.0+ | 1024 维，多语种可本地化，与追溯型同模型 |
| Rerank | **bge-reranker-v2-m3** | - | cross-encoder 精排，中英文兼顾 |
| 数据校验 | **Pydantic** | v2 | 请求 / 文档视图 / 答案 DTO 的 schema 即类型 |
| HTTP 客户端 | **httpx** | 0.27+（异步） | 降级查询各上下文只读 REST |
| 消息 | **aiokafka** | 0.10+ | 订阅领域事件触发重索引 |
| 对象存储 | **MinIO**（`minio` Python SDK） | - | 原始文档文件存储 |
| 缓存 | **redis-py (async)** | 5.0+ | 检索结果短期缓存 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | - | trace 串联、指标告警 |
| 配置 | pydantic-settings | 2.0+ | 环境变量统一管理 |
| 部署 | 独立微服务 `rag-doc-service` | - | K8s 部署；MVP 可 docker-compose 本地起 |

### 2.2 为什么是纯向量检索（而非 GraphRAG）

- 三类文档的答案集中在少数段落（"E027 怎么修"在手册某章、"SPI 报警处置"在 SOP 某步骤），是"局部、事实性"查询（[基础问题.md](../问题归纳/基础问题.md) §二），向量相似度足以召回。
- 文档之间是弱关联（SOP 和手册之间无 `source_work_order_id` 这种显式引用），不需要图遍历。这与追溯型 RAG 形成分工（[追溯型 RAG-详细设计.md](../追溯型 RAG/追溯型 RAG-详细设计.md) §2.2）。

### 2.3 为什么选 PGVector（而非专用向量库）

文档型 RAG 核心难点是**版本 + 权限 + 生效状态组合过滤**，PGVector 的 SQL `WHERE` 前置过滤正是为它量身定做：

```sql
WHERE state='PUBLISHED' AND tenant_scope = ANY($scopes)
  AND ($route_version IS NULL OR binding_route_version = $route_version)
  AND ($asset_id IS NULL OR binding_asset_id = $asset_id)
ORDER BY embedding <=> $query_vec   -- 先 SQL 过滤再向量近邻
```

- 元数据与向量同库同事务：文档状态流转与向量可见性原子更新，不会"元数据已 DEPRECATED 但向量仍可召回"。
- 文档库规模（数百万 chunk）远未到瓶颈，HNSW 索引检索延迟数十毫秒。
- **比追溯型更省运维**：追溯型是 Neo4j + MySQL 双库（图 + 元数据），文档型全程一个 PG 实例（向量 + 元数据 + 幂等 + 位点同库），MVP 部署成本低。
- 若未来换 Milvus / Qdrant，版本过滤逻辑要从 SQL 改写为各库标量过滤 API--需提前在 `VectorRetriever` 抽象（§9.5）。

### 2.4 部署形态（车间网隔离）

- 向量库（PGVector）+ Embedding（bge-m3）+ Rerank（bge-reranker）本地化部署；LLM 视车间安全策略云端 API 或本地化模型二选一，`BaseChatModel` 抽象零代码切换。
- 原始文档文件存 MinIO（与既有对象存储一致），PGVector 只存文本 chunk + 向量 + 定位。
- MVP 用 `docker-compose` 本地起 PostgreSQL(pgvector) + MinIO + Redis + rag-doc-service（§9.9）。

### 2.5 依赖清单（pyproject.toml 片段）

```toml
[project]
name = "rag-doc-service"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "gunicorn>=21.2",
  "pydantic>=2.6",
  "pydantic-settings>=2.2",
  "sqlalchemy[asyncio]>=2.0",
  "asyncpg>=0.29",
  "pgvector>=0.3",
  "llama-index>=0.10",
  "langchain-core>=0.2",
  "httpx>=0.27",
  "aiokafka>=0.10",
  "redis>=5.0",
  "minio>=7.2",
  "opentelemetry-api>=1.24",
  "opentelemetry-instrumentation-fastapi>=0.45b",
  "prometheus-client>=0.20",
  # 文档解析
  "unstructured>=0.14",
  "pypdf>=4.0",
  "python-docx>=1.1",
  # bge-m3 embedding + bge-reranker 本地化推理
  "sentence-transformers>=3.0",
  "FlagEmbedding>=1.2",
]
```

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ rag-doc-service（独立微服务，Python + FastAPI + PGVector）          │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ DocumentRetrievalService                  │  │
│  │ /rag/docs/*  │  │  query->向量检索+过滤->rerank->LLM综合      │  │
│  └──────────────┘  └─────────────────┬────────────────────────┘  │
│                                      │                            │
│              ┌───────────────────────┼───────────────────────┐    │
│              ▼                       ▼                       ▼    │
│  ┌───────────────────┐  ┌─────────────────────┐  ┌──────────┐ │
│  │ DocumentIngestion │  │ VectorRetriever     │  │ Reranker │ │
│  │ Service           │  │ (PGVector pre-filter)│  │ bge-rer  │ │
│  │ 解析->切分->向量化 │  └──────────┬──────────┘  └──────────┘ │
│  └────────┬──────────┘             │                          │
│           │                        ▼                            │
│  ┌────────▼────────┐        ┌──────────────────┐                │
│  │ ReindexCoordinator│       │ MinIO            │                │
│  │ 事件->状态流转/重绑 │       │ (原始文档文件)    │                │
│  └────────┬────────┘        └──────────────────┘                │
│           │                                                        │
│  ┌────────▼──────────────────────────────────────┐               │
│  │ PostgreSQL + PGVector（单一实例，全程同库）       │               │
│  │  knowledge_document / document_version /       │               │
│  │  document_chunk(+embedding+HNSW) /              │               │
│  │  index_idempotency / index_offset               │               │
│  └───────────────────────────────────────────────┘               │
└───────────────────────────────────┼──────────────────────────────┘
                                    │ 订阅领域事件（只读，触发重索引）
                          ┌─────────▼──────────┐
                          │ aiokafka Consumer   │
                          │ process.route.lifecycle│
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

- **单库同事务**：文档元数据 / chunk / 向量 / 幂等 / 位点全在一个 PG 实例，摄入与重索引的事务原子性靠 PG 本地事务保证--比追溯型（Neo4j + MySQL 跨库非分布式）更简单可靠。
- **冗余过滤字段到 chunk**：`state` / `tenant_scope` / `doc_type` / `binding_route_version` / `binding_asset_id` 冗余到 `document_chunk` 表，向量检索 `WHERE` 直接在 chunk 表前置过滤，避免 join，性能最优。冗余由摄入 / 重索引时同步维护（§5.3）。
- **摄入与检索分离**：`DocumentIngestionService`（写）与 `VectorRetriever`（读）解耦，摄入滞后不阻塞检索--未 `PUBLISHED` 的新版本天然不召回。
- **版本治理三道闸**：① 摄入闸（版本绑定 + 状态机）；② 重索引闸（`ProcessRouteActivated` 驱动状态流转）；③ 检索闸（SQL `state='PUBLISHED'` + `route_version` 前置过滤）。
- **ACL 防腐层**：降级查询各上下文只读 REST（查文档绑定关系）经 ACL 适配，外部 DTO -> 内部视图，符合 CLAUDE.md ACL 约束。

---

## 4. 文档领域建模（MVP）

### 4.1 聚合根 / 实体 / 值对象（MVP）

```text
KnowledgeDocument（聚合根）
  ├─ document_id, doc_type(SOP/MANUAL/STANDARD), title, category, tenant_scope
  └─ DocumentVersion（实体）
       ├─ version_id, version_no, state, source_type, file_ref, file_content_hash
       ├─ bindings: list[DocumentBinding]（值对象，jsonb）
       └─ DocumentChunk（实体）
            ├─ chunk_id, ordinal, text, embedding, section_type, locator
            └─ 冗余: state, tenant_scope, doc_type, binding_route_id/version, binding_asset_id
```

- **`DocumentBinding` 值对象**：`{binding_type: ROUTE_VERSION/ASSET, target_ref, inherited}`，存 `document_version.bindings` jsonb。工艺绑定型文档含 `ROUTE_VERSION` 绑定，设备绑定型含 `ASSET` 绑定。
- **chunk 冗余字段**：从 version / document 同步冗余到 chunk，供检索前置过滤。状态流转时同步更新（§5.3）。

### 4.2 PGVector DDL（MVP 表结构）

启动时由 `SchemaInitializer`（§9.7）幂等执行（`IF NOT EXISTS`）：

```sql
-- 启用扩展
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- 文档表
CREATE TABLE IF NOT EXISTS knowledge_document (
  document_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_type      varchar(32)  NOT NULL,     -- SOP / MANUAL / STANDARD
  title         varchar(512) NOT NULL,
  category      varchar(32)  NOT NULL,     -- PROCESS_BOUND / ASSET_BOUND / GENERAL
  tenant_scope  varchar(64)  NOT NULL,
  created_at    timestamptz  NOT NULL DEFAULT now()
);

-- 文档版本表
CREATE TABLE IF NOT EXISTS document_version (
  version_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id        uuid NOT NULL REFERENCES knowledge_document(document_id),
  version_no         varchar(32)  NOT NULL,
  state              varchar(32)  NOT NULL,  -- DRAFT/SUBMITTED/PUBLISHED/DEPRECATED/ARCHIVED/PENDING_REBIND
  source_type        varchar(32)  NOT NULL,  -- UPLOAD / SYNC
  file_ref           varchar(512) NOT NULL,  -- MinIO URI
  file_content_hash  varchar(64)  NOT NULL,  -- 摄入幂等
  bindings           jsonb        NOT NULL DEFAULT '[]',  -- DocumentBinding 集合
  effective_at       timestamptz,
  deprecated_at      timestamptz,
  created_at         timestamptz  NOT NULL DEFAULT now(),
  UNIQUE (document_id, version_no),
  UNIQUE (file_content_hash)                  -- 同文件不重复摄入
);
CREATE INDEX IF NOT EXISTS dv_state_idx ON document_version (state);
CREATE INDEX IF NOT EXISTS dv_doc_idx ON document_version (document_id);

-- 文档分块表（含向量 + 冗余过滤字段）
CREATE TABLE IF NOT EXISTS document_chunk (
  chunk_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_id            uuid NOT NULL REFERENCES document_version(version_id) ON DELETE CASCADE,
  document_id           uuid NOT NULL REFERENCES knowledge_document(document_id),
  ordinal               int  NOT NULL,
  text                  text NOT NULL,
  embedding             vector(1024) NOT NULL,
  section_type          varchar(32),                 -- STEP / FAULT_CODE / PARAM / NOTE / SECTION
  locator               jsonb NOT NULL,              -- {page, offset, heading_path}
  -- 冗余字段（检索前置过滤，避免 join）
  state                 varchar(32) NOT NULL,
  tenant_scope          varchar(64) NOT NULL,
  doc_type              varchar(32) NOT NULL,
  binding_route_id      varchar(64),                 -- 工艺绑定型
  binding_route_version varchar(32),
  binding_asset_id      varchar(64),                 -- 设备绑定型
  UNIQUE (version_id, ordinal)
);

-- HNSW 向量索引（cosine）
CREATE INDEX IF NOT EXISTS document_chunk_embedding_idx
  ON document_chunk USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- 过滤字段索引（pre-filter 加速）
CREATE INDEX IF NOT EXISTS document_chunk_filter_idx
  ON document_chunk (state, tenant_scope, doc_type);
CREATE INDEX IF NOT EXISTS document_chunk_route_idx
  ON document_chunk (binding_route_id, binding_route_version);
CREATE INDEX IF NOT EXISTS document_chunk_asset_idx
  ON document_chunk (binding_asset_id);

-- 幂等表（重索引事件去重）
CREATE TABLE IF NOT EXISTS index_idempotency (
  event_id       varchar(64)  NOT NULL,
  consumer_group varchar(64)  NOT NULL,
  topic          varchar(128) NOT NULL,
  processed_at   timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, consumer_group)
);

-- 位点表
CREATE TABLE IF NOT EXISTS index_offset (
  consumer_group varchar(64)  NOT NULL,
  topic          varchar(128) NOT NULL,
  partition_no   int     NOT NULL,
  offset_no      bigint  NOT NULL,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer_group, topic, partition_no)
);
```

- **`ON DELETE CASCADE`**：版本删除时 chunk 跟随删除（仅 DRAFT 阶段允许删；PUBLISHED 后不可删，只 DEPRECATED）。
- **冗余字段索引**：`document_chunk_filter_idx` 覆盖 `state + tenant_scope + doc_type` 组合过滤，是检索 pre-filter 的主力索引。
- **HNSW 参数**：`m=16, ef_construction=64` 是平衡构建速度与召回的常用值；检索时 `ef_search` 可调（默认 40）。🔴 需按真实 chunk 量评测调优。

### 4.3 文档版本生命周期（MVP 状态机）

对齐工艺版本生命周期（`RouteVersionState`：DRAFT/SUBMITTED/ACTIVATED/DEPRECATED/ARCHIVED）：

```mermaid
stateDiagram-v2
    [*] --> DRAFT: 上传/同步
    DRAFT --> PUBLISHED: 审核通过
    PUBLISHED --> PENDING_REBIND: ProcessRouteActivated(关联工艺升版)
    PENDING_REBIND --> PUBLISHED: 文档owner确认继承绑定(inherited=true)
    PENDING_REBIND --> PUBLISHED: 文档owner发新版本(内容变更)
    PUBLISHED --> DEPRECATED: 新版本PUBLISHED / ProcessRouteDeprecated
    DEPRECATED --> ARCHIVED: 归档(不默认检索,引用可追溯)
```

- **`PENDING_REBIND`**：工艺升版时关联文档进入待确认，由文档 owner 人工判断继承绑定或发新版本（[详细设计](./文档型 RAG-详细设计.md) §4.4 方案 B）。🔴 MVP 默认人工确认，不自动流转。
- **同类绑定唯一 PUBLISHED**：新版本 PUBLISHED 时，同 `document_id` + 同类绑定的旧版本自动 DEPRECATED（§5.4）。

---

## 5. 文档摄入与索引构建

### 5.1 摄入管线

```text
原始文档(PDF/Word/MD) ──▶ MinIO(file_ref)
        │
        ▼
DocumentIngestionService.ingest()
  ├─ 1. 计算 file_content_hash，查重（幂等）
  ├─ 2. 加载：从 MinIO 拉文件
  ├─ 3. 解析：unstructured/pypdf/python-docx -> 结构化文本(标题层级)
  ├─ 4. 切分：按 doc_type 选 NodeParser（§5.2）
  ├─ 5. 向量化：bge-m3 批量 embed（1024 维）
  ├─ 6. 持久化：knowledge_document + document_version + document_chunk 同 PG 事务
  └─ 7. 发 DocumentVersionPublished 事件（供检索/审计）
```

- **同事务持久化**：文档元数据 + 版本 + chunk + 向量同 PG 事务，原子提交。
- **冗余字段同步**：写入 chunk 时从 version / document 同步冗余 `state` / `tenant_scope` / `doc_type` / `binding_*` 字段。

### 5.2 切分策略（按 doc_type）

| doc_type | 切分策略 | LlamaIndex NodeParser |
|---------|---------|----------------------|
| SOP / 作业指导书 | 按工序步骤切 + 步骤内段落 | `HierarchicalNodeParser`（标题层级感知）+ 步骤标记 |
| MANUAL（设备维修手册） | 按故障代码 / 章节切 | `MarkdownNodeParser` / `HeadingNodeParser` |
| STANDARD（检验标准） | 按检验项切，表格保留 | `SentenceSplitter` + 表格保留 |

```python
# app/application/ingestion/chunking.py
class ChunkStrategySelector:
    """按 doc_type 选切分策略，单一职责。"""

    def select(self, doc_type: str) -> NodeParser:
        if doc_type == "SOP":
            # SOP 按标题层级切，保留步骤边界，chunk 256-512 token 重叠 10%
            return HierarchicalNodeParser.from_defaults(
                chunk_sizes=[512, 128], chunk_overlap=50
            )
        if doc_type == "MANUAL":
            # 手册按章节标题切，故障代码章节完整保留
            return HeadingNodeParser(max_chunk_size=512, overlap=50)
        if doc_type == "STANDARD":
            # 检验标准按句切，参数表整体保留
            return SentenceSplitter(chunk_size=384, chunk_overlap=30)
        raise ValueError(f"不支持的文档类型: {doc_type}")
```

- 🔴 chunk size / overlap 需按真实文档评测调优（[详细设计](./文档型 RAG-详细设计.md) §5.2），上表是 MVP 起始值。
- 切分时识别 `section_type`（STEP / FAULT_CODE / PARAM / NOTE），写入 chunk 元数据供检索过滤。

### 5.3 冗余字段同步

chunk 表的 `state` / `tenant_scope` / `binding_*` 冗余自 version / document，状态流转时必须同步：

```python
# 状态流转时同步冗余字段（重索引消费者调用）
async def sync_chunk_state(self, version_id: str, new_state: str) -> None:
    await self._session.execute(
        text("""
            UPDATE document_chunk
            SET state = :new_state
            WHERE version_id = :version_id
        """),
        {"new_state": new_state, "version_id": version_id},
    )
```

- `ProcessRouteActivated` 触发 `PENDING_REBIND`、`ProcessRouteDeprecated` 触发 `DEPRECATED` 时，同步更新 chunk 的 `state`，保证检索 `WHERE state='PUBLISHED'` 立即生效。

### 5.4 版本治理与重索引（事件驱动）

**重索引事件订阅**：

| 领域事件 | 主题 | 消费者组 | 重索引动作 |
|---------|------|---------|-----------|
| `ProcessRouteActivated` | `process.route.lifecycle` | `rag-doc-process` | ① 旧 `route_version` 关联文档版本 -> `PENDING_REBIND`（同步 chunk.state）；② 发布 `RebindRequired` 通知文档 owner |
| `ProcessRouteDeprecated` | `process.route.lifecycle` | `rag-doc-process` | 关联文档版本 -> `DEPRECATED`（同步 chunk.state），不删 |
| `rag.reindex.request` | `rag.reindex.*` | `rag-doc-reindex` | 追溯型 RAG 发布的重索引请求，按 `route_id` + `route_version` 重索引关联文档 |
| `DocumentVersionPublished` | `rag.doc.lifecycle` | `rag-doc-lifecycle` | 新版本 PUBLISHED 时，同类绑定旧版本 -> `DEPRECATED` |

**新版本 PUBLISHED 时旧版本自动 DEPRECATED**（同类绑定唯一 PUBLISHED）：

```python
async def publish_version(self, version_id: str) -> None:
    async with self._session.begin():
        # 1. 当前版本 PUBLISHED
        await self._session.execute(
            text("UPDATE document_version SET state='PUBLISHED', effective_at=now() "
                 "WHERE version_id=:vid"), {"vid": version_id},
        )
        # 2. 同 document_id + 同类绑定的旧版本 DEPRECATED
        await self._session.execute(
            text("""
                UPDATE document_version SET state='DEPRECATED', deprecated_at=now()
                WHERE document_id=(SELECT document_id FROM document_version WHERE version_id=:vid)
                  AND version_id <> :vid
                  AND state='PUBLISHED'
            """), {"vid": version_id},
        )
        # 3. 同步 chunk.state（旧版本 chunk -> DEPRECATED，新版本 chunk -> PUBLISHED）
        await self._session.execute(
            text("UPDATE document_chunk SET state='PUBLISHED' WHERE version_id=:vid"), {"vid": version_id},
        )
        await self._session.execute(
            text("""
                UPDATE document_chunk SET state='DEPRECATED'
                WHERE document_id=(SELECT document_id FROM document_version WHERE version_id=:vid)
                  AND version_id <> :vid AND state='PUBLISHED'
            """), {"vid": version_id},
        )
```

> **版本一致性不是文档型 RAG 自己保证的，是从领域模型兜上来的**--工艺版本有生命周期、变更发 `ProcessRouteActivated`、文档绑定对齐 `route_version`。RAG 只是严格遵循这套契约（[详细设计](./文档型 RAG-详细设计.md) §2.4 / §5.4）。

### 5.5 幂等与去重

事件经各上下文 Transactional Outbox **至少一次**投递（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §1），RAG 侧必须幂等消费。

- **重索引幂等**：`event_id + consumer_group` 幂等表（§4.2），重复投递被挡住。
- **摄入幂等**：`file_content_hash` 唯一约束--同文件重复摄入抛唯一约束冲突，跳过。
- **chunk 幂等**：`version_id + ordinal` 唯一约束。
- **位点上移**：幂等记录与位点更新同 PG 事务，"已处理 ⇒ 已 ack"。

---

## 6. 检索与生成

### 6.1 检索入口

```text
用户问题 + 上下文{route_version?, asset_id?, station_id?}
        │
        ▼
DocumentRetrievalService.query()
  ├─ 1. 向量化：bge-m3 embed query
  ├─ 2. 向量检索 + 元数据过滤（PGVector pre-filter，§6.2）-> top_k=20
  ├─ 3. Rerank：bge-reranker 精排 -> top_n=5
  ├─ 4. LLM 综合：question + chunks -> DocAnswer + citations
  └─ 5. 置信度判断：confidence<0.6 或无引用 -> needs_human_review
```

### 6.2 向量检索 + 元数据过滤（PGVector pre-filter）

```python
# app/infrastructure/pgvector/retriever.py
RETRIEVE_SQL = """
SELECT
    c.chunk_id, c.text, c.section_type, c.locator,
    c.document_id, c.version_id, c.binding_route_version,
    dv.version_no, kd.title, kd.doc_type,
    1 - (c.embedding <=> :query_vec) AS similarity
FROM document_chunk c
JOIN document_version dv ON dv.version_id = c.version_id
JOIN knowledge_document kd ON kd.document_id = c.document_id
WHERE c.state = 'PUBLISHED'                            -- ① 只检索已发布
  AND c.tenant_scope = ANY(:tenant_scopes)             -- ② 租户前置过滤
  AND (:route_version IS NULL                          -- ③ 版本过滤：带则精确，不带则当前生效
       OR c.binding_route_version = :route_version)
  AND (:asset_id IS NULL                               -- ④ 设备过滤
       OR c.binding_asset_id = :asset_id)
ORDER BY c.embedding <=> :query_vec                    -- ⑤ cosine 近邻
LIMIT :top_k
"""

class VectorRetriever:
    """PGVector 向量检索 + 元数据前置过滤。检索只管取 chunk，不管 LLM 综合（SRP）。"""

    def __init__(self, session: AsyncSession, embedder: BgeClient) -> None:
        self._session = session
        self._embedder = embedder

    async def retrieve(
        self, query: str, tenant: TenantContext,
        route_version: str | None = None, asset_id: str | None = None,
        top_k: int = 20,
    ) -> list[ChunkHit]:
        query_vec = await self._embedder.embed(query)
        result = await self._session.execute(
            text(RETRIEVE_SQL),
            {
                "query_vec": query_vec,
                "tenant_scopes": tenant.scopes(),
                "route_version": route_version,
                "asset_id": asset_id,
                "top_k": top_k,
            },
        )
        return [ChunkHitMapper.to_view(row) for row in result]
```

- **`state='PUBLISHED'` 前置**：`DEPRECATED` / `PENDING_REBIND` 不进候选，从结构上杜绝失效文档（§1.2 DEPRECATED 不泄漏）。
- **版本过滤**：带 `route_version` 精确定位当时版本（历史回溯）；不带则取当前生效（`PUBLISHED` 即当前生效）。
- **租户前置过滤**：`tenant_scope = ANY(:tenant_scopes)` 在向量近邻前裁剪。
- **PGVector pre-filter**：先 SQL `WHERE` 再 `<=>` 近邻，版本/权限/设备组合过滤全在向量检索前完成。

### 6.3 Rerank

```python
# app/infrastructure/ai/reranker.py
class BgeReranker:
    """bge-reranker-v2-m3 cross-encoder 精排。"""

    def __init__(self) -> None:
        self._model = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)

    async def rerank(self, query: str, chunks: list[ChunkHit], top_n: int = 5) -> list[ChunkHit]:
        if not chunks:
            return []
        pairs = [[query, c.text] for c in chunks]
        scores = self._model.compute_score(pairs, normalize=True)
        ranked = sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)
        return [c for c, _ in ranked[:top_n]]
```

- 🔴 top_k / top_n 需评测调优（[详细设计](./文档型 RAG-详细设计.md) §6.3）。

### 6.4 LLM 综合与引用

```python
# app/domain/answer.py
class DocCitation(BaseModel):
    chunk_id: str
    document_id: str
    version_no: str
    title: str
    locator: dict            # {page, offset, heading_path}
    quoted_text: str

class DocAnswer(BaseModel):
    answer: str
    citations: list[DocCitation]     # 强制引用，无引用判失败重试
    confidence: float
    route_version_filter: str | None
    disclaimer: str = "本答案来自文档型 RAG，处置需按现行 SOP 确认"
    needs_human_review: bool = False
```

```python
# app/application/retrieval_service.py
class DocumentRetrievalService:
    def __init__(
        self, retriever: VectorRetriever, reranker: BgeReranker,
        llm: BaseChatModel, cache: RetrievalCache,
    ) -> None:
        self._retriever = retriever; self._reranker = reranker
        self._llm = llm; self._cache = cache

    async def query(self, req: DocQuery, tenant: TenantContext) -> DocAnswer:
        # 1. 缓存（query + 版本 + 租户命中即用）
        cached = await self._cache.get(req.question, req.route_version, tenant)
        if cached:
            return cached
        # 2. 向量检索 + 过滤 -> top_k
        chunks = await self._retriever.retrieve(
            req.question, tenant, req.route_version, req.asset_id, top_k=20
        )
        # 3. Rerank -> top_n
        ranked = await self._reranker.rerank(req.question, chunks, top_n=5)
        # 4. LLM 综合（结构化输出，强制引用）
        answer = await self._synthesize(req.question, ranked, req.route_version)
        # 5. 置信度兜底
        if answer.confidence < 0.6 or not answer.citations:
            answer.needs_human_review = True
        await self._cache.set(req.question, req.route_version, tenant, answer)
        return answer

    async def _synthesize(
        self, question: str, chunks: list[ChunkHit], route_version: str | None
    ) -> DocAnswer:
        prompt = self._build_prompt(question, chunks, route_version)
        # with_structured_output 强制模型返回 DocAnswer schema
        return await self._llm.with_structured_output(DocAnswer).ainvoke(prompt)
```

- **强制引用 `chunk_id`**：系统提示词约束"无引用的答案判失败重试"--与追溯型 RAG"证据强制引用 node_id"同思路。
- **置信度阈值**：`confidence < 0.6` 或无引用 -> `needs_human_review`，不展示给操作工。
- **系统提示词约束**：明确"只能基于提供的 chunks 回答，不得编造；无相关文档则回答'未找到相关文档'；输出严格遵循 DocAnswer 结构"。

---

## 7. ACL 防腐层（只读 REST 契约 + 降级）

图投影滞后或绑定关系缺失时，`VectorRetriever` / `ReindexCoordinator` 经 ACL 降级查询对应上下文只读 REST 补齐。MVP 只读 REST 契约：

| 上下文 | 只读 REST | 用途 | 版本校验 |
|--------|----------|------|---------|
| 工艺管理 | `GET /api/process-routes/{route_id}?version={route_version}` | 降级取工艺版本详情，确认绑定关系 | 强制 `version` 入参，校验 `status` |
| 工艺管理 | `GET /api/process-routes/{route_id}/current` | 查当前生效工艺版本（不带 route_version 检索时） | - |
| 过点执行 | `GET /api/checkpoints?sn=` | 按单件查当时 `route_version`（历史 SOP 检索） | - |

```python
# app/infrastructure/acl/process_management.py
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

    async def fetch_current_route_version(
        self, route_id: str, tenant: TenantContext
    ) -> RouteVersionView:
        """不带 route_version 检索时，查当前生效工艺版本。"""
        resp = await self._http.get(
            f"/api/process-routes/{route_id}/current",
            headers=tenant.headers(),
            timeout=2.0,
        )
        resp.raise_for_status()
        return RouteVersionMapper.to_view(RouteVersionDTO.model_validate(resp.json()))
```

- 外部 DTO 不进检索核心，只暴露 `RouteVersionView`--防腐层核心职责（CLAUDE.md ACL 约束）。
- 降级查询是兜底，不进过点主事务，超时降级为低置信度而非阻塞。

---

## 8. 推荐包结构（Python src layout）

```text
rag_doc_service/
  app/
    api/                       # FastAPI 路由层
      doc_router.py            # /rag/docs/query, /rag/docs/ingest, /rag/docs/search
      schemas.py               # Request / Response 模型
    application/               # 应用服务，编排
      ingestion_service.py     # DocumentIngestionService
      retrieval_service.py     # DocumentRetrievalService
      reindex_coordinator.py   # ReindexCoordinator（事件->状态流转）
      chunking.py              # ChunkStrategySelector
    domain/                    # 文档子域模型
      document.py              # KnowledgeDocument / DocumentVersion / DocumentBinding
      chunk.py                 # DocumentChunk / ChunkLocator
      answer.py                # DocAnswer / DocCitation
      tenant.py                # TenantContext
      projection.py            # ReindexHandler 协议 / ReadOnlyIngestionGate
    infrastructure/
      pgvector/                # PGVector 存储
        driver.py              # AsyncEngine 封装
        schema.py              # SchemaInitializer（DDL，§4.2）
        retriever.py           # VectorRetriever（向量检索+过滤）
        document_repo.py       # 文档/版本/chunk Repository
      rag/                     # LlamaIndex VectorStoreIndex 封装（可选上层）
        index.py
      embedding/               # bge-m3 客户端
        bge_client.py
      ai/                      # LLM + Reranker
        llm_factory.py
        reranker.py            # BgeReranker
      acl/                     # 降级查询各上下文只读 REST
        process_management.py
        checkpoint.py
      minio_/                  # 原始文档文件存储
        object_store.py
      kafka/                   # aiokafka 消费者（重索引事件）
        consumer_group.py
        listeners.py
      persistence/             # SQLAlchemy 模型 + Repository
        models.py              # index_idempotency / index_offset
        idempotency_repo.py
        offset_repo.py
      redis_/                   # 检索结果缓存
        retrieval_cache.py
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

- `domain/projection.ReindexHandler` 是协议（ISP），每个事件类型实现自己的处理器。
- `infrastructure/pgvector/` 是存储落地，检索与摄入分离（SRP）。
- `infrastructure/acl/` 是防腐层，外部 DTO 经 Mapper 转内部视图，不污染检索核心。

---

## 9. 关键代码骨架

### 9.1 摄入服务（DocumentIngestionService）

```python
# app/application/ingestion_service.py
class DocumentIngestionService:
    """文档摄入编排：解析 -> 切分 -> 向量化 -> 持久化。SRP。"""

    def __init__(
        self, object_store: ObjectStore, parser: DocumentParser,
        chunk_selector: ChunkStrategySelector, embedder: BgeClient,
        doc_repo: DocumentRepo, idem_repo: IdempotencyRepo,
    ) -> None:
        self._store = object_store; self._parser = parser
        self._chunk_selector = chunk_selector; self._embedder = embedder
        self._doc_repo = doc_repo; self._idem = idem_repo

    async def ingest(self, cmd: IngestCommand, tenant: TenantContext) -> str:
        # 1. 上传 MinIO + 计算 hash
        file_ref = await self._store.put(cmd.filename, cmd.content)
        content_hash = sha256(cmd.content)
        # 2. 幂等：同文件不重复摄入
        if await self._doc_repo.find_by_hash(content_hash):
            return "already_ingested"
        # 3. 解析 + 切分
        structured = await self._parser.parse(cmd.filename, cmd.content)
        node_parser = self._chunk_selector.select(cmd.doc_type)
        chunks = node_parser.get_nodes_from_documents([structured])
        # 4. 批量向量化
        texts = [c.text for c in chunks]
        embeddings = await self._embedder.embed_batch(texts)
        # 5. 同事务持久化（文档 + 版本 + chunk + 向量 + 冗余字段）
        version_id = await self._doc_repo.save_document(
            doc_type=cmd.doc_type, title=cmd.title, category=cmd.category,
            tenant=tenant, file_ref=file_ref, content_hash=content_hash,
            bindings=cmd.bindings, chunks=chunks, embeddings=embeddings,
            state="DRAFT",  # MVP：上传后 DRAFT，待审核 PUBLISHED
        )
        return version_id
```

- 摄入后状态为 `DRAFT`，需审核通过才 `PUBLISHED` 进入检索可见（§4.3 状态机）。
- `file_content_hash` 幂等：同文件重复摄入直接返回。

### 9.2 重索引消费者（ReindexCoordinator）

```python
# app/application/reindex_coordinator.py
class ReindexHandler(Protocol):
    """一个事件类型一个重索引处理器。"""
    event_type: str
    async def handle(self, event: DomainEvent, session: AsyncSession) -> None: ...

class ReindexCoordinator:
    """消费领域事件，幂等驱动文档版本状态流转 / 重新绑定。"""

    def __init__(
        self, handlers: dict[str, ReindexHandler],
        session_factory: async_sessionmaker,
        idem_repo: IdempotencyRepo, offset_repo: OffsetRepo,
        metrics: MetricsCollector,
    ) -> None:
        self._handlers = handlers; self._sf = session_factory
        self._idem = idem_repo; self._offset = offset_repo; self._metrics = metrics

    async def consume(self, msg: ConsumerRecord, group: str) -> None:
        event = DomainEvent.model_validate_json(msg.value)
        # 1. 幂等检查
        if await self._idem.exists(event.event_id, group):
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            self._metrics.reindex_duplicate.inc(group)
            return
        # 2. 路由到处理器
        handler = self._handlers.get(event.event_type)
        if handler is None:
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        # 3. 同 PG 事务：状态流转 + 幂等记录 + 位点推进
        async with self._sf() as session, session.begin():
            await handler.handle(event, session)
            await self._idem.record(event.event_id, group, msg.topic, session=session)
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset, session=session)
        self._metrics.reindex_total.inc(handler.event_type)
```

```python
# app/infrastructure/kafka/listeners.py
class ProcessRouteActivatedHandler:
    """ProcessRouteActivated -> 关联文档版本进入 PENDING_REBIND。"""
    event_type = "ProcessRouteActivated"

    async def handle(self, event: DomainEvent, session: AsyncSession) -> None:
        p = event.payload
        route_id, new_version = p["route_id"], p["route_version"]
        # 旧 route_version 关联文档版本 -> PENDING_REBIND（同步 chunk.state）
        await session.execute(text("""
            UPDATE document_version SET state='PENDING_REBIND'
            WHERE bindings @> :binding AND state='PUBLISHED'
        """), {"binding": json.dumps([{"binding_type": "ROUTE_VERSION",
                                        "target_ref": {"route_id": route_id}}])})
        # 同步 chunk.state
        await session.execute(text("""
            UPDATE document_chunk SET state='PENDING_REBIND'
            WHERE binding_route_id=:route_id AND state='PUBLISHED'
              AND binding_route_version <> :new_version
        """), {"route_id": route_id, "new_version": new_version})


class ProcessRouteDeprecatedHandler:
    """ProcessRouteDeprecated -> 关联文档版本 DEPRECATED（不删）。"""
    event_type = "ProcessRouteDeprecated"

    async def handle(self, event: DomainEvent, session: AsyncSession) -> None:
        p = event.payload
        await session.execute(text("""
            UPDATE document_version SET state='DEPRECATED', deprecated_at=now()
            WHERE bindings @> :binding AND state IN ('PUBLISHED','PENDING_REBIND')
        """), {"binding": json.dumps([{"binding_type": "ROUTE_VERSION",
                                        "target_ref": {"route_id": p["route_id"],
                                                       "route_version": p["route_version"]}}])})
        await session.execute(text("""
            UPDATE document_chunk SET state='DEPRECATED'
            WHERE binding_route_id=:rid AND binding_route_version=:rv
        """), {"rid": p["route_id"], "rv": p["route_version"]})
```

- `bindings @> :binding` 用 PG jsonb 包含查询定位关联文档（`bindings` 含对应 `ROUTE_VERSION` 绑定）。
- 状态流转与 chunk.state 同步、幂等记录、位点推进**同 PG 事务**（单库优势，§3.1）。
- 不自动 PUBLISHED--`PENDING_REBIND` 等文档 owner 人工确认（🔴 §4.3）。

### 9.3 检索服务（DocumentRetrievalService）

见 §6.4。检索与综合分离：`VectorRetriever` 只管取 chunk，`_synthesize` 只管 LLM 综合（SRP）。

### 9.4 Embedding 客户端（bge-m3）

```python
# app/infrastructure/embedding/bge_client.py
class BgeClient:
    """bge-m3 向量化，1024 维，批量推理。"""

    def __init__(self) -> None:
        self._model = BGEM3Inference(model_name="BAAI/bge-m3", use_fp16=True)

    async def embed(self, text: str) -> list[float]:
        return (await self._model.async_encode([text]))[0]

    async def embed_batch(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        results = []
        for i in range(0, len(texts), batch_size):
            results.extend(await self._model.async_encode(texts[i:i + batch_size]))
        return results
```

### 9.5 向量检索器（VectorRetriever）

见 §6.2。检索器只管取 chunk，不做 LLM 综合。`VectorRetriever` 抽象保证未来换 Milvus / Qdrant 时检索逻辑可替换（§2.3）。

### 9.6 启动断言

```python
# app/domain/projection.py
class ReadOnlyIngestionGate(Exception):
    """启动时发现非只读动作（写 MES），拒绝启动。"""
class RawDataTopicGate(Exception):
    """启动时发现消费者组误订原始报文主题，拒绝启动。"""

# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.reindex_registry
    # 启动断言：重索引处理器只改文档库状态，不写 MES
    registry.assert_read_only()              # 扫描处理器，禁止调 MES 写接口
    # 启动断言：消费者组只订 process.route.lifecycle / rag.reindex.*，未误订 dc.*
    registry.assert_no_raw_data_topic()      # 校验订阅列表无 dc.equipment.data.raw 等
    # Schema 初始化（表/索引/HNSW）
    await app.state.schema_initializer.init(app.state.engine)
    # 初始化消费者组 ...
    async with app.state.kafka_consumer_groups as groups:
        for g in groups:
            asyncio.create_task(g.run())
        yield
```

- `assert_read_only` 扫描所有 `ReindexHandler`，禁止出现对 MES 写接口的调用--红线靠启动断言兜底。
- `assert_no_raw_data_topic` 校验消费者组订阅列表无 `dc.*` 原始报文主题。

### 9.7 DEPRECATED 泄漏兜底

```python
# 检索后校验：结果不得含非 PUBLISHED chunk
async def _assert_no_deprecated_leak(self, chunks: list[ChunkHit]) -> None:
    leaked = [c for c in chunks if c.state != "PUBLISHED"]
    if leaked:
        self._metrics.deprecated_leak.inc(len(leaked))
        # 过滤掉泄漏 chunk，只返回 PUBLISHED
        chunks = [c for c in chunks if c.state == "PUBLISHED"]
```

- SQL 强制 `state='PUBLISHED'`，正常情况无泄漏；此校验是双保险，泄漏即告警。

### 9.8 FastAPI 入口

```python
# app/api/doc_router.py
router = APIRouter(prefix="/rag/docs", tags=["document-rag"])

@router.post("/query", response_model=DocAnswer)
async def query(
    req: DocQuery,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: DocumentRetrievalService = Depends(get_retrieval_service),
) -> DocAnswer:
    """问 + 答 + 引用。供工程师 UI / L1 Agent search_docs 调用。"""
    return await svc.query(req, tenant)

@router.post("/search", response_model=list[ChunkHit])
async def search(
    req: SearchRequest,
    tenant: TenantContext = Depends(tenant_from_token),
    retriever: VectorRetriever = Depends(get_retriever),
) -> list[ChunkHit]:
    """只检索 chunks 不综合，供 L1 Agent / UI 直接消费引用片段。"""
    return await retriever.retrieve(
        req.question, tenant, req.route_version, req.asset_id, top_k=req.top_k
    )

@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestCommand,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: DocumentIngestionService = Depends(get_ingestion_service),
) -> IngestResponse:
    """文档摄入（MVP 管理接口，生产环境接文档管理系统同步）。"""
    version_id = await svc.ingest(req, tenant)
    return IngestResponse(version_id=version_id)
```

- 三个端点：`/query`（检索 + LLM 综合）给工程师问答 / L1 Agent；`/search`（纯 chunks）给 L1 Agent 与 UI 直接消费引用片段；`/ingest` 给文档摄入。
- 租户上下文从 token 解析，注入检索链路全程。

### 9.9 配置与部署

```python
# app/config.py
class Settings(BaseSettings):
    # PostgreSQL + PGVector
    pg_dsn: str = "postgresql+asyncpg://rag:rag@postgres:5432/rag_doc"
    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "rag-docs"
    # Redis
    redis_url: str = "redis://redis:6379/0"
    # Kafka
    kafka_bootstrap: str = "kafka:9092"
    # LLM（可插拔）
    llm_provider: str = "openai"   # openai / dashscope / local
    llm_api_base: str | None = None
    llm_model: str = "qwen-plus"
    # Embedding / Rerank
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # 检索参数
    retrieve_top_k: int = 20
    rerank_top_n: int = 5
    confidence_threshold: float = 0.6

    class Config:
        env_prefix = "RAG_DOC_"
```

```yaml
# docker-compose.yml（MVP 本地起）
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: rag_doc
      POSTGRES_USER: rag
      POSTGRES_PASSWORD: rag
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]

  minio:
    image: minio/minio
    command: server /data
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ["9000:9000"]
    volumes: ["miniodata:/data"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  rag-doc-service:
    build: .
    depends_on: [postgres, minio, redis]
    environment:
      RAG_DOC_PG_DSN: postgresql+asyncpg://rag:rag@postgres:5432/rag_doc
      RAG_DOC_KAFKA_BOOTSTRAP: kafka:9092
      RAG_DOC_MINIO_ENDPOINT: minio:9000
      RAG_DOC_REDIS_URL: redis://redis:6379/0
    ports: ["8081:8080"]

volumes:
  pgdata:
  miniodata:
```

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `rag_doc_ingest_total` | 文档摄入数（按 doc_type label） |
| `rag_doc_ingest_latency_seconds` | 摄入延迟（解析+切分+向量化，Histogram） |
| `rag_doc_ingest_error_total` | 摄入失败次数 |
| `rag_doc_chunk_total` | chunk 总数（按 doc_type / state label） |
| `rag_doc_reindex_total` | 重索引事件数（按 event_type label） |
| `rag_doc_reindex_lag_seconds` | 重索引滞后（事件 occurred_at 与处理完成差） |
| `rag_doc_retrieval_total` | 检索次数 |
| `rag_doc_retrieval_latency_seconds` | 检索延迟（向量+rerank+LLM，Histogram） |
| `rag_doc_retrieval_cache_hit_total` | 检索缓存命中 |
| `rag_doc_low_confidence_total` | 置信度 <0.6 转人工次数 |
| `rag_doc_no_citation_total` | 无引用判失败重试次数 |
| `rag_doc_acl_fallback_total` | 降级查询各上下文 REST 次数 |
| `rag_doc_deprecated_leak_total` | 检索结果误含 DEPRECATED（应为 0，告警） |

### 10.2 trace 串联

- 每次检索一个 `trace_id`，OpenTelemetry 在 `VectorRetriever`、rerank、LLM 注入 span，透传到下游 Java 服务（`traceparent` header）。
- `DocAnswer.citations` 的 `chunk_id` + `locator` 让工程师从答案回溯到原文档具体位置。

### 10.3 兜底

- **重索引滞后兜底**：`rag_doc_reindex_lag_seconds` 超阈值 -> 告警 + 检索置信度降权，提示"文档可能未与最新工艺同步"。
- **置信度兜底**：`confidence < 0.6` 或无引用 -> `needs_human_review`，不展示给操作工。
- **LLM 输出兜底**：`DocAnswer` 经 Pydantic 校验，无引用 / 字段缺失判失败重试；重试仍失败转人工。
- **DEPRECATED 泄漏兜底**：`rag_doc_deprecated_leak_total` 应为 0，泄漏即告警（§9.7）。
- **向量库故障兜底**：PGVector 不可用时返回 503，不阻塞 MES 生产；文档库可从 MinIO 原始文件 + 事件回放重建。

---

## 11. 实现步骤

### 阶段一：骨架与最小摄入检索（2 周）

1. 搭 `rag_doc_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 接 PGVector（`pgvector/pgvector:pg16` 镜像），建 §4.2 表结构与 HNSW 索引。
3. 实现摄入管线（unstructured 解析 + LlamaIndex 切分 + bge-m3 向量化 + 同事务持久化，§9.1），跑通一份 SOP 摄入。
4. 实现向量检索 + 元数据过滤（§6.2 SQL），带 `state` + `tenant_scope` + `route_version` 前置过滤。
5. 实现 LLM 综合（§6.4），`DocAnswer` Pydantic 强约束 + 强制引用 + 置信度阈值。
6. FastAPI 端点 `/rag/docs/query` / `/rag/docs/search` / `/rag/docs/ingest`（§9.8）。

### 阶段二：版本治理与重索引（2 周）

7. 实现文档版本状态机（DRAFT/PUBLISHED/PENDING_REBIND/DEPRECATED/ARCHIVED，§4.3）。
8. 实现文档与工艺版本绑定（`DocumentBinding` 值对象，含 `inherited`）。
9. 实现重索引消费者：订阅 `process.route.lifecycle`（`ProcessRouteActivated` / `ProcessRouteDeprecated`），触发 `PENDING_REBIND` / `DEPRECATED` 状态流转 + chunk.state 同步（§9.2）。
10. 接收追溯型 `rag.reindex.request` 事件，按 `route_id` + `route_version` 重索引关联文档。
11. 实现幂等表 + 位点表 + 手动 ack（§5.5，同 PG 事务）。
12. `ReadOnlyIngestionGate` / `assert_no_raw_data_topic` 启动断言（§9.6）。

### 阶段三：Rerank、权限加固与可观测（1–2 周）

13. 接 bge-reranker-v2-m3 精排（§6.3），评测调优 top_k / top_n。
14. 租户过滤在 SQL 前置，验证 `tenant_scope` 不达标看不到 chunk。
15. 接 OpenTelemetry + prometheus 指标（§10.1），`rag_doc_deprecated_leak_total` 告警兜底（§9.7）。
16. 检索结果缓存（redis）按 query + 版本 + 租户去重。

### 阶段四：ACL 降级、评测与协同对接（1–2 周）

17. 接 ACL 降级查询工艺管理只读 REST（§7），强制 `route_version` 入参。
18. 沉淀评测集（典型 SOP / 手册 / 检验标准问答 + 预期引用），回归模型 / 提示词 / 切分变更。
19. 对接追溯型 RAG：`TraceAnswer.suggested_action` 调 `search_docs(query, route_version_filter)`。
20. 对接 L1 Agent：`search_docs` 工具封装 `/rag/docs/query`。
21. 灰度一条产线（设备维修手册 + SOP + 检验标准），收集工程师 / 操作工反馈。

### 阶段五：扩展（按需）

22. 扩展 IPC 标准 / 培训资料（通用知识型，按自身版本管理，不绑 route_version）。
23. 🔴 8D 报告案例型检索（若纳入，需独立切分 / 提示词策略，[详细设计](./文档型 RAG-详细设计.md) §4.1）。
24. 🔴 检验标准独立 `rule_version` 绑定 + 订阅 `quality.gate.lifecycle`（§1.1）。
25. 🔴 工艺升版时 SOP 内容变更自动比对（替代人工 `PENDING_REBIND` 确认）。

---

## 12. 约束落地检查清单

- [ ] 文档元数据 / chunk / 向量 / 幂等 / 位点同库存于 PGVector，摄入与重索引同 PG 事务（§3.1）。
- [ ] 检索 SQL 强制 `state='PUBLISHED'`，`DEPRECATED` / `PENDING_REBIND` 不进候选；`rag_doc_deprecated_leak_total` 应为 0（§9.7）。
- [ ] chunk 表冗余 `state` / `tenant_scope` / `binding_route_version` / `binding_asset_id`，状态流转时同步更新（§5.3）。
- [ ] 文档版本绑定 `route_version`；检索带版本过滤（带则精确历史版本，不带则当前生效）。
- [ ] 订阅 `ProcessRouteActivated` / `ProcessRouteDeprecated` 触发 `PENDING_REBIND` / `DEPRECATED` 状态流转（§9.2）。
- [ ] 接收追溯型 `rag.reindex.request` 事件，与追溯型重索引对齐。
- [ ] 新版本 PUBLISHED 时同类绑定旧版本自动 DEPRECATED，不删（§5.4）。
- [ ] 工艺升版关联文档进入 `PENDING_REBIND` 人工确认，不自动 PUBLISHED（🔴 §4.3）。
- [ ] 租户 `tenant_scope` 在检索 SQL 前置过滤，权限不达标看不到 chunk。
- [ ] `event_id + consumer_group` 幂等表 + `file_content_hash` / `version_id+ordinal` 唯一约束，重复投递 / 重复摄入不产生重复 chunk。
- [ ] 消费者位点落 PG，重启从断点续跑，处理事务成功后才 ack offset。
- [ ] RAG 服务不进过点主事务，文档摄入 / 重索引秒级最终一致，过点 P99 ≤200ms 不受影响。
- [ ] 检索结果 `DocAnswer` 结构化，citations 强制引用 `chunk_id` + `locator`，无引用判失败重试。
- [ ] LLM 输出经 Pydantic `DocAnswer` 校验，失败重试。
- [ ] `confidence < 0.6` 或无引用 -> `needs_human_review`，不展示给操作工。
- [ ] 向量库故障返回 503 不阻塞 MES 生产；文档库可从 MinIO 原始文件 + 事件回放重建。
- [ ] 所有答案带 disclaimer：来自文档型 RAG，处置需按现行 SOP 确认。
- [ ] `ReadOnlyIngestionGate` / `assert_no_raw_data_topic` 启动断言生效（§9.6）。

---

## 13. 面试防守 Q&A

**Q：文档型 RAG 和通用 RAG 有什么本质区别？**
A：不是检索模型差异，是**版本治理**。通用 RAG 检索文档不分版本；MES 的 SOP / 作业指导书绑定 `route_version`，工艺有版本生命周期（[领域总览.md](../../领域模型/领域总览.md) §5.1）。检索到已失效 SOP 会直接导致批量不良。所以我的文档型 RAG 做了三件事：① 文档版本绑定领域版本、有状态机（PUBLISHED/DEPRECATED/PENDING_REBIND）；② 订阅 `ProcessRouteActivated` 触发重索引 / 重新绑定；③ 检索 SQL 强制 `state='PUBLISHED'` + `route_version` 过滤前置。版本一致性不是 RAG 保证的，是从领域模型兜上来的（[RAG服务引入路线.md](../RAG服务引入路线.md) §5 Q&A）。

**Q：为什么选 PGVector 而不是 Milvus / Qdrant？**
A：文档型核心难点是版本 + 权限 + 生效状态组合过滤，PGVector 让元数据和向量同库存，组合过滤走完整 SQL `WHERE` 前置（`state='PUBLISHED' AND tenant_scope=ANY(...) AND binding_route_version=$rv`），pre-filter 后再向量近邻。Milvus / Qdrant 标量过滤表达力弱于 SQL，且元数据与向量常分库、跨库一致性难保证。文档库规模（数百万 chunk）远未到瓶颈，HNSW 检索数十毫秒。比追溯型还省运维--追溯型是 Neo4j + MySQL 双库，文档型全程一个 PG 实例（§2.3）。

**Q：工艺升版了，SOP 怎么跟着变？**
A：订阅 `ProcessRouteActivated`，关联文档进入 `PENDING_REBIND` 待确认状态（同步 chunk.state，检索立即不再召回），由文档 owner 人工判断：内容需变则发新版本绑定新 `route_version`；内容不变则继承绑定（`inherited=true`）。**不自动 PUBLISHED**--"SOP 是否需变"不能靠机器假设，宁可让人确认。旧版本 DEPRECATED 不删，历史答案引用仍可点开（§9.2）。

**Q：会不会拖慢过点？**
A：不会进过点主事务。过点 P99 ≤200ms 是硬约束，文档摄入 / 重索引是异步消费事件，与过点判定完全解耦。文档检索容忍秒级延迟--它是事后 / 旁边问答工具，不是实时过点判定。

**Q：文档型 RAG 和追溯型 RAG 是不是重复了？**
A：不重复，互补分工。追溯型是 GraphRAG + 领域事件流，答"这条单件 5M1E 全貌"（哪批锡膏、哪台设备），处理结构化关系；文档型是向量检索，答"SPI 报警怎么处置"（处置知识），处理非结构化文档。两者共享同一套版本契约--都由 `ProcessRouteActivated` 驱动，追溯型把版本做成图节点，文档型把版本做成文档元数据过滤维度。追溯型的 `suggested_action` 调文档型补 SOP 片段，协同闭环（[详细设计](./文档型 RAG-详细设计.md) §6.6）。

**Q：LLM 编造文档里没有的内容怎么办？**
A：两道防线。一是系统提示词约束"只能基于提供的 chunks 回答，无相关文档则回答'未找到'"；二是 `DocAnswer` 强制引用 `chunk_id`，无引用的答案 Pydantic 校验失败、判失败重试。每个引用带 `locator`（页码/偏移），工程师能点开原文核对。低置信度转人工，不硬答（§6.4）。

**Q：失效 SOP 会不会被检索到？**
A：三道闸兜住。① 摄入闸：文档版本有状态机，PUBLISHED 才可检索；② 重索引闸：`ProcessRouteDeprecated` 触发 DEPRECATED，同步 chunk.state；③ 检索闸：SQL 强制 `state='PUBLISHED'`，DEPRECATED / PENDING_REBIND 不进候选。还有 `rag_doc_deprecated_leak_total` 指标应为 0，泄漏即告警，双保险（§9.7）。

**Q：文档库错了或漏了怎么办？**
A：文档库归 RAG 自有，错了不影响 MES 生产--事实源是 MinIO 原始文件 + 领域版本，PGVector 崩溃返回 503 不阻塞过点。重建靠 MinIO 原始文件 + 事件回放，无需 MES 配合。所有答案带引用 + 置信度，低置信度转人工，与 MES 防错理念一致。

**Q：上线了吗？**
A：这是设计阶段的引入规划，不是已落地。重点是版本治理对齐工艺生命周期、PGVector 选型用 SQL 前置过滤兜版本+权限、重索引由领域事件驱动这三条架构判断。落地按"先 B 后 A"推进--文档型 2–4 周可出 demo，先验证车间可用性（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。诚实 + 体现架构判断力，比硬吹"已上线文档 RAG"得分高。

---

## 14. 一句话定位

"文档型 RAG 把车间的 SOP / 手册 / 检验标准做成向量知识库--向量是主体、版本治理是灵魂：文档版本绑定 `route_version`、状态机对齐工艺生命周期、订阅 `ProcessRouteActivated` 驱动重索引、检索 SQL 强制 `state='PUBLISHED'` + 版本前置过滤，从结构上杜绝答出已失效 SOP 导致批量不良。选 PGVector 让版本/权限/生效状态组合过滤走 SQL `WHERE` 前置、全程单库同事务，与追溯型 RAG 共享同一套版本契约、互补分工--追溯型答'是什么、为什么'，文档型答'怎么办'，全程只读 MES、不进过点主事务，低置信度转人工，让 RAG 从旁边问答变成能进车间的处置知识副驾。"
