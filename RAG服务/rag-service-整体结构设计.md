# rag-service 整体结构设计

> 本文是 rag-service（单一 Python 微服务）的**整体结构设计**，位于 A/B/E 三路线详细设计/实现方案之上的一层：
> - 定义**跨路线共享的内核**（LLM / Embedding / 可观测 / 配置 / Kafka / ACL / 持久化 / 租户 / Web 底座）；
> - 定义**单服务骨架**（FastAPI 统一入口、三路线模块、启动断言、健康检查、DI、迁移）；
> - 定义**路线间调用与对外集成契约**（Port/Adapter，保留可拆性）。
>
> 各路线内部的领域建模、Cypher/向量检索细节、事件投影 handler 清单，仍以各路线详细设计/实现方案文档为准；本文只规定它们在单服务内的**组织方式与共享边界**。
>
> 与 [整体技术选型与模块划分.md](../整体技术选型与模块划分.md) §1.2/§3.2 对齐：rag-service 是**一个 Python 微服务**，A/B/E 三路线作为模块共存，**全程只读**（图是事件流的只读投影）。
>
> **范围说明**：C 数据型、D 防错即时辅助两条路线暂不建设（descoped），不在本文档范围内。

---

## 0. 设计取舍

| # | 取舍 | 理由 |
|---|------|------|
| 1 | **模块化单体 + 共享内核（Shared Kernel）** | 三路线运行时 profile 同质（均为 FastAPI + 事件订阅 + 检索/编排），合并单服务可消除各路线文档中重复的 `llm_factory` / `bge_client` / `obs` / `config` / `kafka` 基类；与总览 §1.2 单服务表述一致 |
| 2 | **可拆性是硬约束** | 每路线模块自包含（自带 application/domain），依赖只指向 `shared/` 的 Protocol（Port），路线间调用走 Port/Adapter；未来任一路线可零成本抽成独立微服务（strangler），只需把 InProcess Adapter 换成 Http Adapter |
| 3 | **多存储共存于单进程** | A 用 Neo4j、B 用 ChromaDB、元数据/审计用 MySQL、缓存用 Redis；单服务集中了存储依赖，但每条存储链路在 `shared/persistence/` 与各路线 `infrastructure/` 间用 Port 隔离，故障域不扩散（如 Neo4j 不可用返回 503，不拖垮 B/E） |
| 4 | **只读红线靠启动断言兜底，不靠自觉** | 统一的 `ReadOnly*Gate` 体系（投影/摄入/工具三类 gate）在 lifespan 启动期扫描，发现任何写动作/原始数据流订阅即拒绝启动 |

> **不选多服务**：三路线基础设施 90% 同构（同 LLM 抽象、同 bge-m3、同 OTel/prometheus、同 pydantic-settings、同 Kafka envelope/幂等模式），拆三服务会把这 90% 复制三份，违背 DRY 与 SRP；总览 §1.2 也只列了一个 rag-service。

---

## 1. 限界上下文与模块边界

### 1.1 模块上下文清单

rag-service 内部划分为 **4 个模块上下文**：3 个路线业务上下文 + 1 个共享内核。

| 模块上下文 | 包路径 | 类型 | 职责 |
|-----------|--------|------|------|
| 追溯型（A） | `routes/traceability/` | 业务上下文 | GraphRAG 全链路追溯，5M1E 根因串联 |
| 文档型（B） | `routes/document/` | 业务上下文 | SOP/手册/标准/8D 向量检索 + 事件驱动重索引 |
| Agentic RAG（E） | `routes/agentic/` | 业务上下文 | L0 收口入口，意图路由到 A/B + 委托 agent-service L1/L2 |
| 共享内核 | `shared/` | 技术内核 | LLM/Embedding/obs/config/kafka/acl/persistence/tenant/web 基础抽象与实现 |

### 1.2 上下文映射（Context Map）

```
                      ┌─────────── shared（Shared Kernel，U）───────────┐
                      │  ai  embedding  obs  config  kafka  acl          │
                      │  persistence  tenant  web  events                │
                      └──┬───────┬───────────────────┬──────────────────┘
                         │       │                   │
            ┌────────────┘       │                   └────────────┐
            ▼                    ▼                                ▼
       traceability(A)      document(B)                      agentic(E)
            │                    ▲                                │
            │  suggested_action  │ (DocRagPort)                   │
            │ ───────────────────┘                                │
            │                                                      │
            │      rag.reindex 事件                                 │
            └───────────────────────────►B                         │
                                                                   │
            E 统一入口调 A/B（Port）◄───────────────────────────────┤
            E 委托 agent-service L1/L2（httpx REST）◄────────────────┘
```

- **shared -> 三路线**：Shared Kernel（U/D），三路线依赖 shared 的抽象与实现，反向禁止。
- **A -> B**：客户/供应商，A 的 `TraceAnswer.suggested_action` 经 `DocRagPort` 拉 B 的 SOP 片段；A 升版时发 `rag.reindex.request` 内部事件通知 B 重索引。
- **E -> A / B**：客户/供应商，E 经 Port 调两路线；E -> agent-service L1/L2 经 httpx REST 委托。
- **路线间禁止直接 import 对方的 domain/application**：一律走 `shared/acl/` 的 Port（见 §3.6），这是可拆性的关键。

### 1.3 与外部上下文的边界

| 外部上下文 | 关系 | 集成方式 |
|-----------|------|---------|
| MES 14 限界上下文（Java） | 只读消费者 | Kafka 领域事件（投影/重索引）+ 只读 REST（降级补齐） |
| agent-service（L1/L2/L3） | 被调用方（A/B 被 L1/L2 调）+ 调用方（E 委托 L1/L2） | httpx REST，W3C `traceparent` 透传 |
| mes-eval | 被测对象 | EvalTarget 适配器（见 §10） |
| MinIO（基础设施） | 对象存储客户端 | B 的原始文档文件存储 |

---

## 2. 顶层目录结构

```text
rag_service/
├── app/
│   ├── api/                              # FastAPI 路由层（统一注册 + 横切）
│   │   ├── __init__.py                   # register_routers(app) 汇总三路线 router
│   │   ├── deps.py                       # 依赖注入 provider（get_*_service）
│   │   ├── middleware.py                 # TenantMiddleware / RequestLogMiddleware
│   │   ├── errors.py                     # 全局 exception_handler
│   │   └── v1/
│   │       ├── trace_router.py           # A: POST /rag/trace/query, /rag/trace/expand
│   │       ├── doc_router.py             # B: POST /rag/docs/{query,search,ingest}
│   │       └── chat_router.py            # E: POST /agent/chat, GET /agent/explain/{id}
│   │
│   ├── routes/                           # 三路线模块（各含 application/domain/infrastructure）
│   │   ├── traceability/                 # A -- 详见《追溯型 RAG-详细设计/实现方案》
│   │   ├── document/                     # B -- 详见《文档型 RAG-详细设计/实现方案》
│   │   └── agentic/                      # E -- 详见《Agentic RAG-详细设计/实现方案》
│   │
│   ├── shared/                           # 共享内核（技术底座，见 §3）
│   │   ├── ai/                           # LLM 抽象（ObservableChatModel + llm_factory + LlmPort）
│   │   ├── embedding/                    # Embedding 抽象（EmbeddingPort + bge + reranker）
│   │   ├── obs/                          # 可观测底座（context/port/tracing/metrics/logging/redactor）
│   │   ├── config/                       # 统一配置（BaseSettings + RagSettings 聚合）
│   │   ├── kafka/                        # 事件消费基类（ConsumerGroup + envelope + 幂等/位点）
│   │   ├── acl/                          # ACL 基类 + 路线间 Port/Adapter + MES 只读客户端
│   │   ├── persistence/                  # 多 Engine 工厂 + DeclarativeBase + Alembic
│   │   ├── tenant/                       # TenantContext + 依赖注入 + 跨服务传递协议
│   │   ├── web/                          # lifespan + health + DI 容器
│   │   └── events/                       # 版本契约（VersionAnchor: route/bom/rule/asset/standard）
│   │
│   ├── config.py                         # 入口 Settings 加载
│   └── main.py                           # FastAPI app + lifespan + register_routers
│
├── alembic/                              # 迁移脚本（统一管理多 schema，见 §9）
├── tests/
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```

> 各路线 `routes/<route>/` 内部的 `application/domain/infrastructure` 三层结构，与各路线现有实现方案文档的包结构**一一投影**（如 A 的 `infrastructure/neo4j/`、`projections/`、`acl/` 原样保留），只是顶层从"独立服务根"挪到"单服务下的路线模块"。迁移映射见 §11。

---

## 3. 共享内核（shared/）设计

共享内核是三路线的 Shared Kernel，**只放被 ≥2 条路线复用的抽象与实现**；仅单路线使用的设施留在该路线 `infrastructure/` 下。

### 3.1 `shared/ai/` -- LLM 抽象

| 组件 | 职责 |
|------|------|
| `LlmPort`（Protocol） | LLM 抽象接口，业务层依赖它而非具体 provider（DIP） |
| `ObservableChatModel` | 包装任意 `langchain-core` `BaseChatModel`，统一埋 token/延迟/模型/`prompt_version`，provider 无关 |
| `llm_factory` | 按 config 创建 `ObservableChatModel`，适配 Claude/通义千问/DeepSeek/本地模型；模型可插拔 |

> 现状：A/B/E 三份实现方案各自定义了 `llm_factory.py`。本文将其上移到 `shared/ai/`，各路线删本地副本、改 import。降本组件（`ModelRouter`/`EvalGate`/`CacheControl` 等）属 agent-service 成本优化横切，rag-service 仅在 E（LangGraph 路由图）内按需引用，不进 shared。

### 3.2 `shared/embedding/` -- Embedding 抽象

| 组件 | 职责 |
|------|------|
| `EmbeddingPort`（Protocol） | 向量化抽象，A（缺陷描述语义入口）/ B（文档向量化主体）两路线共用 |
| `BgeClient` | bge-m3 1024 维实现，批量推理；A 的 DefectCatalog `name_embedding` 也走它 |
| `BgeReranker` | bge-reranker-v2-m3 cross-encoder 精排（B 用） |

> 选型理由见各路线文档：bge-m3 多语种、可本地化、1024 维 cosine。

### 3.3 `shared/obs/` -- 可观测底座

对齐 agent-service 可观测方案，rag-service 复用同一套五层可观测模型与 trace 双存储。

| 组件 | 职责 |
|------|------|
| `ObservabilityContext` | 不可变 dataclass，随会话流动（`session_id`/`trace_id`/`tenant`/`route`/`prompt_version`/`step_no`） |
| `ObservabilityPort`（Protocol） | 抽象接口，业务节点依赖它而非 OTel/prometheus 具体实现（DIP） |
| `Tracing` | OTel span 封装（`session_span`/`retrieval_span`/`projection_span`/`llm_span`） |
| `MetricsCollector` | Counter/Histogram 集中定义；统一指标前缀 `rag_`（如 `rag_trace_query_duration_seconds`、`rag_doc_search_hits`、`rag_projection_lag_ms`） |
| `LoggingContext` | structlog + JSONRenderer，自动注入 `trace_id`/`span_id` |
| `Redactor` | 脱敏纯函数（序列号保留前 4 后 2、物料批次白名单、PII 不采集） |

> Trace 双存储：Tempo/Jaeger（SRE 火焰图）+ MySQL 平铺表（工程师 UI 证据链回溯），同源 `trace_id` 串联 agent-service 与 MES。观测是只读旁路，失败不反噬业务（图库崩返回 503，不影响 MES 过点）。

### 3.4 `shared/config/` -- 统一配置

| 组件 | 职责 |
|------|------|
| `BaseSettings` | 公共配置项：Kafka/MySQL/Redis/LLM/Embedding/OTel，环境变量命名统一前缀 `RAG_` |
| `RagSettings` | 聚合各路线子配置（`TraceSettings`/`DocSettings`/`AgenticSettings`），按路线开关启停（如 `rag.agentic.enabled=false` 则不注册 E 的 router） |

> 现状：三路线各自 `config.py`。本文上移公共项到 `BaseSettings`，各路线子配置作为 `RagSettings` 的嵌套字段。路线级开关支持灰度引入（先 B 再 A，E 收口）。

### 3.5 `shared/kafka/` -- 事件消费基类

| 组件 | 职责 |
|------|------|
| `DomainEvent` | 领域事件 envelope 公共定义（`event_id`/`event_type`/`event_version`/`occurred_at`/`source_service`/`trace_id`/`partition_key`），对齐 [消息处理实现说明.md](../实现说明/业务事件/消息处理实现说明.md) §4.3 |
| `ConsumerGroup` | aiokafka 消费者基类：手动 ack + 位点落 MySQL + 消费者组按主题前缀分组 |
| `IdempotencyRepo` | `event_id` 幂等表操作基类（A/B 两路线共用模式） |
| `OffsetRepo` | 消费者位点表操作基类 |
| `ProjectionHandler`（Protocol） | 事件 -> 投影 handler 协议（ISP），A 的图投影 / B 的重索引都实现它 |

> 现状：A/B 两路线各自实现 `consumer_group.py`/`listeners.py`/`idempotency_repo.py`/`offset_repo.py`，逻辑高度同构。本文上移基类到 `shared/kafka/`，各路线只保留自己的 `handlers/`（事件 -> 投影动作映射）。

### 3.6 `shared/acl/` -- ACL 基类 + 路线间 Port/Adapter

**这是可拆性的核心。** 分两类：

**(a) 对 MES 只读 REST 的 ACL（出站）**

| 组件 | 职责 |
|------|------|
| `BaseReadonlyAclClient` | httpx 异步基类：自动注入 `traceparent`、超时/重试、租户 header、只读断言（方法名禁止写动词） |
| `MesClients` | 对 MES 14 上下文只读 REST 的客户端集合（工艺/物料/质量/过点/设备…），A/B 共享（降级补齐用） |

**(b) 路线间调用的 Port/Adapter（可拆性关键）**

| Port（Protocol） | 默认 Adapter（单服务内） | 拆服务后 Adapter | 调用方 |
|-----------------|----------------------|----------------|--------|
| `TraceRagPort` | `InProcessTraceRagAdapter`（直调 A 的 `TraceRetrievalService`） | `HttpTraceRagAdapter`（httpx -> rag-service A） | E |
| `DocRagPort` | `InProcessDocRagAdapter`（直调 B 的 `DocumentRetrievalService`） | `HttpDocRagAdapter`（httpx -> rag-doc-service） | A、E |

> 规则：路线间**禁止直接 import 对方的 application/domain**，一律依赖 `shared/acl/` 的 Port。单服务模式下 DI 容器注入 InProcess Adapter；未来拆服务只需把绑定换成 Http Adapter，业务代码零改动。这把"现在单服务、将来可拆"从口号变成结构属性。

### 3.7 `shared/persistence/` -- 多存储 Engine 工厂 + 迁移

| 组件 | 职责 |
|------|------|
| `db` | AsyncEngine 工厂：MySQL 主库（元数据/审计/幂等/位点）+ ChromaDB persistent client（B 的向量库），按 config 懒初始化 |
| `base` | SQLAlchemy 2.0 `DeclarativeBase`（async + asyncmy，MySQL） |
| `migrations/` | Alembic 统一管理多 schema（见 §9） |

> 现状：各路线用原始 SQL DDL，无迁移工具。本文引入 Alembic 统一管理（§9）。
> 多 DB 注意：单进程同时持有 Neo4j driver + ChromaDB client + MySQL asyncmy + Redis client，连接池分别配额，lifespan 启动期做就绪探测，任一不可用按路线降级（不拖垮其他路线）。

### 3.8 `shared/tenant/` -- 租户上下文 + 跨服务传递

| 组件 | 职责 |
|------|------|
| `TenantContext` | 租户上下文（`tenant_id`/`scopes`: workshop/line 列表） |
| `dependency` | FastAPI 依赖：`tenant_from_token`（从 JWT 解析）/ `tenant_from_header`（从 `X-Tenant-Scope` 解析） |
| `propagation` | 跨服务传递协议：出站 httpx 自动注入 `X-Tenant-Scope` header；Kafka 消费时从事件 envelope metadata 还原 |

> 现状缺口：各路线各自定义 `TenantContext` 与过滤逻辑（A 用 Cypher `WHERE`、B 用 SQL `WHERE`），但**未定义跨服务传递协议**。本文统一：A/B/E 共用一个 `TenantContext`，传递协议在 `shared/tenant/propagation.py` 一处定义。

### 3.9 `shared/web/` -- FastAPI 公共底座

| 组件 | 职责 |
|------|------|
| `lifespan` | 启动断言编排（见 §7）+ 各存储就绪探测 + 按路线开关启停 consumer/router |
| `health` | `GET /health`（进程存活）+ `GET /ready`（Neo4j/PG/MySQL/Redis/Kafka 连通性 + 各 consumer 组位点滞后度） |
| `container` | DI 容器：注册 LLM/Embedding/各 Port 的 Adapter 绑定，`deps.py` 从容器取实例 |

> 现状缺口：三路线均无健康检查端点、无统一 router 注册、无 DI 容器。本文一次性补齐。

### 3.10 `shared/events/` -- 版本契约

| 组件 | 职责 |
|------|------|
| `version_contract` | `VersionAnchor(kind, ref_id, version)` 统一版本锚点（route/bom/rule/asset/standard）；`ProcessRouteActivated` 驱动的版本失效事件 -> A 重投图 / B 重索引的统一入口 |

> 版本一致性三段传递链（核心安全契约）：`图 SNAPSHOT_OF_{kind}{version} -> L1 evidence.version_anchor -> L2 Draft.version_anchor -> MES 应用服务校验 ACTIVE`。rag-service 侧负责第一段（图用快照边物理锁定版本）+ 发布 `rag.reindex.request` 内部事件通知 B。

---

## 4. 三路线模块设计

各路线模块内部结构沿用其详细设计/实现方案文档，此处只给**模块边界 + 对外端点 + 与 shared 的依赖关系**的速查表。

### 4.1 路线 A 追溯型（`routes/traceability/`）

| 层 | 关键组件 | 依赖 shared |
|----|---------|------------|
| domain | `TraceSubgraph`/`TraceNode`/`TraceEdge`/`FiveM1ECluster`、`TraceAnswer`/`RootCauseHypothesis`/`FiveM1ECategory`、`Seed`、`ProjectionHandler`/`GraphProjector`/`ReadOnlyProjectionGate` | tenant |
| application | `TraceRetrievalService`、`SeedResolver` | ai、embedding、obs |
| infrastructure | `neo4j/`(driver/schema/retriever)、`projections/`(各上下文投影 handler)、`acl/`(降级 MES 只读 REST) | kafka、persistence、acl、obs |

**对外端点**：`POST /rag/trace/query`（检索+综合）、`POST /rag/trace/expand`（只取子图不综合）。
**存储**：Neo4j（图主体 + DefectCatalog 向量索引）、MySQL（幂等/位点/审计）、Redis（子图缓存）。
**GraphProjector** 订阅：`mes.checkpoint.lifecycle`/`mes.testresult.structured`/`mes.routing.progress`/`process.route.lifecycle`/`material.*`/`quality.*`（MVP 4 上下文，详见 A 实现方案 §5.1）。

### 4.2 路线 B 文档型（`routes/document/`）

| 层 | 关键组件 | 依赖 shared |
|----|---------|------------|
| domain | `KnowledgeDocument`(聚合根)/`DocumentVersion`/`DocumentChunk`/`DocumentBinding`/`ChunkLocator`、`DocAnswer`/`DocCitation`、`ReindexHandler`/`ReadOnlyIngestionGate` | tenant |
| application | `DocumentIngestionService`、`DocumentRetrievalService`、`ReindexCoordinator`、`ChunkStrategySelector` | ai、embedding、obs |
| infrastructure | `chromadb/`(client/schema/retriever/document_repo)、`minio_/`(object_store)、`acl/` | kafka、persistence、acl、obs |

**对外端点**：`POST /rag/docs/query`（检索+综合）、`POST /rag/docs/search`（只检索 chunks）、`POST /rag/docs/ingest`（文档摄入）。
**存储**：ChromaDB（chunk 向量 + metadata，chunk 不可变 + 版本隔离靠查询过滤）、MinIO（原始文件）、MySQL（幂等/位点/审计）、Redis（检索缓存）。
**重索引触发**：订阅 `process.route.lifecycle` + A 发布的 `rag.reindex.request` 内部事件。
**审核流**：工艺绑定型文档（SOP/检验标准）随 `ProcessRouteActivated` **联动 PUBLISHED**（工艺生效即文档生效，责任归工艺 owner，决策 #3）；通用知识型/设备绑定型仍走独立 DRAFT->PUBLISHED。
**版本绑定**：MVP 工艺绑定型按 ROUTE 锚点（`version_kind="route"`），`VersionAnchor` 统一覆盖 route/bom/rule/asset/standard，评测后可切 RULE 锚点（决策 #2）。

### 4.3 路线 E Agentic RAG（`routes/agentic/`）

| 层 | 关键组件 | 依赖 shared |
|----|---------|------------|
| domain | `IntentCategory`、`AgentAnswer`/`AnswerSource`、`ToolDescriptor`/`ToolRegistry`/`ReadOnlyToolGate` | tenant |
| application | `GatewayService`、`IntentRouter` | ai、obs |
| infrastructure | `ai/route_graph_builder`(LangGraph 轻量路由图，`recursion_limit=6`)、`acl/`(trace_rag/doc_rag/l1_delegation/l2_delegation) | acl、persistence、obs |

**对外端点**：`POST /agent/chat`（统一问答入口）、`GET /agent/explain/{audit_id}`（回溯路由与工具链）。
**存储**：MySQL（答案审计 `answer_audit`/路由 trace `route_trace`）、Redis（查询缓存）。
**调用方式**：A/B 经 `shared/acl/` Port（单服务内 InProcess Adapter）；L1/L2 委托经 httpx REST（`POST /agent/diagnose`、`POST /agent/draft`，透传 `traceparent`）。
**双角色**：E 既是 rag-service 的路线 E（收口 A/B），又是 agent-service 分层的 L0（见总览 §4.1）；本服务只实现其"路由 + 轻量组合 + 委托"部分，深度多步推理仍委托 L1。

---

## 5. 对外接口契约汇总

| 路线 | 端点 | 方法 | 用途 | 主要调用方 |
|------|------|------|------|-----------|
| A | `/rag/trace/query` | POST | 子图检索 + LLM 综合，返回 `TraceAnswer`（含 `subgraph_ref`） | agent-service L1（`query_traceability_graph` 工具） |
| A | `/rag/trace/expand` | POST | 只取子图不综合，返回 `TraceSubgraph` | L1、E、工程师 UI |
| B | `/rag/docs/query` | POST | 检索 + LLM 综合，返回 `DocAnswer` | L1/L2（`search_docs` 工具）、E |
| B | `/rag/docs/search` | POST | 只检索 chunks，返回 `list[ChunkHit]` | L1、E |
| B | `/rag/docs/ingest` | POST | 文档摄入（管理接口） | 文档管理员 |
| E | `/agent/chat` | POST | 统一问答入口，返回 `AgentAnswer` | 责任人卡片 / 前端 |
| E | `/agent/explain/{audit_id}` | GET | 回溯路由与工具链 | 审计/工程师 UI |
| shared | `/health` | GET | 进程存活 | K8s liveness |
| shared | `/ready` | GET | 依赖连通性 + consumer 位点滞后 | K8s readiness |
| shared | `/metrics` | GET | prometheus 指标 | Prometheus |

> 所有业务端点均经 `TenantMiddleware` 注入 `TenantContext`，出站自动透传 `X-Tenant-Scope` + `traceparent`。

---

## 6. 模块间集成点

### 6.1 路线间调用（进程内 Port/Adapter）

| 调用方 -> 被调方 | Port | 单服务内路径 | 说明 |
|-----------------|------|------------|------|
| A -> B | `DocRagPort` | `TraceRetrievalService` -> `InProcessDocRagAdapter` -> `DocumentRetrievalService` | A 的 `suggested_action` 拉 SOP 片段，带 `version_anchor` |
| A -> B（事件） | `rag.reindex.request` | A 发布 -> B 的 `ReindexCoordinator` 消费 | 工艺升版触发 B 重索引 |
| E -> A/B | `TraceRagPort`/`DocRagPort` | `GatewayService` -> InProcess Adapter -> 各路线 service | E 不自己多步推理，轻量组合 `recursion_limit=6` |

> 拆服务时：把 DI 容器里 Port -> InProcess Adapter 的绑定换成 Port -> Http Adapter，业务代码零改动。

### 6.2 与 agent-service 的集成

| 集成点 | 方向 | 契约 |
|--------|------|------|
| L1 调图 | Agent -> rag-service (A) | `query_traceability_graph` 工具封装 `POST /rag/trace/query`，注册在 L1 ToolRegistry 首位 |
| L2 回查图 | Agent -> rag-service (A) | `fetch_subgraph_nodes(subgraph_ref)` -> `POST /rag/trace/expand`，L2 不重查图 |
| L1/L2 调文档 | Agent -> rag-service (B) | `search_docs(query, version_anchor)` -> `POST /rag/docs/query` |
| E 委托 L1/L2 | rag-service (E) -> agent-service | `POST /agent/diagnose`（60s）、`POST /agent/draft`（30s），透传 `traceparent` |

> **traceparent 全链路**（决策 #1）：E 委托 L1 时手动注入 `traceparent`；L1 `main.py` 挂 `opentelemetry-instrumentation-fastapi` 为硬要求，接收 incoming `traceparent` 并续接 trace，出站 httpx instrumentation 自动透传到 A/B/MES。L1 实现方案需补 instrumentation 接入。

### 6.3 与 MES 的集成（只读）

- **Kafka 只读事件**：`GraphProjector`(A) / `ReindexCoordinator`(B) 订阅各上下文 Outbox 事件，幂等消费（`event_id`）+ 位点落 MySQL。
- **只读 REST 降级**：图投影滞后或需聚合计算时，A 经 `MesClients` 调各上下文只读 REST 补齐。
- **单向只读**：rag-service 从不回写 MES；图库崩返回 503 不阻塞生产。

### 6.4 subgraph_ref 与版本一致性传递链

```
图 SNAPSHOT_OF_{kind}{version}（A，物理锁定版本；MVP 锁 route）
  -> L1 evidence.version_anchor（透传）
  -> L2 Draft.version_anchor（锁定）
  -> MES 应用服务校验 ACTIVE（最后一道）
```

rag-service 侧：A 用快照边把版本一致性变成结构属性；A 升版发 `rag.reindex.request` 通知 B 重索引。版本锚点统一为 `VersionAnchor(kind, ref_id, version)`，覆盖 route/bom/rule/asset/standard。

---

## 7. 启动断言与只读红线

统一的 `ReadOnly*Gate` 体系，在 `shared/web/lifespan` 启动期扫描，任一失败即拒绝启动（fail-fast）。

| Gate | 归属 | 断言内容 |
|------|------|---------|
| `ReadOnlyProjectionGate` | A | 图投影 handler 禁止 `DELETE`/`REMOVE`/历史覆盖性 `SET` |
| `RawDataTopicGate` | A | 消费者组禁止订阅 `dc.*` 原始数据流（高频采集不全量入图） |
| `ReadOnlyIngestionGate` | B | 摄入/重索引 handler 禁止任何写 MES 调用 |
| `ReadOnlyToolGate` | E | ToolRegistry 拒绝注册 `read_only=False` 的工具 |
| `BaseReadonlyAclClient` 方法名扫描 | shared | 所有 ACL 客户端方法名禁止写动词（create/update/delete/...） |

> 启动断言是把"只读旁路"从约定变成结构属性：最坏情况是"没检索出来"，不会产生写副作用。

---

## 8. 部署与运行时

- **进程模型**：单进程，`uvicorn + gunicorn` worker；A/B/E 均为请求-响应型，共享 worker 池。
- **K8s**：独立微服务 `rag-service`，liveness probe = `/health`，readiness probe = `/ready`，按路线 QPS 配 HPA（A/B/E 合一指标）。
- **路线级开关**：`RagSettings` 的 `rag.<route>.enabled` 控制 router 注册与 consumer 启停，支持灰度引入（先 B -> A -> E 收口）。
- **故障域隔离**：Neo4j/PG/MySQL/Redis 任一不可用，对应路线降级（返回 503），不拖垮其他路线。
- **可拆触发条件**（未来）：某路线 QPS/数据量单独增长到需要独立伸缩（如 A 的图投影 consumer 与 B 的检索争抢资源），按 §3.6 换 Http Adapter 即可拆出，业务代码零改动。

---

## 9. 数据库迁移与 Schema 管理

引入 **Alembic** 统一管理（现状缺口：各路线用原始 SQL DDL，无迁移工具）。

| schema | 归属路线 | 内容 |
|--------|---------|------|
| `rag_shared` | shared | `DomainEvent` 幂等/位点基表（若统一） |
| `rag_trace` | A | `index_idempotency`/`index_offset`/`subgraph_audit` |
| `rag_doc` | B | ChromaDB collection（chunk 向量+metadata，非 Alembic）；幂等/位点归 `rag_shared` |
| `rag_agentic` | E | `answer_audit`/`route_trace` |

- **MySQL**（A/E 元数据与审计）：Alembic 管理 schema 演进。
- **ChromaDB**（B）：非 Alembic，collection 由代码初始化，Parquet 文件持久化（chunk 不可变）。
- **Neo4j**（A）：`SchemaInitializer` 启动时幂等执行约束/索引/向量索引 DDL（非 Alembic，图库 DDL 幂等即可）。

---

## 10. 评测接入（mes-eval）

rag-service 是 mes-eval 的被测对象，3 条路线各一个 `EvalTarget` 适配器（见总览 §3.3）：

| 被测对象 | 适配器 | 评测入口 |
|---------|--------|---------|
| A 追溯型 | `mes_eval/infrastructure/targets/traceability_rag.py` | 调 `POST /rag/trace/query`，断言 5M1E 召回 + 证据回溯 + 版本锚点 |
| B 文档型 | `doc_rag.py` | 调 `POST /rag/docs/query`，断言忠实度/答案相关性 + 版本过滤 |
| E Agentic | `agentic_rag.py` | 调 `POST /agent/chat`，断言路由准确率 + 工具链正确性 |

> 版本锚定贯穿评测全程：每条金标准用例钉死版本锚点（`version_kind`+`version`+`version_ref_id`），`VersionAnchorChecker` 强制比对。安全红线（失效工艺泄漏/写越界/租户越权/PII/实体幻觉/证据空）任一非 0 阻断 CI。

---

## 11. 与各路线详细设计文档的关系

本文档定义**跨路线共享结构 + 单服务骨架**；各路线详细设计/实现方案文档定义**路线内部细节**。迁移映射：

| 各路线文档原位置 | 本文档新位置 | 动作 |
|----------------|------------|------|
| `rag_service/app/api/trace_router.py` 等 | `app/api/v1/<route>_router.py` | 路由层上移到统一 `api/v1/` |
| 各路线 `app/application/` `app/domain/` | `app/routes/<route>/application/` `domain/` | 原样下沉到路线模块 |
| 各路线 `infrastructure/ai/llm_factory.py` | `app/shared/ai/llm_factory.py` | **上移合并**，删本地副本 |
| 各路线 `infrastructure/embedding/bge_client.py` | `app/shared/embedding/bge_client.py` | **上移合并** |
| 各路线 `infrastructure/obs/` | `app/shared/obs/` | **上移合并** |
| 各路线 `config.py` | `app/shared/config/`（公共）+ 路线子配置 | **拆分合并** |
| 各路线 `infrastructure/kafka/consumer_group.py`/`idempotency_repo.py`/`offset_repo.py` | `app/shared/kafka/` | **上移基类**，各路线只留 `handlers/` |
| 各路线 `infrastructure/acl/`（对 MES） | `app/routes/<route>/infrastructure/acl/`（路线专属）+ `app/shared/acl/MesClients`（公共） | 公共部分上移 |
| 路线间互调的 httpx 客户端 | `app/shared/acl/` Port + InProcess/Http Adapter | **重构为 Port/Adapter** |
| 各路线 `main.py` | `app/main.py`（统一）+ `app/shared/web/lifespan` | **合并**，加健康检查/DI/router 注册 |

> 后续动作：各路线详细设计/实现方案文档需补一句"包结构投影到 rag-service 整体结构设计 §2/§11，`llm_factory`/`embedding`/`obs`/`config`/`kafka` 基类见 §3"，避免文档间矛盾。此项不在本文档范围内，列为待办。

---

## 12. 决策记录

以下 4 项原为各路线文档标记为 🔴 的待对齐项，2026-07-13 经评审定稿（仅 #2 的 rule_version 切换时机为评测后触发）：

| # | 议题 | 决策 | 落地影响 |
|---|------|------|---------|
| 1 | E 委托 L1 的 `traceparent` 透传 | L1 `main.py` 挂 `opentelemetry-instrumentation-fastapi` 为硬要求，接收 incoming `traceparent` 并续接 trace；出站 httpx instrumentation 已有 | E -> L1 -> A/B/MES 全链路串联；L1 实现方案需补 instrumentation 接入 |
| 2 | B 检验标准文档版本绑定 | MVP 工艺绑定型按 ROUTE 锚点（`version_kind="route"`），`VersionAnchor` 统一覆盖 route/bom/rule/asset/standard，评测后可切 RULE 锚点 | `routes/document/domain/document.py` 双轨字段；`shared/events` 增 quality.gate 事件 |
| 3 | B 工艺绑定型文档审核流 | **联动 PUBLISHED**：`ProcessRouteActivated` 直接把关联文档置 PUBLISHED，工艺生效即文档生效，责任归工艺 owner（**与 B 详细设计默认"独立审核"相反，需同步修订 B 详细设计 §4.3**） | `routes/document/application/reindex_coordinator.py` 改为自动 PUBLISHED；去掉 SUBMITTED 人工确认中间态 |
| 4 | 单服务内 E 调 A/B 的方式 | 走 InProcess Adapter（直调 application service），不走本机 REST；外部契约（A/B 对 agent-service）由 L1/L2 工具封装对齐 | `shared/acl/` 注入 InProcess Adapter |

> 已定稿决策不再标 🔴。#3 反转了 B 详细设计默认，需同步修订 B 路线文档。
> **范围决策**：C 数据型、D 防错即时辅助两条路线暂不建设（descoped），原涉 C/D 的待对齐项（C 读库来源、defect_records 持久化、D 缓存粒度、EAM/PM 只读 REST 等）随之撤销。

---

## 13. 附录：关键决策汇总

| # | 决策 | 核心权衡 |
|---|------|---------|
| 1 | **单服务·三路线模块 + 共享内核** | 消除三路线 90% 同构基础设施的重复；与总览 §1.2 一致；每路线模块自包含可拆 |
| 2 | **路线间调用走 Port/Adapter，禁止直 import** | 把"现在单服务、将来可拆"从口号变成结构属性；拆服务只换 Adapter 绑定 |
| 3 | **shared 只放 ≥2 路线复用的设施** | 避免共享内核膨胀；单路线专属设施留路线 `infrastructure/` |
| 4 | **多 DB 共存，按路线降级** | 单进程集中存储依赖（Neo4j/ChromaDB/MySQL/Redis），但 Port 隔离故障域，任一 DB 不可用不拖垮其他路线 |
| 5 | **统一 ReadOnly*Gate 启动断言** | 只读红线靠结构兜底不靠自觉；fail-fast 拒绝启动 |
| 6 | **Alembic 统一管多 schema** | 补齐各路线无迁移工具的缺口；Neo4j/ChromaDB 除外（幂等 DDL / collection 代码初始化） |
| 7 | **统一健康检查 + DI 容器 + router 注册** | 补齐三路线均缺失的运维底座 |
| 8 | **租户跨服务传递协议一处定义** | 补齐各路线各自定义 TenantContext、无传递协议的缺口 |
| 9 | **C/D 暂不建设（descoped）** | rag-service MVP 收窄为 A/B/E；C（NL2SQL 旁路）、D（实时推送）暂不建设，本文档不涉及 |
