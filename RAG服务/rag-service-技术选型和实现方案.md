# rag-service 技术选型和实现方案

> 本文承接 [`rag-service-整体结构设计.md`](rag-service-整体结构设计.md)，给出**一个统一 RAG 服务**的技术选型、关键接口、数据模型、检索链路和验收方案。
>
> 文档检索、制造追溯和 Agentic 是同一应用内的三项能力：前两者实现为只读检索 Port，Agentic 实现为受限的应用层编排。本文不再维护路线级依赖、路线级开关、路线间 Adapter 或多套答案契约。
>
> 制造追溯不使用图检索，不引入 Neo4j、Cypher 或图投影。实现基础是 **MySQL 关系型追溯读模型 + 固定关联查询计划 + MES 只读 REST 补数**。
>
> 口径纪律：RAG 在本项目中是设计阶段的引入规划，本文描述目标方案与落地路径，不代表能力已经上线。

---

## 0. 实现原则

| # | 原则 | 落地方式 |
|---|------|---------|
| 1 | 一个服务、一个入口 | `POST /rag/query` 接收问题，内部按证据需求组合工具 |
| 2 | 统一证据契约 | 文档 chunk 和制造记录都转换为 `Evidence`，答案只引用证据 |
| 3 | 非图追溯 | 业务主键与关联字段查询，白名单查询计划限制展开范围 |
| 4 | 简单链路优先 | 单次检索足够时不启动多步 Agentic；复杂问题才使用 LangGraph |
| 5 | 权限与版本前置 | 在 Port 入参和存储查询中强制执行，不能依赖 prompt 约束 |
| 6 | 只读与有界 | MES ACL 只读，工具有步数、时间、记录数和 token 预算 |
| 7 | 可降级、可重建 | 向量索引和追溯读模型都不是权威数据源，可从原始数据重建 |

---

## 1. 技术选型

### 1.1 选型总表

| 层次 | 选型 | 建议版本 | 用途 |
|------|------|---------|------|
| 语言 | Python | 3.11+ | 服务主体 |
| Web | FastAPI + Pydantic v2 | FastAPI 0.110+ / Pydantic 2.6+ | 统一 API、请求与结构化输出校验 |
| ASGI | uvicorn + gunicorn | 0.29+ / 21.2+ | 服务运行与 worker 管理 |
| LLM 抽象 | langchain-core `BaseChatModel` | 0.2+ | provider 无关的模型适配与观测包装 |
| Agentic 编排 | LangGraph | 0.2+ | 有限状态机、工具调用和停止条件 |
| 文档检索 | LlamaIndex + ChromaDB 适配 | 0.10+ / 0.2+ | 向量索引与检索适配 |
| 向量库 | ChromaDB | 0.5+ | chunk 向量与元数据过滤 |
| Embedding | bge-m3 | 1024 维 cosine | 文档向量化，可本地化部署 |
| Reranker | bge-reranker-v2-m3 | - | 文档候选精排 |
| 推理组件 | sentence-transformers + FlagEmbedding | 3.0+ / 1.2+ | 本地或 sidecar 推理 |
| 关系数据库 | MySQL | 8.0 | 文档元数据、追溯读模型、幂等位点、审计 |
| ORM / 驱动 | SQLAlchemy 2.0 async + asyncmy | 2.0+ / 0.2.9+ | 参数化关系查询 |
| 数据库迁移 | Alembic | 1.13+ | MySQL Schema 版本管理 |
| 缓存 | Redis + redis-py async | 7 / 5.0+ | 检索缓存、限流与短期预算状态 |
| 消息 | Kafka + aiokafka | aiokafka 0.10+ | 消费文档与制造领域事件 |
| HTTP | httpx async | 0.27+ | MES 只读 API 与 agent-service 调用 |
| 配置 | pydantic-settings | 2.2+ | 统一嵌套配置，前缀 `RAG_` |
| Trace | OpenTelemetry | SDK 1.24+ | W3C `traceparent` 与工具 span |
| Metrics | prometheus-client | 0.20+ | `rag_` 前缀指标 |
| Logging | structlog | 24.1+ | JSON 日志与 trace context |
| 文档解析 | unstructured + pypdf + python-docx | 0.14+ / 4.0+ / 1.1+ | PDF、Office 与半结构化文档解析 |
| 基础镜像 | python:3.11-slim | - | 单服务镜像 |

### 1.2 关键选型说明

#### ChromaDB 只承担文档向量检索

车间文档规模预计为数千文档、数十万 chunk 以内，嵌入式 ChromaDB 可以减少独立基础设施。其使用前提是：

- chunk 不可变，文档升版追加新 chunk；
- 元数据包含租户、文档状态、文档版本和 `VersionAnchor`；
- 工艺绑定文档必须按显式或解析得到的版本过滤，历史问题不得退回“查最新”；
- 原始文档、确定性切块策略和事件记录可重建向量索引；
- 文档治理与聚合查询放 MySQL，不依赖 ChromaDB 做事务或报表。

若数据规模、HA 或多写并发超过该前提，应通过 `DocumentIndexPort` 更换实现，而不是改变上层答案契约。

#### MySQL 读模型足以支撑制造追溯

追溯问题具有明确入口和业务关联：序列号、批次号、工单号、过点记录、物料批次、设备、人员、测试结果与工艺版本。使用带索引的 MySQL 表可以完成这些查询，并能直接表达时间、版本和租户条件。

该方案避免额外维护图存储、图 Schema 和图同步链路。代价是复杂关联必须提前设计查询计划，不能自由多跳探索；在制造问答场景中，这个限制同时是安全与性能优势。

#### LangGraph 只用于需要追加检索的编排

固定的一次文档检索或一次追溯查询使用普通 async application service。只有“追溯发现工艺版本后再查对应 SOP”“首轮证据不足需换种子补查”等多步场景才进入 LangGraph。`recursion_limit` 只是最后防线，应用层仍需独立计算工具次数、时长和证据预算。

### 1.3 依赖清单

```toml
[project]
name = "rag-service"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "gunicorn>=21.2",
  "pydantic>=2.6",
  "pydantic-settings>=2.2",
  "langchain-core>=0.2",
  "langgraph>=0.2",
  "llama-index>=0.10",
  "llama-index-vector-stores-chroma>=0.2",
  "chromadb>=0.5",
  "sentence-transformers>=3.0",
  "FlagEmbedding>=1.2",
  "sqlalchemy[asyncio]>=2.0",
  "asyncmy>=0.2.9",
  "alembic>=1.13",
  "redis>=5.0",
  "aiokafka>=0.10",
  "httpx>=0.27",
  "opentelemetry-sdk>=1.24",
  "opentelemetry-instrumentation-fastapi>=0.45b0",
  "opentelemetry-instrumentation-httpx>=0.45b0",
  "prometheus-client>=0.20",
  "structlog>=24.1",
  "unstructured>=0.14",
  "pypdf>=4.0",
  "python-docx>=1.1",
]
```

具体模型 provider 依赖按部署环境选装。生产交付应在评审后锁定依赖版本，不只保留下限。

### 1.4 部署依赖

```yaml
services:
  rag-service:
    build: .
    environment:
      RAG_LLM__PROVIDER: deepseek
      RAG_EMBEDDING__BASE_URL: http://bge-inference:8080
      RAG_MYSQL__DSN: mysql+asyncmy://rag:rag@mysql:3306/rag
      RAG_REDIS__URL: redis://redis:6379/0
      RAG_KAFKA__BOOTSTRAP: kafka:9092
      RAG_CHROMA__PERSIST_DIR: /data/chroma
      RAG_ORCHESTRATION__MAX_STEPS: "6"
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    volumes:
      - chromadata:/data/chroma
    ports:
      - "8000:8000"

  bge-inference:
    image: michaelfeil/infinity:latest
    ports:
      - "8080:8080"

  mysql:
    image: mysql:8.0

  redis:
    image: redis:7-alpine

volumes:
  chromadata:
```

Kafka、MES、模型 provider 与 OTel Collector 可复用现有部署。配置项用于调整统一服务行为，不包含 `document.enabled`、`traceability.enabled`、`agentic.enabled` 一类路线开关。

---

## 2. 统一契约与 Port

### 2.1 检索上下文

```python
# app/domain/query.py
@dataclass(frozen=True)
class RetrievalContext:
    request_id: str
    trace_id: str
    tenant: TenantContext
    version_anchor: VersionAnchor | None
    time_range: TimeRange | None
    budget: RetrievalBudget


@dataclass(frozen=True)
class RetrievalBudget:
    max_steps: int = 6
    max_tool_calls: int = 8
    max_records: int = 200
    timeout_seconds: int = 30
```

`RetrievalContext` 由 `QueryService` 创建，工具不能自行放宽租户、版本、时间或预算。`TenantContext` 不信任模型生成的参数，只能来自认证上下文。

### 2.2 证据与答案

```python
# app/domain/evidence.py
class EvidenceType(StrEnum):
    DOCUMENT_CHUNK = "DOCUMENT_CHUNK"
    TRACE_RECORD = "TRACE_RECORD"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    evidence_type: EvidenceType
    source: SourceRef
    locator: EvidenceLocator
    content: str | Mapping[str, object]
    version_anchor: VersionAnchor | None
    occurred_at: datetime | None
    freshness: Freshness
```

`RagAnswer` 包含 `summary`、`sections`、`citations`、`evidence_ids`、`version_anchors`、`missing_evidence`、`retrieval_steps` 和 `trace_id`。引用必须指向本次 `EvidenceSet` 中的 ID，生成后由 `CitationPolicy` 再校验一次。

### 2.3 检索 Port

```python
# app/application/ports/document_search.py
class DocumentSearchPort(Protocol):
    async def search(
        self,
        query: str,
        context: RetrievalContext,
        *,
        doc_types: set[str] | None = None,
        top_k: int = 8,
    ) -> list[Evidence]: ...


# app/application/ports/trace_lookup.py
class TraceLookupPort(Protocol):
    async def lookup(
        self,
        seed: TraceSeed,
        context: RetrievalContext,
    ) -> TraceEvidenceResult: ...

    async def expand(
        self,
        records: list[Evidence],
        plan_name: str,
        context: RetrievalContext,
    ) -> TraceEvidenceResult: ...
```

`TraceEvidenceResult` 同时包含 `evidence`、`resolved_version_anchors` 和 `missing_evidence`。调用方必须处理缺失项，不能只读取证据列表。

### 2.4 统一应用入口

```python
# app/application/query_service.py
class QueryService:
    async def handle(self, query: RagQuery, tenant: TenantContext) -> RagAnswer:
        context = self._context_factory.create(query, tenant)
        plan = await self._planner.plan(query, context)

        if plan.requires_multi_step:
            result = await self._orchestrator.run(plan, context)
        else:
            result = await self._direct_retriever.execute(plan, context)

        answer = await self._composer.compose(query, result.evidence, context)
        await self._audit.record(query, result, answer, context)
        return answer
```

规划器输出的是“需要哪些证据和工具”，不是文档型、追溯型或 Agentic 的路线标签。

---

## 3. 文档检索实现

### 3.1 摄入链路

```text
原始文档
  -> 类型识别与解析
  -> 清洗、章节与页码保留
  -> 确定性切块
  -> 文档版本 / 租户 / VersionAnchor 元数据绑定
  -> bge-m3 向量化
  -> ChromaDB 写入
  -> MySQL 摄入状态与索引位点提交
```

- 以 `(doc_id, document_version, chunk_seq, chunk_strategy_version)` 作为业务幂等键。
- 原始文件和解析结果按文档版本保存，重跑切块与向量化不会覆盖其他版本。
- 文档撤回或权限变化需要立即更新 MySQL 元数据，并使相关缓存失效；索引中的状态同步完成前，检索必须叠加权威状态校验或停止该文档的召回。

### 3.2 chunk 元数据

| 字段 | 用途 |
|------|------|
| `tenant_id`、`workshop_scope`、`line_scope` | 检索前权限过滤 |
| `doc_id`、`document_version`、`doc_type` | 来源与类型过滤 |
| `state` | `PUBLISHED` 等状态过滤 |
| `version_kind`、`version_ref_id`、`version` | 工艺/BOM/规则等版本过滤 |
| `effective_from`、`effective_to` | 当前指导与历史查询的时间语义 |
| `chunk_seq`、`chunk_strategy_version` | 幂等与重建 |
| `page`、`section`、`source_locator` | 精确引用 |

### 3.3 查询链路

1. 根据 `RetrievalContext` 构造强制过滤条件；
2. 向量召回候选，候选数大于最终 `top_k`，为重排保留空间；
3. bge-reranker 精排；
4. 去除重复章节和低相关片段；
5. 转换为 `Evidence`，保留原始 locator 与版本；
6. 记录召回数、过滤后数量、重排耗时与索引 freshness。

工艺绑定文档如果没有显式版本，应先由 `version_resolve` 根据目标工单或过点记录解析。解析失败时返回澄清或证据不足，不自动使用当前 ACTIVE 版本回答历史问题。

---

## 4. 非图制造追溯实现

### 4.1 读模型

读模型围绕实际查询建立，不复制 MES 全量数据。首版可采用以下表：

| 表 | 关键字段 | 主要索引 |
|----|---------|---------|
| `trace_work_order` | `work_order_id`、产品、工单状态、计划/实际时间 | `(tenant_id, work_order_id)` |
| `trace_unit_operation` | `operation_record_id`、`serial_no`、`work_order_id`、站点、设备、人员、工艺版本、过点时间 | `(tenant_id, serial_no, occurred_at)`、`(tenant_id, work_order_id, occurred_at)` |
| `trace_material_usage` | 过点记录、物料批次、用量、上料时间 | `(tenant_id, operation_record_id)`、`(tenant_id, material_lot_id, used_at)` |
| `trace_test_result` | 过点记录、测试项、结果、缺陷代码、测试时间 | `(tenant_id, operation_record_id)`、`(tenant_id, defect_code, tested_at)` |
| `trace_route_binding` | 工单/过点记录、工艺 `ref_id` 与版本、生效区间 | `(tenant_id, work_order_id)`、`(tenant_id, route_ref_id, route_version)` |
| `trace_equipment_context` | 过点记录、设备、经评审的参数摘要、采集时间 | `(tenant_id, operation_record_id, captured_at)` |
| `trace_sync_checkpoint` | 来源主题、分区、位点、最近事件时间 | 消费者组与分区唯一键 |

每张业务表都包含 `tenant_id`、必要的 workshop/line scope、来源上下文、来源记录 ID、来源更新时间和数据版本。设备参数只保存回答目标问题所需的摘要，不全量复制高频原始采集数据。

### 4.2 固定查询计划

```python
# app/infrastructure/trace/query_plans.py
QUERY_PLANS = {
    "unit_overview": ("unit_operation", "test_result", "route_binding"),
    "unit_5m1e": (
        "unit_operation",
        "material_usage",
        "test_result",
        "route_binding",
        "equipment_context",
    ),
    "lot_units": ("material_usage", "unit_operation", "test_result"),
    "work_order_history": ("work_order", "unit_operation", "test_result"),
}
```

计划名由服务端规划器选择，客户端不能直接提交表名、字段名或 SQL。每个计划定义：

- 允许读取的 repository 方法；
- 必需的种子字段；
- 最大记录数与时间窗口；
- 可继续展开的计划名；
- 必须返回的版本字段；
- 数据权限与 freshness 检查。

### 4.3 查询算法

```text
TraceSeed(serial_no | material_lot_id | work_order_id | defect + time_range)
   │
   ▼
解析种子并校验租户、时间范围
   │
   ▼
读取主记录（过点 / 工单 / 批次使用记录）
   │
   ▼
按固定计划使用业务关联字段批量查询
   │
   ├── 过点 -> 测试结果
   ├── 过点 -> 物料使用
   ├── 过点 -> 工艺版本绑定
   └── 过点 -> 设备与人员上下文
   │
   ▼
版本对齐、去重、权限复核、记录数裁剪
   │
   ▼
Trace Evidence + Resolved Versions + Missing Evidence
```

实现时使用 SQLAlchemy 参数化查询和批量 `IN` 条件，避免逐条记录发起查询。任何追加展开都消耗同一 `RetrievalBudget`，不能递归创建新预算。

### 4.4 MES 只读补数

满足以下条件时可通过 ACL 补数：

- 读模型缺少种子记录；
- 读模型位点超过查询允许的新鲜度阈值；
- 目标问题需要未进入读模型、但已列入白名单的权威字段；
- 读模型与事件版本冲突，需要权威记录确认。

补数仍须转换为统一 `Evidence`，来源标记为 MES 上下文与资源 ID，并记录读取时间。REST 超时或部分失败时返回 `MissingEvidence`，不能用文档片段或模型常识代替制造记录。

### 4.5 5M1E 与根因表达

| 分类 | 证据来源示例 |
|------|-------------|
| 人 Man | 过点人员、资质或班次记录（按权限脱敏） |
| 机 Machine | 设备、工站、经评审的参数摘要 |
| 料 Material | 物料批次、上料与使用记录 |
| 法 Method | 当时工艺版本、对应版本 SOP 与检验标准 |
| 测 Measurement | 测试结果、缺陷代码、检测记录 |
| 环 Environment | 已纳入读模型的环境记录；未接入时明确缺失 |

5M1E 是证据归类，不是自动根因结论。答案可以说明“哪些记录存在异常或与问题相关”，但最终根因需要足够证据或人工确认。

---

## 5. Agentic 编排实现

### 5.1 状态定义

```python
# app/application/orchestrator.py
class RagState(TypedDict):
    query: str
    plan: RetrievalPlan
    evidence: list[Evidence]
    missing: list[MissingEvidence]
    steps: list[RetrievalStep]
    tool_calls: int
    started_at: float
    stop_reason: str | None
```

状态节点为 `analyze -> plan -> act -> observe -> compose`。`observe` 后根据以下条件决定追加检索或停止：

- 仍存在可补救的证据缺口；
- 存在合法且未尝试过的工具动作；
- 当前步数、工具次数、记录数和总时长均未超限；
- 上一轮产生了新证据或解析出了新版本锚点。

### 5.2 工具白名单

| 工具 | 实现 Port | 典型用途 |
|------|----------|---------|
| `document_search` | `DocumentSearchPort` | SOP、手册、标准、历史 8D 检索 |
| `trace_lookup` | `TraceLookupPort.lookup` | 根据单件、批次或工单建立证据集 |
| `record_expand` | `TraceLookupPort.expand` | 按固定计划追加物料、测试、设备等记录 |
| `version_resolve` | `VersionResolverPort` | 从历史制造记录解析版本锚点 |

所有工具接收服务端保存的 `context_id`，由服务端还原 `RetrievalContext`；模型不能伪造或替换租户上下文。

### 5.3 典型组合

```text
问题：批次 LOT-01 焊接不良怎么处理？

1. trace_lookup(lot=LOT-01)
2. 从目标过点记录解析 route VersionAnchor
3. record_expand(plan_name="lot_units")
4. document_search(query="焊接不良 处置", version_anchor=已解析版本)
5. 对齐测试记录与对应版本 SOP
6. 输出制造事实、文档建议、候选解释和缺失证据
```

这里只发生了一次统一 RAG 请求。多次工具调用是多步检索过程，不是跨路线调用。

### 5.4 停止与降级

| 情况 | 行为 |
|------|------|
| 单次检索证据充分 | 直接生成，不进入多步循环 |
| 缺少关键种子或时间范围 | 返回澄清问题 |
| 工具无新增证据 | 停止并说明缺口 |
| 达到步数/时间/token 上限 | 基于已有证据部分回答或返回预算耗尽 |
| 权限或版本冲突 | 停止相关检索，不尝试换路径绕过 |
| LLM 生成失败 | `RETRIEVAL_ONLY` 可返回证据；普通请求返回可重试错误 |

---

## 6. 配置、启动与依赖注入

### 6.1 配置模型

```python
class RagSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_nested_delimiter="__",
    )

    llm: LlmSettings
    embedding: EmbeddingSettings
    mysql: MysqlSettings
    redis: RedisSettings
    kafka: KafkaSettings
    chroma: ChromaSettings
    document: DocumentSettings
    trace: TraceSettings
    orchestration: OrchestrationSettings
    security: SecuritySettings
```

`document`、`trace` 和 `orchestration` 配置分别保存 top-k、查询窗口、最大步数等参数，不代表独立产品或独立部署单元。

### 6.2 启动流程

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    container = build_container(settings)
    await run_startup_assertions(settings, container)
    await container.initialize()
    await container.start_consumers()
    app.state.container = container
    yield
    await container.close()
```

启动断言覆盖只读 ACL、工具白名单、查询计划、Kafka topic 和必要索引；`/ready` 再反映 MySQL、ChromaDB、Redis、Kafka 和关键 MES 只读依赖状态。

### 6.3 路由注册

```python
def register_routers(app: FastAPI) -> None:
    app.include_router(query_router)      # /rag/query, /rag/answers/{id}/evidence
    app.include_router(document_router)   # /rag/documents/*
    app.include_router(health_router)     # /health, /ready, /metrics
```

`QueryService` 通过 DI 获得规划器、两个检索 Port、编排器、答案生成器和审计 Port。基础设施替换不影响 API 契约。

---

## 7. 事件、版本与一致性

### 7.1 事件消费

- 文档生命周期事件驱动解析、发布状态同步和重索引。
- 制造领域事件更新关系型追溯读模型。
- `event_id` 用于幂等，Kafka 位点和来源事件时间用于 freshness。
- 读模型更新失败进入重试或死信流程，不提交未完成位点。
- 禁止订阅未经评审的高频原始采集主题。

### 7.2 版本解析

| 场景 | 版本策略 |
|------|---------|
| 当前 SOP 查询 | 使用当前有效且已发布版本，并记录解析依据 |
| 历史单件/批次问题 | 从目标过点或工单绑定解析当时版本 |
| 用户显式指定版本 | 严格使用指定版本，冲突时提示，不静默切换 |
| 通用设备手册 | 按设备型号与适用版本过滤，无版本语义时可不绑工艺版本 |
| 无法解析工艺版本 | 不输出确定性的版本相关作业指导，返回澄清或缺口 |

### 7.3 缓存

缓存 key 至少包含租户与数据范围摘要、规范化问题、版本锚点、时间范围、工具参数、索引版本和 prompt/模型版本。不得只按 query 文本缓存，否则可能产生越权或历史版本串用。

---

## 8. 数据库与迁移

| Schema | 内容 | 迁移方式 |
|--------|------|---------|
| `rag_knowledge` | 文档、版本、绑定、摄入任务、索引状态 | Alembic |
| `rag_trace` | 关系型追溯读模型、同步检查点 | Alembic |
| `rag_runtime` | 事件幂等与位点、答案审计、检索步骤 | Alembic |
| ChromaDB | 文档向量与过滤元数据 | collection 初始化、备份与重建 |
| Redis | 缓存、限流、短期预算 | 不作为唯一事实来源 |

迁移脚本必须包含 `upgrade()` 与 `downgrade()`；新增读模型字段优先采用“加字段 -> 回放/双写 -> 切读 -> 清理”的兼容流程。追溯读模型重建期间通过 generation 或别名切换，避免半张表对外提供查询。

ChromaDB collection 使用 cosine 空间，启动时幂等创建。备份不能替代重建能力，至少定期验证一次从原始文档和确定性管线完整重建。

---

## 9. 安全与只读实现

### 9.1 启动期检查

| 检查 | 内容 |
|------|------|
| `ReadOnlyAclGate` | MES 客户端只能注册白名单只读方法 |
| `ReadOnlyToolGate` | Agentic 工具必须声明只读且属于白名单 |
| `QueryPlanGate` | 查询计划只能引用已注册 repository 与允许关联 |
| `RawDataTopicGate` | 禁止订阅未评审原始数据主题 |
| `IndexGate` | 必需 MySQL 索引和 ChromaDB collection 已就绪 |

### 9.2 运行时检查

- 每次文档与追溯调用都校验 `TenantContext`；
- 每次追溯展开都校验时间窗口、记录数和计划权限；
- 版本相关回答执行 `VersionPolicy`；
- 生成后执行 `CitationPolicy`，检查关键事实是否有证据；
- MES 只读 ACL 使用低权限独立凭据，从网络与账号层限制写权限；
- 文档管理接口与问答接口分别授权，普通问答用户不能摄入或修改文档。

启动扫描只能发现已知结构问题，不能替代运行时鉴权、数据库权限和测试。

---

## 10. 可观测与故障处理

### 10.1 指标

| 指标 | 用途 |
|------|------|
| `rag_query_duration_seconds`、`rag_query_total` | 统一请求 SLO |
| `rag_document_retrieval_duration_seconds`、`rag_document_hits` | 文档检索质量与性能 |
| `rag_trace_lookup_duration_seconds`、`rag_trace_records` | 追溯查询性能与证据规模 |
| `rag_trace_read_model_freshness_seconds` | 读模型滞后 |
| `rag_orchestrator_steps`、`rag_tool_calls_total` | Agentic 步数与工具成本 |
| `rag_answer_citation_failures_total` | 引用校验失败 |
| `rag_insufficient_evidence_total` | 证据不足与拒答情况 |
| `rag_llm_tokens_total`、`rag_llm_call_duration_seconds` | 模型成本与延迟 |
| `rag_dependency_health` | MySQL、ChromaDB、Redis、Kafka、MES 只读依赖状态 |

### 10.2 Trace 与审计

OTel span 链为 `rag.query -> plan -> document.search/trace.lookup -> llm.compose`。跨服务调用透传 `traceparent`，与 agent-service 和 MES 使用同一个 `trace_id`。

审计保存请求摘要、工具步骤、证据 ID/来源/版本、读模型 freshness、模型与 prompt 版本、停止原因和最终状态。序列号、人员和物料信息按治理规则脱敏，不将完整敏感上下文写入日志。

### 10.3 降级矩阵

| 故障 | 行为 |
|------|------|
| ChromaDB 不可用 | 可返回追溯事实，明确文档建议缺失 |
| 追溯读模型不可用 | 尝试白名单 MES 只读补数；仍失败则只回答有证据的文档内容 |
| MES 只读接口超时 | 使用符合 freshness 要求的读模型，否则返回缺口 |
| Redis 不可用 | 绕过缓存并收紧限流，不影响权限与版本判断 |
| LLM 不可用 | 检索模式返回证据；生成模式返回可重试错误 |
| Kafka 消费滞后 | 展示 freshness，超过阈值时转只读补数或拒绝强一致回答 |

---

## 11. 测试与评测

### 11.1 单元测试

- 版本解析：当前指导、历史过点、显式版本、冲突和无法解析；
- 文档过滤：租户、发布状态、版本和生效时间；
- 追溯计划：非法计划、缺少种子、超时间窗、超记录数均拒绝；
- 权限：每次展开保持相同数据范围；
- 编排停止：无新增证据、达到预算、权限冲突和依赖超时；
- 引用校验：不存在证据 ID、证据与版本不匹配时失败；
- 脱敏与缓存 key：不同租户和版本不会共享结果。

### 11.2 集成测试

- 文档摄入重跑幂等，升版后旧 chunk 不变、新 chunk 可检索；
- 重复制造事件不会生成重复读模型记录；
- 序列号、批次和工单三种种子均能按固定计划取证；
- MES 补数超时能返回部分证据与缺失项；
- 追溯解析出版本后，文档检索使用该版本；
- ChromaDB 或追溯依赖故障时符合降级矩阵；
- `traceparent` 在 rag-service、agent-service 和 MES 间连续传播。

### 11.3 契约测试

- `/rag/query` 同时覆盖 `AUTO` 与 `RETRIEVAL_ONLY`；
- 文档与追溯证据均符合统一 `Evidence` Schema；
- 响应中的每个 citation 都能通过证据查询接口定位；
- agent-service 不传租户上下文或伪造扩大数据范围时被拒绝；
- 历史请求缺少版本且无法解析时不得自动查最新工艺。

### 11.4 评测集

| 维度 | 核心断言 |
|------|---------|
| 文档检索 | Recall@k、引用定位、版本过滤、越权过滤 |
| 制造追溯 | 种子解析、关键记录召回、错误关联、版本对齐、缺失识别 |
| Agentic | 工具选择、追加检索收益、步数与预算、无新证据停止 |
| 综合答案 | 忠实度、引用完整、事实/建议/推断区分、冲突表达 |
| 安全红线 | 写操作、跨租户、失效版本、无证据制造事实任一出现即阻断 |
| 性能成本 | 延迟、工具次数、模型 token、缓存命中与降级表现 |

---

## 12. 分阶段交付

阶段是同一服务的增量建设，不对应不同 RAG 路线。

| 阶段 | 主要交付 | 验收重点 |
|------|---------|---------|
| 一：统一底座与文档检索 | 统一契约、权限/版本策略、摄入管线、`/rag/query` 基础链路 | 文档引用、版本过滤、只读和租户测试通过 |
| 二：关系型追溯 | 读模型、事件同步、固定查询计划、MES 补数、统一证据融合 | 三类种子、5M1E 召回、错误关联和 freshness 测试通过 |
| 三：Agentic 编排 | LangGraph 状态机、工具预算、追加检索、审计与证据页面 | 复杂问题有实际收益，预算和停止条件可靠 |
| 四：生产治理 | 压测、备份重建、故障演练、评测门禁与运维看板 | SLO、安全红线、恢复与重建全部通过 |

---

## 13. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 关系型读模型滞后 | 制造事实不完整 | freshness 指标、阈值控制、MES 只读补数、必要时拒答 |
| 固定关联计划覆盖不足 | 某些问题无法完整追溯 | 返回缺口；按评测新增评审后的计划，不开放任意查询 |
| 关联字段质量差 | 错误串联或漏串联 | 唯一键约束、来源校验、异常关联评测、人工核验提示 |
| Agentic 多步成本过高 | 延迟与 token 增加 | 简单请求直达、缓存、工具/步数/时间预算、无新增证据停止 |
| 文档版本过滤被绕过 | 使用失效或不适用 SOP | Port 强制校验、契约测试、历史版本解析、安全评测阻断 |
| ChromaDB 损坏或丢失 | 文档检索不可用 | 定期备份、确定性重建、降级为追溯证据 |
| MES ACL 被误用为写客户端 | 产生生产副作用 | 白名单、独立只读账号、网络限制、启动与运行时检查 |
| 统一契约过度膨胀 | 文档与制造证据互相污染 | 统一外层 `Evidence`，内部保留各自结构化 content 与 locator |

---

## 14. 完成定义（DoD）

- [ ] 只有一个统一 `/rag/query` 问答入口和一套 `RagAnswer`/`Evidence` 契约。
- [ ] 文档检索、追溯检索与 Agentic 不存在独立路线开关或路线间调用协议。
- [ ] 追溯仅使用 MySQL 读模型、固定关联查询与 MES 只读接口，无图数据库依赖。
- [ ] 追溯每个查询计划都有种子、时间窗、记录数、权限和版本限制。
- [ ] 文档 chunk 不可变，版本、租户和发布状态在检索时过滤。
- [ ] 历史问题能从制造记录解析版本，无法解析时不会退回最新工艺。
- [ ] Agentic 工具全部只读，步数、工具次数、记录数、时长和 token 有预算。
- [ ] 无新增证据、权限冲突和版本冲突能正确停止，不循环、不绕过。
- [ ] 所有关键答案陈述可定位到文档片段或制造记录，缺失证据显式返回。
- [ ] MySQL 迁移、ChromaDB 重建、事件幂等和读模型重建经过验证。
- [ ] 依赖故障符合降级矩阵，不影响 MES 生产主事务。
- [ ] 文档、追溯、Agentic、端到端与安全评测接入 CI，红线失败阻断交付。
