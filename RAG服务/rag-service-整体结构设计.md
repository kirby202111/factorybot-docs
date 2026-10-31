# rag-service 整体结构设计

> 本文定义一个统一的 rag-service，而不是若干条独立 RAG 路线。服务同时具备**文档检索、制造追溯、Agentic 检索编排**三项能力，它们在同一套领域契约、权限控制、版本治理和可观测体系下协同工作。
>
> 文档检索负责找到制度、SOP、手册和历史经验；制造追溯负责从关系型读模型与 MES 只读接口还原单件、批次、工单及其 5M1E 证据；Agentic 负责根据问题选择工具、补充检索、交叉核验并组织答案。三者是内部能力，不是独立产品、独立服务或需要用户选择的运行路线。
>
> **明确不采用图检索**：不引入 Neo4j、GraphRAG、Cypher、图投影或 `subgraph_ref`。追溯通过业务主键、关联字段、关系型读模型和受限的只读查询完成。
>
> rag-service 全程只读 MES，不进入过点等生产主事务；答案必须能够回到文档片段或制造记录，无法满足版本、权限或证据约束时应拒答或明确说明证据缺口。

---

## 0. 设计目标与边界

| 项目 | 设计决策 |
|------|---------|
| 服务形态 | 一个 Python 微服务、一个统一问答入口、一套应用层与领域模型 |
| 能力关系 | 文档检索与追溯是检索工具，Agentic 是按需启用的编排能力，不拆成三条业务路线 |
| 追溯实现 | MySQL 关系型读模型 + MES 只读 REST，按种子实体和关联字段逐跳取证 |
| 文档实现 | 文档解析、切块、向量召回、元数据过滤与重排，保留精确引用 |
| 版本治理 | `VersionAnchor` 贯穿文档、追溯记录、检索结果和最终答案 |
| 权限治理 | 租户、车间、产线及数据范围在检索前生效，不在生成后补救 |
| 只读边界 | 只允许查询、摄入 RAG 自有知识数据和记录审计；不得调用 MES 写接口 |
| Agentic 边界 | 有最大步数、工具白名单和停止条件，不做无限循环或开放式写操作 |
| 降级原则 | 局部证据不可用时返回部分答案与缺口；不得用模型记忆补齐制造事实 |

### 0.1 设计取舍

1. **统一优先于拆分**：三类能力共享 query、证据、答案、版本、权限和审计模型，避免三套服务之间反复转换契约。
2. **编排不等于业务分裂**：Agentic 可以调用不同工具，但调用工具不代表存在不同 RAG 路线。
3. **追溯不等于图数据库**：MES 已有明确的工单、过点、物料批次、设备、人员和工艺版本关联，使用索引化关系查询更直接，也减少一套存储和同步链路。
4. **证据先于生成**：检索器先产出结构化证据，生成器只能基于证据作答；没有证据时允许输出澄清问题或拒答。
5. **简单请求不过度 Agentic**：单次检索足够时直接返回；只有需要多源组合、追加查询或交叉核验时才进入多步编排。

---

## 1. 统一能力模型

### 1.1 三项能力的职责

| 能力 | 输入 | 输出 | 不负责 |
|------|------|------|--------|
| 文档检索 | 自然语言、文档类型、版本锚点、权限上下文 | 文档片段、引用位置、相关性分数 | 直接推断制造事实、绕过版本过滤 |
| 制造追溯 | 序列号、批次号、工单号、缺陷或时间范围 | 结构化制造记录、5M1E 证据、来源引用、缺失项 | 图遍历、无边界全链展开、根因断言 |
| Agentic 编排 | 用户问题、会话上下文、可用工具 | 检索计划、工具调用、补充查询、综合答案 | 修改 MES、脱离证据自由推理 |

三项能力共同回答同一类问题。例如“这个批次为什么出现焊接不良，应按什么处理”需要：

1. 追溯工具确认批次、过点、测试结果、物料、设备和工艺版本；
2. 文档工具按相同版本锚点检索 SOP、检验标准和历史 8D；
3. Agentic 对证据进行对齐，区分“记录显示的事实”“文档给出的处置建议”和“尚不能确认的根因”；
4. 统一答案返回文档引用与制造记录引用，而不是只给一段自然语言。

### 1.2 总体结构

```text
                           用户 / 前端 / agent-service
                                      │
                                      ▼
                             POST /rag/query
                                      │
                       ┌──────────────┴──────────────┐
                       │      RAG Application        │
                       │  QueryService / Orchestrator│
                       └──────┬───────────────┬───────┘
                              │               │
                    ┌─────────▼───────┐ ┌─────▼──────────┐
                    │ DocumentSearch  │ │ TraceLookup    │
                    │ 文档检索工具     │ │ 非图追溯工具    │
                    └─────────┬───────┘ └─────┬──────────┘
                              │               │
                       ChromaDB + metadata    │ MySQL 读模型
                              │               │ MES 只读 REST
                              └───────┬───────┘
                                      ▼
                         Evidence Set + Version Anchor
                                      │
                                      ▼
                         Evidence-grounded Generation
                                      │
                                      ▼
                    RagAnswer + Citations + Missing Evidence
```

Agentic 编排位于应用层，不是另一套 RAG。简单问题由 `QueryService` 直接调用检索工具；复杂问题由 `RagOrchestrator` 在有限状态机内多次调用相同工具。

### 1.3 一次请求的通用流程

1. **建立上下文**：解析租户与数据权限，继承 `trace_id`，记录问题与请求预算。
2. **解析约束**：识别序列号、批次、工单、时间范围和显式版本要求；无法确认关键约束时先澄清。
3. **制定检索计划**：确定需要文档证据、追溯证据或二者，并设置结果数量与查询范围。
4. **执行检索**：所有工具接收同一个 `RetrievalContext`，强制执行租户和版本过滤。
5. **判断证据充分性**：证据足够则生成；存在可补救缺口则在预算内追加检索；否则返回缺口。
6. **组织答案**：分离事实、建议与推断，每个关键陈述绑定证据 ID。
7. **审计与返回**：保存工具调用、证据快照引用、模型信息和答案摘要，返回统一 `RagAnswer`。

---

## 2. 顶层目录结构

```text
rag_service/
├── app/
│   ├── api/
│   │   ├── v1/
│   │   │   ├── query_router.py          # 统一问答与证据查询
│   │   │   └── document_router.py       # 文档摄入等管理接口
│   │   ├── deps.py
│   │   ├── middleware.py
│   │   └── errors.py
│   │
│   ├── application/
│   │   ├── query_service.py             # 统一用例入口
│   │   ├── orchestrator.py              # Agentic 状态机与停止条件
│   │   ├── query_planner.py             # 从问题提取检索需求
│   │   ├── answer_composer.py           # 基于证据生成统一答案
│   │   ├── document_ingestion_service.py
│   │   └── ports/
│   │       ├── document_search.py       # 文档检索 Port
│   │       ├── trace_lookup.py          # 追溯检索 Port
│   │       ├── llm.py
│   │       └── audit.py
│   │
│   ├── domain/
│   │   ├── query.py                     # RagQuery / RetrievalContext
│   │   ├── answer.py                    # RagAnswer / AnswerSection
│   │   ├── evidence.py                  # Evidence / Citation / MissingEvidence
│   │   ├── document.py                  # 文档、版本、chunk、绑定关系
│   │   ├── trace.py                     # TraceSeed / TraceRecord / FiveM1EEvidence
│   │   ├── version.py                   # VersionAnchor
│   │   └── policy.py                    # 权限、版本与只读策略
│   │
│   ├── infrastructure/
│   │   ├── document/
│   │   │   ├── parsers/
│   │   │   ├── chunking/
│   │   │   ├── chromadb/
│   │   │   └── repositories/
│   │   ├── trace/
│   │   │   ├── mysql_read_models/       # 索引化关系型追溯读模型
│   │   │   ├── mes_readonly/            # MES 只读 REST 适配器
│   │   │   └── query_plans.py           # 白名单关联查询与展开限制
│   │   ├── orchestration/
│   │   │   └── langgraph_runner.py
│   │   ├── llm/
│   │   ├── embedding/
│   │   ├── persistence/
│   │   ├── kafka/
│   │   ├── observability/
│   │   └── tenancy/
│   │
│   ├── config.py
│   └── main.py
│
├── alembic/
├── tests/
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```

目录按技术与领域职责划分，不再使用 `routes/document`、`routes/traceability`、`routes/agentic` 表达三个并列业务路线。`DocumentSearch` 与 `TraceLookup` 通过应用层 Port 注入，编排器只依赖接口，不依赖 ChromaDB、SQL 或 HTTP 实现。

---

## 3. 统一领域契约

### 3.1 查询与答案

```text
RagQuery
├── query: str
├── session_id: str | None
├── subject_refs: list[SubjectRef]        # serial/batch/work_order 等可选线索
├── time_range: TimeRange | None
├── version_anchor: VersionAnchor | None
├── response_mode: AUTO | RETRIEVAL_ONLY
└── retrieval_budget: RetrievalBudget

RagAnswer
├── answer_id: str
├── summary: str
├── sections: list[AnswerSection]         # 事实 / 处置建议 / 待确认事项
├── citations: list[Citation]
├── evidence_ids: list[str]
├── version_anchors: list[VersionAnchor]
├── missing_evidence: list[MissingEvidence]
├── retrieval_steps: list[RetrievalStepSummary]
└── trace_id: str
```

`response_mode` 只控制是否生成自然语言，不用于选择所谓 RAG 路线。系统是否进行多步检索由证据需求和预算决定。

### 3.2 统一证据模型

| 字段 | 说明 |
|------|------|
| `evidence_id` | 本次请求内稳定的证据标识 |
| `evidence_type` | `DOCUMENT_CHUNK` 或 `TRACE_RECORD` |
| `source` | 文档 ID 与版本，或 MES 上下文与只读资源 |
| `locator` | 文档页码/chunk 位置，或制造记录类型与业务主键 |
| `content` | 用于生成的原文片段或结构化记录 |
| `occurred_at` | 制造记录发生时间；文档可为空 |
| `version_anchor` | 与证据绑定的工艺/BOM/规则等版本 |
| `permissions_snapshot` | 检索时使用的数据范围摘要，不保存敏感令牌 |
| `freshness` | 读模型位点或接口读取时间 |

最终答案只能引用已进入 `EvidenceSet` 的内容。文档证据与追溯证据采用同一引用协议，UI 不需要理解三套答案模型。

### 3.3 版本与权限

- `VersionAnchor(kind, ref_id, version)` 覆盖 `route/bom/rule/asset/standard`。
- 用户显式指定版本时不得静默切换；未指定时可根据工单或过点记录解析历史版本。
- 当前操作指导只使用有效且已发布的文档；历史质量分析必须使用该制造记录当时的版本。
- `TenantContext` 至少包含 `tenant_id` 与 workshop/line scopes，并作为所有检索 Port 的必填参数。
- 文档元数据过滤、SQL 条件与 MES 只读请求都必须携带相同权限上下文。

---

## 4. 核心能力设计

### 4.1 文档检索

文档链路为：解析 -> 清洗 -> 切块 -> 元数据绑定 -> 向量化 -> 过滤召回 -> 重排 -> 证据输出。

关键约束：

1. **版本是元数据的一部分**：chunk 绑定文档版本及 `VersionAnchor`，不可只依赖正文中的版本文字。
2. **chunk 不可变**：文档升版生成新 chunk，旧版本保留用于历史追溯；发布状态与生效区间独立维护。
3. **先过滤再生成**：租户、文档状态、版本和生效时间在召回阶段过滤，重排不得越权召回其他数据。
4. **引用可定位**：保留文档 ID、版本、页码、章节、chunk ID 和原文位置。
5. **事件驱动更新**：消费文档与工艺生命周期事件，幂等更新 RAG 自有索引，不回写 MES。

文档工具可以独立支撑简单 SOP 问答，也可以由 Agentic 在追溯结果明确工艺版本后再次调用，实现“先定位历史版本，再查对应作业指导”。

### 4.2 非图制造追溯

#### 数据来源

- **MySQL 关系型读模型**：保存查询所需的制造事实快照和索引，数据来自 MES 领域事件或只读同步。
- **MES 只读 REST**：用于读模型缺数、位点滞后或需要权威记录时补齐，响应仍转换为统一证据。

读模型按业务查询设计，例如 `work_order_fact`、`unit_operation_fact`、`test_result_fact`、`material_usage_fact`、`route_version_fact`。它们使用业务主键和关联字段连接，不建立图节点、图边或 Cypher 查询。

#### 查询过程

1. **种子解析**：从序列号、批次号、工单号或缺陷记录定位最小查询范围。
2. **主体取证**：读取工单、单件过点、测试结果等核心记录。
3. **受限关联展开**：根据白名单查询计划，通过 `work_order_id`、`operation_record_id`、`material_lot_id`、`asset_id`、`operator_id`、`route_version` 等字段追加 5M1E 记录。
4. **版本对齐**：以目标过点记录为基准确认当时工艺版本，禁止用当前版本覆盖历史事实。
5. **证据裁剪**：按问题与时间范围保留相关记录，对重复记录去重，并标注读模型 freshness。
6. **缺口处理**：关联字段为空、接口超时或权限不足时写入 `MissingEvidence`，不得虚构关系。

#### 展开边界

| 限制 | 默认策略 |
|------|---------|
| 关联深度 | 由固定查询计划限定，不开放任意递归 |
| 时间范围 | 优先使用目标过点前后窗口或工单周期 |
| 记录数量 | 每次查询与整次请求均设上限，超出后要求缩小范围 |
| 可查询关联 | 仅允许评审过的 MES 业务关联，禁止客户端传 SQL/表名 |
| 权限 | 每一跳都校验相同租户与数据范围 |
| 超时 | 读模型与 REST 分别设置超时，部分失败不伪装成完整链路 |

追溯结果可以按人、机、料、法、测、环分组，但分组只是证据展示方式。系统只能提出有证据支持的候选原因，不能把相关性直接表述为已确认根因。

### 4.3 Agentic 检索编排

Agentic 的价值是根据证据缺口调整检索，而不是为所有请求增加模型调用。

```text
ANALYZE
   │ 提取实体、约束和证据需求
   ▼
PLAN ───────────────┐
   │                │
   ▼                │
ACT: 调用文档/追溯工具 │
   │                │
   ▼                │
OBSERVE: 规范化证据   │
   │                │
   ├── 缺口可补救且预算未用尽 ──┘
   │
   ▼
COMPOSE: 生成有引用的答案
   │
   ▼
COMPLETE / CLARIFY / INSUFFICIENT_EVIDENCE
```

硬性约束：

- 工具仅包含只读 `document_search`、`trace_lookup`、`record_expand`、`version_resolve` 等白名单能力。
- 默认最大 6 个编排步骤，并限制模型调用次数、工具次数、总时长和证据条数。
- 无新增证据、达到预算或权限/版本冲突时立即停止循环。
- 编排器不能绕过应用层直接访问数据库，也不能传入任意 SQL、URL 或工具名称。
- 深度诊断或报告撰写如需交给 agent-service，应通过明确的外部用例接口委托；rag-service 仍负责提供同一套证据与版本契约。

### 4.4 证据融合与答案生成

`AnswerComposer` 接收证据集合而非原始数据库连接，按以下顺序组织答案：

1. 已核实的制造事实；
2. 与事实对应版本的文档要求；
3. 有证据支持的候选解释或建议；
4. 缺失证据、冲突与下一步人工确认事项。

不同来源冲突时不得由模型投票决定。应展示来源、时间和版本差异，并优先提示人工核验。

---

## 5. 应用层与技术底座

### 5.1 应用服务

| 组件 | 职责 |
|------|------|
| `QueryService` | 统一事务边界与请求编排入口，建立权限、版本、审计上下文 |
| `QueryPlanner` | 提取检索需求，不直接访问基础设施 |
| `RagOrchestrator` | 在预算内调用工具并判断停止条件 |
| `DocumentSearch` | 返回标准化文档证据 |
| `TraceLookup` | 返回标准化追溯证据及缺失项 |
| `AnswerComposer` | 调用 LLM 并校验引用完整性 |
| `DocumentIngestionService` | 管理 RAG 自有文档与索引 |
| `AnswerAuditService` | 保存请求、检索步骤、证据引用和结果摘要 |

### 5.2 技术组件

| 组件 | 职责 |
|------|------|
| LLM 抽象 | 统一模型提供方、超时、重试、token 与延迟观测 |
| Embedding / Reranker | bge-m3 向量化与重排，支持本地化部署 |
| Persistence | MySQL Engine、ChromaDB client、Redis client 的集中管理 |
| Kafka | 统一事件 envelope、幂等、位点与错误处理 |
| MES ACL | 只读 httpx 客户端，统一超时、租户和 `traceparent` 透传 |
| Observability | OTel tracing、Prometheus metrics、结构化日志与脱敏 |
| Web | FastAPI lifespan、统一异常、DI、健康检查 |

基础设施可以复用，但不以“被几条路线复用”为归属标准；只要属于当前统一服务的横切能力，就集中定义接口和配置。

---

## 6. 对外接口

| 端点 | 方法 | 用途 | 调用方 |
|------|------|------|--------|
| `/rag/query` | POST | 统一问答；按需组合文档、追溯与 Agentic | 前端、agent-service |
| `/rag/answers/{answer_id}/evidence` | GET | 查看答案证据、引用和检索步骤摘要 | 前端、审计 |
| `/rag/documents/ingest` | POST | 摄入或更新 RAG 自有知识文档 | 文档管理员 |
| `/rag/documents/{document_id}/status` | GET | 查看解析、发布与索引状态 | 文档管理员 |
| `/health` | GET | 进程存活 | K8s |
| `/ready` | GET | MySQL、ChromaDB、Redis、Kafka 与关键只读依赖状态 | K8s |
| `/metrics` | GET | Prometheus 指标 | Prometheus |

文档检索和追溯检索作为内部工具 Port 暴露给编排器。若 agent-service 需要单独检索，可复用 `/rag/query` 的 `RETRIEVAL_ONLY` 模式，并传入主题线索，避免维护两套互相漂移的查询契约。

所有业务接口统一返回 `trace_id`；错误响应区分权限不足、版本冲突、依赖不可用、证据不足与请求预算耗尽。

---

## 7. 外部集成

### 7.1 与 MES

- 通过 Kafka 消费经过契约评审的领域事件，更新文档索引或关系型追溯读模型。
- 通过 ACL 调用 MES 只读 REST，禁止写方法和凭据提权。
- 所有事件以 `event_id` 幂等，记录 consumer offset 与读模型 freshness。
- 不订阅与检索无关的高频原始采集流，不将全量原始数据复制到 RAG。
- 任何 RAG 故障都不阻塞 MES 过点和生产写事务。

### 7.2 与 agent-service

- agent-service 将 rag-service 作为统一知识与制造证据提供方。
- 调用时透传 `traceparent`、租户上下文和主题线索，接收统一 `RagAnswer` 或证据集合。
- 诊断和报告生成可以引用 RAG 证据，但不得绕过 RAG 自行拼装另一套文档/追溯检索实现。
- rag-service 的 Agentic 仅负责检索与证据组织；跨领域业务流程仍由相应应用服务负责。

---

## 8. 数据一致性与版本传递

```text
MES 领域事件 / 只读记录
        │
        ├── 文档版本与发布状态 ──> chunk metadata ──> Document Evidence
        │
        └── 制造事实与版本字段 ──> 关系型读模型 ────> Trace Evidence
                                                │
                                                ▼
                                      统一 VersionAnchor 校验
                                                │
                                                ▼
                                     RagAnswer / agent-service
```

- 文档与追溯证据必须使用可比较的版本语义，不能只做字符串拼接后交给模型判断。
- 历史问题以目标制造记录的版本为准；当前作业指导以当前 ACTIVE/PUBLISHED 版本为准。
- 读模型滞后时必须展示 freshness；对强一致要求的查询直接读取 MES 只读接口或提示暂不可答。
- 文档升版追加新数据，不覆盖历史 chunk；追溯读模型可按事件更新，但审计中保留证据来源与读取时间。

---

## 9. 只读、安全与启动断言

| Gate / 策略 | 断言内容 |
|-------------|---------|
| `ReadOnlyMesClient` | 仅允许白名单 GET/只读查询接口，禁止 create/update/delete/complete 等写动作 |
| `ReadOnlyToolPolicy` | Agentic 工具注册必须声明只读，拒绝动态加载未评审工具 |
| `RetrievalScopePolicy` | 每个文档与追溯 Port 调用都必须包含租户与数据范围 |
| `VersionPolicy` | 工艺相关检索缺少可解析版本时不得返回确定性的作业指导 |
| `RawDataTopicPolicy` | 禁止订阅未评审的原始高频数据主题 |
| `QueryPlanPolicy` | 追溯只允许预定义关联查询，限制深度、记录数和时间窗口 |
| `CitationPolicy` | 生成后校验关键事实引用；引用为空或证据不匹配时降级为拒答/澄清 |

启动时校验工具、ACL 和配置；运行时在每次调用执行权限与预算检查。启动断言不能替代运行时授权。

---

## 10. 存储与 Schema

| 存储 | 内容 | 管理方式 |
|------|------|---------|
| MySQL `rag_knowledge` | 文档元数据、版本、绑定、摄入状态 | Alembic |
| MySQL `rag_trace` | 关系型追溯读模型、查询所需索引 | Alembic |
| MySQL `rag_runtime` | 事件幂等与位点、答案审计、检索步骤摘要 | Alembic |
| ChromaDB | 文档 chunk 向量与过滤元数据 | collection 代码初始化与备份重建 |
| Redis | 短期检索缓存、请求预算与限流状态 | 无业务事实唯一副本 |

不部署 Neo4j，不维护图 Schema。MySQL 中的追溯表是面向查询的读模型，不是 MES 权威数据源，可通过事件重放或只读同步重建。

---

## 11. 可观测与审计

### 11.1 Tracing

统一 span 链：`rag.query -> plan -> document.search/trace.lookup -> llm.compose`。每次工具调用记录耗时、结果数量、过滤条件摘要、版本锚点、读模型位点和错误类型，不记录敏感原文或访问令牌。

### 11.2 Metrics

- `rag_query_duration_seconds`、`rag_query_total`
- `rag_document_retrieval_duration_seconds`、`rag_document_hits`
- `rag_trace_lookup_duration_seconds`、`rag_trace_records`
- `rag_orchestrator_steps`、`rag_tool_calls_total`
- `rag_answer_citation_failures_total`、`rag_insufficient_evidence_total`
- `rag_consumer_lag`、`rag_read_model_freshness_seconds`

### 11.3 审计

审计保存答案 ID、请求摘要、模型与 prompt 版本、工具步骤、证据 ID/来源/版本和最终状态。正文与敏感字段按数据治理要求脱敏或限制保存周期。工程师可以从答案回到原始文档位置或 MES 只读记录。

---

## 12. 部署与故障处理

- 单服务部署，API 与事件消费者可共用代码和配置，按负载拆为不同进程角色，但不改变统一领域模型。
- MySQL、ChromaDB、Redis 和 Kafka 分别配置连接池、超时与就绪检查。
- ChromaDB 不可用时，追溯证据仍可返回，但答案必须注明文档建议缺失。
- 追溯读模型或 MES 只读接口不可用时，可回答有证据的文档问题；制造事实不得由文档内容代替。
- LLM 不可用时，`RETRIEVAL_ONLY` 仍可返回证据；普通问答返回可重试的依赖错误。
- Redis 仅作缓存与限流，故障不应造成证据或权限判断失真。

配置按基础设施和功能策略组织，例如 `rag.document.*`、`rag.trace.*`、`rag.orchestration.*`，用于调参和安全限制，不作为三条独立产品的启用开关。

---

## 13. 评测设计

围绕一个统一 RAG 的能力与端到端结果评测，而不是分别维护三套路线指标。

| 维度 | 关键指标 |
|------|---------|
| 文档检索 | Recall@k、重排命中、引用定位准确率、版本过滤正确率 |
| 制造追溯 | 种子解析准确率、关键记录召回率、错误关联率、版本对齐率、缺口识别率 |
| Agentic | 工具选择正确率、追加检索收益、平均步数、预算超限率、无效应答率 |
| 综合答案 | 忠实度、答案相关性、引用完整率、事实/建议区分、缺失证据表达 |
| 安全 | 租户越权、失效版本泄漏、写操作、无证据制造事实均为阻断项 |
| 性能 | 端到端延迟、工具耗时、token 成本、依赖故障下的降级成功率 |

端到端用例至少覆盖：纯 SOP 查询、单件历史追溯、批次不良与对应版本 SOP 组合、证据不足、版本冲突、读模型滞后和越权访问。

---

## 14. 分阶段交付

阶段划分只表示同一服务的增量交付，不表示引入不同 RAG 路线。

| 阶段 | 交付内容 | 退出标准 |
|------|---------|---------|
| 一：统一检索底座 | 统一契约、权限/版本策略、文档摄入与检索、基础 `/rag/query` | 文档引用与版本过滤评测通过，只读和租户测试通过 |
| 二：非图追溯 | 关系型读模型、MES 只读适配器、固定查询计划、5M1E 证据输出 | 关键记录召回、错误关联、版本对齐和缺口测试通过 |
| 三：Agentic 编排 | 有限状态机、工具预算、补充检索、证据融合和答案审计 | 复杂问题收益明确，步数与成本受限，无证据不硬答 |
| 四：生产治理 | 完整可观测、备份重建、压测、故障演练和评测门禁 | 降级、恢复、重建与安全红线全部通过 |

---

## 15. 关键决策汇总

| # | 决策 | 核心权衡 |
|---|------|---------|
| 1 | 一个 RAG 服务与统一答案契约 | 避免能力被产品化拆分，减少重复编排与契约转换 |
| 2 | 文档、追溯、Agentic 是内部能力 | 可按需组合，但共享权限、版本、证据和审计 |
| 3 | 追溯采用关系型读模型与只读接口 | 复用明确业务关联，避免图数据库和图投影的额外复杂度 |
| 4 | Agentic 仅用于有收益的追加检索 | 控制延迟与成本，简单请求保持确定性链路 |
| 5 | 统一证据模型约束生成 | 文档和制造记录都可审计，缺证据时不硬答 |
| 6 | 版本与权限在检索前强制执行 | 防止失效工艺和跨范围数据进入模型上下文 |
| 7 | RAG 只读并退出生产主事务 | 故障不阻塞生产，模型无写副作用 |
| 8 | 分阶段增量交付同一服务 | 先验证证据质量，再增加多步编排，不以路线拆分进度 |
