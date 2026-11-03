# Agentic RAG 详细设计（统一入口收口 + 意图路由 + 跨能力显式委托）

> 本文是 [RAG服务引入路线.md](RAG服务引入路线.md) §3「Agentic 的正确定位」所述 **Agentic 能力**的落地展开，输出**技术栈、意图路由设计、工具与跨能力委托编排、统一输出、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + AgentScope 2.0 + Pydantic）。Agent 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 REST / Kafka 解耦，互不侵入。
> **底座口径**：与 [基于AgentScope2.0的架构设计.md](../基于AgentScope2.0的架构设计.md) §3.2 与附录速查表、[架构设计v2/00-架构决策记录ADR.md](../架构设计v2/00-架构决策记录ADR.md) ADR-01 对齐（`agentscope>=2.0.0`、Python 3.12+）--统一 `Agent`（内置 ReAct）+ `Toolkit` 承载收口编排；**意图分诊由单 Agent 的 ReAct + `Toolkit` 工具选择承担**（2.0 从未提供 `Routing`/`Handoffs` 原语），跨能力委托是**显式服务调用**（`invoke` 语义由我们自己实现，只传 `evidence_set_id`/`session_id`/`traceparent`，不传消息历史），`AgentState` + `SessionService`（Redis-backed；2.0.5 起旧名 `SessionManager` 已失效）承载跨请求会话。框架只决定"怎么实现"，不改变本文的只读、版本与证据约束。
> **口径纪律**：Agentic RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Agentic RAG"。MES 领域对错误答案零容忍，所以本文强调**收口不造新能力 + 路由委托诊断与草稿 + 继承全部只读红线**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 定位：E = rag-service 的 Agentic 收口入口

[AGENT服务引入路线.md](../AGENT服务/AGENT服务引入路线.md) §6 明确：**rag-service 的 Agentic 能力就是 Agent 的收口形态**。E 不是又一个新能力，而是**统一入口收口层**--一个 Agent 入口，按问题路由到文档检索与制造追溯的工具 + 专精 Agent（[RAG服务引入路线.md](RAG服务引入路线.md) §3「Agentic 的正确定位」）。

- **价值在"收口"不在"新能力"**：文档检索与制造追溯是两项检索能力，诊断 / 草稿 / 编排是三项专精 Agent 能力。用户不该在多个入口间选择该问谁--E 做意图路由，把问题送到对的地方。
- **何时做**：等文档检索、制造追溯与诊断 / 草稿 / 编排三项能力成型后再收口（[RAG服务引入路线.md](RAG服务引入路线.md) §3、[AGENT服务引入路线.md](../AGENT服务/AGENT服务引入路线.md) §5）。否则 E 没工具可调，退化为套壳问答。**E 是最后一步，不是第一步**。

### 1.2 目标

把文档检索与制造追溯两项检索能力 + 诊断 / 草稿 / 编排三项 Agent 能力收口成**一个统一入口**，让用户用自然语言提问，E 自动判断"这问题该由哪项能力答"，路由到对应工具或委托对应专精 Agent，返回统一格式的答案 + 来源引用 + 工具链。

典型场景：

1. **追溯诊断类**："SN-001 焊接不良根因" -> E 识别为深度诊断 -> **显式委托诊断能力**（多步 5M1E 推理，跨能力服务调用）-> 返回根因假设
2. **文档查询类**："SPI 报警怎么处置" -> E 识别为文档检索 -> 调文档检索的 `search_docs` 工具 -> 返回 SOP 片段 + 引用
3. **追溯事实类**："SN-001 过了哪几站" -> E 识别为追溯检索 -> 调制造追溯的 `query_traceability` 工具 -> 返回关系型证据子集
4. **草稿生成类**："给这批不良草拟返工单" -> E 识别为写意图 -> **显式委托草稿能力**（跨能力服务调用，草稿不落库）-> 返回返工单草稿供工程师确认

### 1.3 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **收口不造新能力** | E 只做路由 + 轻量组合 + 委托，不重建检索/推理/草稿能力 | E 无自有向量库与追溯读模型，全部调两项检索能力与三项 Agent 能力 |
| **只读（继承诊断能力）** | E 全程只读，无写工具；写意图委托草稿能力但草稿能力也只草拟不落库 | `ReadOnlyToolGate` 启动断言；写工具不注册到 E 的 ToolRegistry；主闸是工具网关的一次性动作令牌（E 不领用任何写令牌），框架 `PermissionEngine` 只是叠加层（§7.3） |
| **不进过点主事务** | E 不调过点引擎放行/拦截 API | 过点 toolset 不暴露 `pass/judge` 类工具（继承诊断能力 §1.2） |
| **版本一致性** | 查工艺带版本锚点（`version`+`version_kind`）；文档检索带版本过滤 | 工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承文档检索、制造追溯与诊断能力） |
| **权限隔离** | 工具调用前按 `tenant_scope` 过滤 | 路由 + 工具调用都带 `TenantContext`，前置过滤（继承诊断能力 §4.3） |
| **可观测兜底** | 每答案带工具链 + 来源引用 + 置信度；低置信度转人工 | trace 从 E 发起串联到专精 Agent/工具；置信度阈值兜底 |
| **委托而非重复** | 深度诊断委托诊断能力，不自己多步推理；写意图委托草稿能力，不自己草拟 | E 的步数与工具调用预算设得很小（轻量组合），深度场景显式委托诊断能力（跨能力服务调用，非框架移交机制） |

### 1.4 与诊断 / 草稿 / 编排三项能力的分工（核心边界）

E 是"广而浅"的收口层，诊断 / 草稿 / 编排三项能力是"深而专"的专精 Agent。E 不重复它们的能力，而是**路由 + 委托**：

| 维度 | E（本文，收口入口收口） | 诊断 | 草稿 | 流程编排 |
|------|-------------------|---------|---------|---------|
| 形态 | 意图路由 + 轻量组合 + 委托 | 多步只读 ReAct 诊断 | 写意图草稿（不落库） | 跨上下文流程编排 |
| 推理深度 | 浅（单工具或 1-2 组合） | 深（≤10 步 5M1E 推理） | 中（拉数据 + 草拟） | 深（多步流程 + 人在回路） |
| 步数与工具调用预算 | 小（最多 1-2 次工具调用） | 约 10 次工具调用（[诊断能力](../AGENT服务/FactoryAgent/FactoryAgent-诊断能力.md) §5.1） | 中 | 中 |
| 写动作 | 无（委托草稿能力） | 无（只读） | 草稿不落库 | 受限写 + confirmation gate |
| 典型问题 | "这问题该由哪项能力答" | "根因是什么" | "草拟返工单" | "换线编排" |

> **reconcile 两条文档的张力**：[RAG服务引入路线.md](RAG服务引入路线.md) §3 说 Agentic 能"在预算内完成多步组合"，[AGENT服务引入路线.md](../AGENT服务/AGENT服务引入路线.md) §0 说 Agent 相对 RAG 的增量是"多步规划 / 工具调用"，§2.1 把"自己决定下一步查什么"的深度推理归给诊断能力。解法：E 做**轻量 ReAct**（能选工具、能组合 1-2 个工具给答案，比单次 RAG 进一步），但**深度跨上下文多步诊断委托诊断能力**。E 的"多步"是相对单次检索而言，不是诊断能力那种"自己决定下一步查什么"的深度推理。这样既满足"能多步推理"的收口定位，又不与诊断能力的专精诊断重复。

### 1.5 与两项检索能力的关系

| rag-service 检索能力 | 本文如何使用 | 形态 |
|---------|-----------|------|
| **制造追溯** | 封装为 `query_traceability` 工具，E 直接调（简单追溯）或显式委托诊断能力（深度诊断） | 工具 + 专精 Agent |
| **文档检索** | 封装为 `search_docs` 工具，E 直接调 | 工具 |

> **向量库口径风险注**：向量召回由文档检索能力自持，E 只调 `search_docs`、不碰向量库。若文档检索侧经 AgentScope RAG 模块接入向量库，须记住该模块**没有 ChromaDB 适配器**（已核实可选后端为 Qdrant / MilvusLite / Elasticsearch / MongoDB），且 `rag/` 模块在 **2.0.0 曾被整体删除、2.0.5 起才回归**--必须 pin 到具体小版本并保留降级预案（证据见 ADR-01 的 RAG 模块行）。


### 1.6 与 Java 技术栈的关系

- E 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只通过 REST 调 rag-service 的检索接口与 agent-service 三项能力的只读接口。
- 跨语言物理边界天然强制只读：E 无法共享 Java 事务/内存，无法进过点主事务、无法旁路应用服务写路径（与诊断能力同构，[FactoryAgent-诊断能力.md](../AGENT服务/FactoryAgent/FactoryAgent-诊断能力.md) §1.3）。
- E 是纯编排层，复用两项检索能力与三项 Agent 能力的既有契约，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.12+ | 与 agent-service 三项能力同栈，复用 AgentScope + Model 抽象 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI，统一问答 HTTP 入口 |
| Agent 底座 | **AgentScope 2.0**（`agentscope>=2.0.0`） | 统一 `Agent` 类（内置 ReAct）+ `Toolkit` 做"路由 -> 工具/委托 -> 收口"；与诊断能力同底座，深度场景把诊断能力作为**显式服务调用**的委托目标（框架无移交原语，`invoke` 语义自实现） |
| LLM 抽象 | AgentScope `Model` | provider 可插拔（DashScope / Ollama / OpenAI 兼容），与 agent-service 一致 |
| 数据校验 | **Pydantic v2** | 意图分类/工具入参/统一答案 DTO 的 schema 即类型 |
| HTTP 客户端 | **httpx**（异步） | 调 rag-service 检索 + agent-service 三项能力只读 REST |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 路由 trace、统一答案审计 |
| 会话状态 | AgentScope `AgentState` + `SessionService`（Redis-backed） | 跨请求会话恢复，与诊断能力同一套机制；⚠️ 旧名 `SessionManager` 自 2.0.5 起已改名失效，不可按旧名写代码 |
| 缓存 | **redis-py (async)** | 相同问题短缓存（同问题重复问不重跑路由/工具） |
| 可观测 | AgentScope `TracingMiddleware` + **OpenTelemetry Python** + **Tempo/Jaeger** + `prometheus-client` | trace 从 E 发起串联到专精 Agent/工具；答案流回放走 `reply_stream` 事件。⚠️ 不选 `AgentScope Studio`（官方仓库已归档，不再作为独立工具维护） |
| 编排组合 | **应用层 Python**：顺序 `await` 串联 + `asyncio.gather` 并发独立 `Agent.reply()` | 2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 已删除，无框架 pipeline 可依赖；编排代码在本仓库统一落在 `flows/` 目录 |
| 评测 | **自研 `EvalRunner`**（断言/指标/judge 三层判定 + pass^k + CI 门禁）+ 独立包 **`py-openjudge`** 作 LLM-as-judge | ⚠️ 不存在"内生评测栈"：`agentscope.evaluate` 在 2.0 已删除且未回归，评测必须自建 |
| 配置 | pydantic-settings | 环境变量统一管理 |
| 部署 | 独立微服务 `agent-gateway-service`（uvicorn + gunicorn worker） | 与三大服务同网格，K8s 部署 |

### 2.2 为什么 E 用 AgentScope 轻量编排而非裸路由

- E 的核心是"路由 + 轻量组合"：多数问题单工具即答，少数需组合 1-2 工具。用 AgentScope 统一 `Agent`（内置 ReAct）承载"意图路由 -> 工具执行 -> 收口"：**分诊与工具选择就是这一个 Agent 的 ReAct 循环 + `Toolkit`**（2.0 从未提供 `Routing`/`Handoffs` 原语，别把分诊写成框架能力）；跨能力委托走**显式服务调用**（`invoke` 语义由我们自己实现）。组合形态：顺序固定的组合=应用层 `await` 串联；并行=`asyncio.gather` 并发多个**各自独立**的 `Agent.reply()`，结论在应用层显式合并（2.0.0 起 `agentscope/pipeline` 与 `agentscope/msghub` 模块已删除，`sequential_pipeline`/`fanout_pipeline`/`MsgHub` 都不可依赖）；编排代码在本仓库落在 `flows/` 目录。
- **不存在共享消息上下文的原语**：`MsgHub` 式"A 根因 + B 隔离范围互相看到彼此结论"在 2.0 无支撑。本设计的替代：各分支只读同一份 `evidence_set_id`（关系型证据子集），彼此**不**看对方中间结论--刻意如此，扇出式互看会放大错误（无中心校验 17.2× vs 中心化 4.4×）。
- 与诊断能力同底座的好处：E 对诊断能力发起**显式服务调用**（同一个底座、同一套 `Toolkit`/ACL 形态），trace / 会话（`AgentState` + `SessionService`）复用一套机制（§5.3）；委托时只传 `evidence_set_id` / `session_id` / `traceparent`，不传消息历史。
- AgentScope 没有"递归步数上限"这类单一参数：E 的"不做深度推理"靠应用层独立计算**工具调用次数、时长与证据预算**（最多 1-2 次工具调用）来保证--深度场景显式委托诊断能力。

### 2.3 为什么 E 不重建检索/推理能力

- 文档检索与制造追溯已建好检索能力（向量召回 + 关系型追溯读模型），agent-service 已建好三项 Agent 能力（诊断/草稿/编排）。E 重建等于重复造轮子 + 多份事实源。
- E 的价值在"路由判断"--把问题送到对的能力，而非自己再做一遍。这符合"整合而非拆分"的纪律：收口成单入口，不另起能力。
- 委托而非重复：深度诊断委托诊断能力，E 不自己多步推理；写意图委托草稿能力，E 不自己草拟。E 是编排层，不是能力层。

### 2.4 部署形态（车间网隔离）

- E 是统一入口，部署在办公网/车间网均可访问的位置。LLM 视安全策略二选一（云端 API 或本地化模型），AgentScope `Model` 的 provider 可插拔保证切换零代码改动。
- E 依赖 rag-service 与 agent-service 的可达性：下游服务故障时 E 降级（§10.3），不硬答。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ agent-gateway-service（统一入口，Python + FastAPI + AgentScope）   │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ GatewayService                            │  │
│  │ /agent/chat  │  │  构建路由 Agent + 驱动                    │  │
│  └──────────────┘  └────────────┬─────────────────────────────┘  │
│                                 │                                  │
│                      ┌──────────▼──────────┐                       │
│                      │ 应用层路由编排     │                       │
│                      │ router -> tool/invoke -> 收敛  │              │
│                      └──────────┬──────────┘                       │
│                                 │                                  │
│         ┌───────────────────────┼───────────────────────┐          │
│         ▼                       ▼                       ▼          │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐    │
│  │ IntentRouter │    │ ToolExecutor     │    │  AgentDelegator   │ │
│  │ NL -> 意图/路由 │    │ 检索工具调用  │    │ 显式委托诊断/草稿 │ │
│  └──────────────┘    └────────┬─────────┘    └──────┬───────┘    │
│                               │                     │            │
│                       ┌───────▼─────────┐           │            │
│                       │ ToolRegistry    │           │            │
│                       │ (只读白名单)     │           │            │
│                       └─────────────────┘           │            │
│                                                     │            │
│  ┌──────────────────┐  ┌──────────────────┐         │            │
│  │ AnswerAudit      │  │ QueryCache(Redis)│         │            │
│  └──────────────────┘  └──────────────────┘         │            │
└─────────────────────────────────────────────────────┼────────────┘
                      │ httpx 只读 REST               │
        ┌─────────────┼───────────────────────────────┼────────┐
        ▼             ▼                 ▼        ▼
  ┌──────────┐  ┌──────────┐    ┌──────────┐ ┌──────────┐
  │ 制造追溯  │  │ 文档检索  │    │ 诊断   │ │ 草稿   │
  │ /rag/trace│  │ /rag/docs │    │ /agent/   │ │ /agent/   │
  └──────────┘  └──────────┘    │ diagnose  │ │ draft    │
                                └──────────┘ └──────────┘
```

### 3.1 关键设计决策

- **路由即核心**：`IntentRouter` 是 E 的命脉--路由错了全盘皆错。规则优先（高频问题关键词命中）+ LLM 兜底分类，降低对模型依赖。
- **工具与委托分离**：`ToolExecutor`（调检索工具，单次或轻量组合）与 `AgentDelegator`（对诊断 / 草稿专精 Agent 发**显式服务调用**；框架无移交原语，`invoke` 语义自研）解耦。简单查询走工具直答，深度场景走委托--单一职责（SRP）。
- **统一输出**：所有路径（工具直答/显式委托）都收敛到 `AgentAnswer` 统一格式，用户看到一致体验。
- **trace 从 E 发起**：E 是入口，`trace_id` 从 E 生成，透传到诊断 / 草稿与检索工具，全链路可回溯。

---

## 4. 意图路由设计

意图路由是 E 的核心。这一节定义"问题 -> 命中哪条路线"的分类规则。

### 4.1 意图分类（IntentCategory）

意图严格对齐文档检索、制造追溯与诊断 / 草稿的能力边界，不另造分类：

| 意图 | 路由到 | 形态 | 典型问题 |
|------|--------|------|---------|
| `TRACE_FACT`（追溯事实） | 制造追溯工具 `query_traceability` | 工具直答 | "SN-001 过了哪几站""这批用了哪批锡膏" |
| `ROOT_CAUSE`（根因诊断） | 诊断能力（显式委托） | 委托 | "SN-001 焊接不良根因""这批不良 5M1E" |
| `DOC_LOOKUP`（文档查询） | 文档检索工具 `search_docs` | 工具直答 | "SPI 报警怎么处置""首件检验流程" |
| `DRAFT_REQUEST`（草稿生成） | 草稿能力（显式委托） | 委托 | "给这批不良草拟返工单""草拟 8D 报告" |
| `UNKNOWN`（未识别） | 转人工 | 兜底 | 路由不了的问题 |

> **TRACE_FACT vs ROOT_CAUSE 的边界**：问"过了哪几站"是事实检索（制造追溯工具直答），问"为什么不良"是根因诊断（委托诊断能力）。前者一次关系型追溯查询即答，后者需多步 5M1E 推理。E 的路由器按"是否需多步推理"区分：事实类走制造追溯工具，诊断类委托诊断能力。

### 4.2 路由规则（规则优先 + LLM 兜底）

```text
IntentRouter.classify(question)
   │
   ├─ 1. 规则优先（高频问题关键词命中）
   │     ├─ 含"根因/5M1E/为什么不良" -> ROOT_CAUSE -> 委托诊断能力
   │     ├─ 含"怎么处置/SOP/怎么修/流程" -> DOC_LOOKUP -> 文档检索工具
   │     ├─ 含"草拟/生成返工单/8D" -> DRAFT_REQUEST -> 委托草稿能力
   │     └─ 含"过了哪几站/用了哪批/位置" -> TRACE_FACT -> 制造追溯工具
   │
   ├─ 2. LLM 兜底分类（规则命中不了）
   │     以 Content Block（Pydantic BaseModel）强制枚举返回
   │
   └─ 3. 仍不确定 -> UNKNOWN -> 转人工
```

- **规则优先**：高频问题不走 LLM，确定性强、成本低。只有规则命中不了才走 LLM 意图分类。
- **LLM 兜底强约束**：`IntentCategory` 固化为 Enum，模型只能选枚举值，避免编造意图。
- **UNKNOWN 兜底**：路由不了的问题不硬答，转人工"该问题暂不支持，请联系工程师"。

### 4.3 路由到工具 vs 专精 Agent 的决策

| 意图 | 单步还是多步 | 路由 |
|------|------------|------|
| TRACE_FACT / DOC_LOOKUP | 单步（一次工具调用即答） | `ToolExecutor` 直答 |
| ROOT_CAUSE | 多步（深度诊断） | `AgentDelegator` 显式委托诊断能力（服务调用） |
| DRAFT_REQUEST | 多步（拉数据 + 草拟） | `AgentDelegator` 显式委托草稿能力（服务调用） |
| 少数需组合的（如"SN-001 根因 + 处置 SOP"） | 轻量组合（1-2 工具） | E 的统一 `Agent` 轻量 ReAct（工具调用预算 1-2 次） |

- **单步直答**：多数问题一次工具调用即答，E 不做多步。
- **深度委托**：需多步推理的显式委托诊断与草稿，E 不自己推理。注意这是**我们的服务调用**（`invoke` 语义自实现，传 `evidence_set_id`/`session_id`/`traceparent`），不是框架移交机制--AgentScope 2.0 从未提供 `Handoffs`/`Routing` 原语，分诊与选路整体由单 Agent 的 ReAct + `Toolkit` 工具选择承担。
- **轻量组合**：少数问题需组合 1-2 工具（如先查追溯再查 SOP），E 用严格受限的工具调用预算处理--这是 E 唯一自己做"多步"的场景，次数与时长由应用层独立计算并硬约束。

---

## 5. 工具注册与跨能力委托（显式服务调用）

### 5.1 工具注册（对齐两项检索能力，只读白名单）

E 的 `Toolkit` 注册文档检索与制造追溯的检索工具（各为一个 AgentScope `Tool`），全部只读：

| 工具名 | 所属能力 | 入参 | 版本校验 |
|--------|-----------|------|---------|
| `query_traceability` | 制造追溯 | sn/batch_no/wo_id, as_of, version, version_kind | 历史回放带版本锚点 |
| `search_docs` | 文档检索 | query, version, version_kind | 强制版本锚点过滤 |

```python
class ToolDescriptor(BaseModel):
    name: str
    description: str
    route: str                        # "traceability" / "doc_search"
    read_only: bool                   # E 必须 True
    args_schema: type[BaseModel]
    required_tenant_scopes: list[str]
```

- `read_only=False` 的工具在 E 的 `ToolRegistry` 启动时直接拒绝注册（继承诊断能力 `ReadOnlyToolGate`）。
- 工具调用前按 `TenantContext` 权限过滤（继承诊断能力 §4.3）。
- 工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承文档检索、制造追溯与诊断能力的版本一致性）。

### 5.2 专精 Agent 委托（AgentDelegator / 显式服务调用）

`AgentDelegator` 把深度场景以**显式服务调用**委托给诊断与草稿专精 Agent（`invoke` 语义由我们自己实现，2.0 框架没有移交原语），E 不自己推理：

```python
class AgentDelegator:
    """显式委托诊断与草稿 专精 Agent：跨能力服务调用，不是框架移交机制。
    上下文只传 evidence_set_id / session_id / traceparent，不传消息历史。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def invoke_diagnosis(
        self, question: str, evidence_set_id: str, session_id: str,
        traceparent: str, tenant: TenantContext
    ) -> Report:
        """委托诊断能力（多步 5M1E 推理）。"""
        resp = await self._http.post(
            "/agent/diagnose",
            json={"question": question, "evidence_set_id": evidence_set_id,
                  "session_id": session_id},
            headers={**tenant.headers(), "traceparent": traceparent},
            timeout=60.0,  # 诊断能力整会话 ≤60s
        )
        resp.raise_for_status()
        return ReportMapper.to_view(resp.json())

    async def invoke_draft(
        self, draft_kind: str, evidence_set_id: str, session_id: str,
        traceparent: str, tenant: TenantContext
    ) -> Draft:
        """委托 草稿编写能力（草稿不落库）；证据经 evidence_set_id 回查，不带消息历史。"""
        resp = await self._http.post(
            "/agent/draft",
            json={"kind": draft_kind, "evidence_set_id": evidence_set_id,
                  "session_id": session_id},
            headers={**tenant.headers(), "traceparent": traceparent},
            timeout=30.0,
        )
        resp.raise_for_status()
        return DraftMapper.to_view(resp.json())
```

- **委托不是框架移交，也不是单次工具调用**：诊断与草稿是专精 Agent（多步推理/草拟），E 把整个问题交给它们处理；承载方式是 httpx 出站调用（可封装成 `Toolkit` 里的一个只读 Tool 供 Agent 选择），语义由我们定义。
- **只传标识，不传消息历史**：委托上下文只有 `evidence_set_id` / `session_id` / `traceparent`；被委托方按 `evidence_set_id` 自行回查关系型证据子集。
- **超时对齐专精 Agent**：诊断能力整会话 ≤60s（[诊断能力](../AGENT服务/FactoryAgent/FactoryAgent-诊断能力.md) §5.1），E 的委托超时设 60s+。
- **结果转统一格式**：诊断能力的 `DiagnosisReport` / 草稿能力的草稿都经 Mapper 转成 E 的 `AgentAnswer`，用户看到一致体验。

### 5.3 轻量路由编排（统一 Agent 的 ReAct + `Toolkit` 工具选择）

E 的编排是轻量的"路由 -> 执行/委托 -> 收口"：

```text
[意图分诊] 单 Agent 的 ReAct + Toolkit 工具选择（无框架 Routing 原语）
      │
      ├─ 单步意图 ──▶ [Toolkit 工具] 调检索工具 ──▶ [收口 Agent]
      ├─ 委托意图 ──▶ [显式服务调用] 诊断与草稿 ──▶ [收口 Agent]
      ├─ 轻量组合 ──▶ [统一 Agent 内置 ReAct]（工具预算 1-2 次）──▶ [收口 Agent]
      └─ UNKNOWN ──▶ [收口 Agent] 转人工兜底
```

- **步数与工具调用预算**：E 自己最多组合 1-2 工具，由应用层独立计算工具调用次数、时长与证据量（AgentScope 无"递归上限"这类单一参数名）。预算超限抛应用层 `BudgetExceededError`，降级为"建议改用诊断能力深度诊断"或转人工。
- **组合形状**：顺序固定的多步=应用层 `await` 串联；确需并行=`asyncio.gather` 并发多个**各自独立**的 `Agent.reply()`，结论在应用层显式合并，编排代码落在本仓库的 `flows/` 目录。2.0 不提供共享消息上下文的原语（`agentscope/pipeline`、`agentscope/msghub` 自 2.0.0 起已删除）；各分支只读同一份 `evidence_set_id`（关系型证据子集），彼此不看对方中间结论。
- **与会话/trace 复用诊断能力机制**：E 的会话状态同样由 `AgentState` + `SessionService` 承载（可落 SQLAlchemy/Redis），trace 经 `TracingMiddleware` 透传到被委托的服务。

---

## 6. 统一输出与可观测

### 6.1 统一答案（AgentAnswer）

所有路径收敛到 `AgentAnswer`，用户看到一致体验；答案本身以 AgentScope Content Block（Pydantic BaseModel）挂在 `Msg` 上返回：

```python
class AnswerSource(BaseModel):
    """答案来源引用（可回溯）。"""
    source_type: str          # "trace_record" / "sop_doc" / "diagnosis_hypothesis"
    ref: str                  # "evidence_id=CheckpointRecord:xxx" / "SOP:WELD-014@v3" / "audit_id=..."
    route: str                # "traceability" / "doc_search" / "diagnosis" / "drafting"

class AgentAnswer(BaseModel):
    question: str
    intent: str               # 命中的 IntentCategory
    route_taken: str          # 实际走的能力路径 "traceability" / "diagnosis" / "doc_search+traceability" ...
    summary: str              # 答案摘要
    detail: dict              # 能力相关的结构化详情（证据集/SOP/图表/报告）
    evidence_set_id: str      # 本次证据集标识，供回查证据集（关系型证据子集，非图引用）
    sources: list[AnswerSource]  # 来源引用
    confidence: float
    tool_chain: list[str]     # ["query_traceability", "search_docs"] 或 ["诊断能力:diagnose"]
    trace_id: str
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"
```

- **`route_taken` + `tool_chain` 透明**：用户/工程师能看到"这个答案走了哪项能力、调了哪些工具"--收口入口的路由可观测。
- **`sources` 强制引用**：每个答案带来源（SOP 带版本、追溯证据 ID、诊断能力假设证据），可点开回溯；回查按 `evidence_set_id` 取证据集，不使用图节点引用。
- **`disclaimer` 不可省**：E 是辅助，最终处置需工程师确认。

### 6.2 trace 串联

- E 是入口，`trace_id` 从 E 生成，`TracingMiddleware`（接 OpenTelemetry）在 `IntentRouter`、`ToolExecutor`、`AgentDelegator` 都注入 span。
- 显式委托诊断与草稿时透传 `traceparent` header（连同 `evidence_set_id` / `session_id`，不传消息历史），诊断与草稿的工具调用 trace 与 E 的 trace 串联--工程师可从 E 的答案回溯到诊断能力的每步工具调用。
- `AnswerAudit` 记录问题/意图/路由/工具链/答案摘要/`trace_id`，可回溯。

---

## 7. 实现方案

### 7.1 网关编排服务（GatewayService）

> 下面是**编排形状**（单 Agent 的 ReAct + `Toolkit` 工具选择做分诊 + 应用层预算 + `SessionService` 会话恢复）。原语名已按 [ADR-01](../架构设计v2/00-架构决策记录ADR.md)（源码树 v2.0.11 核对）剔除不存在/已删除项；具体构造参数与方法签名仍以官方源码树为准。

```python
class GatewayService:
    def __init__(
        self,
        route_agent_factory: RouteAgentFactory,   # 按意图装配统一 Agent + Toolkit
        session_service: SessionService,          # AgentState 会话恢复（Redis-backed）
        intent_router: IntentRouter,
        tool_executor: ToolExecutor,
        delegator: AgentDelegator,
        cache: QueryCache,
        audit_repo: AnswerAuditRepo,
    ) -> None: ...

    async def chat(self, request: ChatRequest, tenant: TenantContext) -> AgentAnswer:
        # 1. 缓存（同问题 + 租户命中即用）
        cached = await self._cache.get(request, tenant)
        if cached:
            return cached
        # 2. 意图分诊（应用层规则优先 + LLM 兜底；框架无 Routing 原语）
        intent = await self._intent_router.classify(request.question)
        # 3. 装配并驱动统一 Agent（内置 ReAct + Toolkit；深度场景经委托 Tool 发显式服务调用）
        #    budget 由应用层独立计算：工具调用次数 ≤1-2、证据条数上限、剩余时长
        agent = self._route_agent_factory.build(intent, tenant, self._budget_policy.for_intent(intent))
        await agent.load_state(await self._session_service.load(request.session_id))
        msg = await asyncio.wait_for(
            agent.reply(Msg(name="user", content=request.question, role="user")),
            timeout=70.0,  # 略大于 诊断能力的 60s 委托超时
        )
        # 4. 收敛统一答案（AgentAnswer 作为 Content Block 挂在 Msg 上）
        answer = self._build_answer(request, intent, msg)
        # 5. 会话状态落盘 + 审计 + 缓存
        await self._session_service.save(request.session_id, agent.state)
        await self._audit_repo.record(request, intent, answer)
        await self._cache.set(request, tenant, answer)
        return answer
```

- 编排与路由/执行/委托分离（SRP）。
- 缓存按"问题 + 租户"键，同问题重复问不重跑。

### 7.2 ACL 防腐层（调检索工具与诊断 / 草稿）

```python
class TraceRagAclClient:
    """制造追溯只读 ACL。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def query_traceability(
        self, seed: dict, as_of: datetime, tenant: TenantContext
    ) -> TraceEvidenceSetView:
        resp = await self._http.post(
            "/rag/trace/expand", json={"seed": seed, "as_of": as_of.isoformat()},
            headers=tenant.headers(), timeout=5.0,
        )
        resp.raise_for_status()
        return TraceEvidenceSetMapper.to_view(resp.json())


class DelegationClient:
    """诊断能力显式委托（跨能力服务调用）ACL——invoke 语义由我们实现，非框架原语。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def invoke(
        self, question: str, evidence_set_id: str, session_id: str,
        traceparent: str, tenant: TenantContext
    ) -> ReportView:
        resp = await self._http.post(
            "/agent/diagnose",
            json={"question": question, "evidence_set_id": evidence_set_id,
                  "session_id": session_id},   # 只传标识，不传消息历史
            headers={**tenant.headers(), "traceparent": traceparent},
            timeout=60.0,
        )
        resp.raise_for_status()
        return ReportMapper.to_view(resp.json())
```

- 外部 DTO 不进 E 核心，只暴露 `TraceEvidenceSetView`/`ReportView`（ACL 约束）。
- 委托超时对齐专精 Agent（诊断能力 ≤60s）。
- 委托 Tool 可注册进 `Toolkit`（只读语义）供统一 `Agent` 选择，但其本质是**显式服务调用**，不是框架移交。

### 7.3 红线继承（只读 + 版本 + 权限）

E 继承诊断能力的全部红线，不因"收口"而放松：

1. **只读闸**：`ReadOnlyToolGate` 启动断言，无写工具注册。
2. **版本闸**：工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。
3. **权限闸**：工具调用前按 `TenantContext` 过滤。
4. **不进过点主事务闸**：过点 toolset 不暴露 `pass/judge`，

> **承载分层（与 ADR-04 一致，红线语义不变）**：主闸在**工具网关**--出站写调用必须携带与动作一一绑定的**一次性动作令牌**，网关校验并消费后即失效，过期或校验失败一律 fail closed；框架 `PermissionEngine`/`PermissionRule`/`PermissionMode`（`EXPLORE` 档位）与 `event.RequireUserConfirmEvent` 只是**叠加层**，因为 Permission 判定发生在 Agent 循环内，绕开循环直接调函数即失效。E 全程只读，**不领用任何写令牌**，本仓库的 `readonly_policy`/`nowrite_policy` 规则集即挂在这两层上。

---

## 8. 推荐包结构（Python src layout）

```text
agent_gateway_service/
  app/
    api/
      chat_router.py           # /agent/chat, /agent/explain
      schemas.py
    application/
      gateway_service.py       # GatewayService
      intent_router.py         # IntentRouter（规则优先 + LLM 兜底）
      flows/                   # 轻量组合编排（普通 Python：顺序 await + asyncio.gather；2.0 无 pipeline 原语）
    domain/
      intent.py                # IntentCategory 枚举
      answer.py                # AgentAnswer / AnswerSource
      tool.py                  # ToolDescriptor / ToolRegistry / ReadOnlyToolGate
      tenant.py                # TenantContext
    infrastructure/
      ai/
        route_agent_factory.py # 统一 Agent + Toolkit 装配（工具选择 + 委托 Tool）工厂
        model_factory.py       # AgentScope Model 接入（provider 可插拔）
        budget_policy.py       # 应用层工具次数/时长/证据预算
      session/
        manager.py             # AgentState + SessionService（Redis-backed；旧名 SessionManager 自 2.0.5 失效）
      acl/                     # 调 rag-service 检索 + agent-service 诊断 / 草稿
        trace_rag.py           # A
        doc_rag.py             # B
        diagnosis_delegation.py       # 诊断能力委托（显式服务调用，非框架移交）
        draft_delegation.py       # 草稿能力委托（显式服务调用，非框架移交）
      persistence/
        models.py              # answer_audit / route_trace
        audit_repo.py
      redis_/
        query_cache.py
      obs/
        tracing.py             # 只做 AgentScope TracingMiddleware 的接线（不自研 trace 层）
        metrics.py             # 网关自有业务指标（prometheus-client）
    config.py
    main.py                    # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
```

- `domain/tool.ReadOnlyToolGate` 启动断言（继承诊断能力）。
- `infrastructure/acl/` 是防腐层，调 rag-service 检索与 agent-service 诊断 / 草稿，外部 DTO 经 Mapper 转内部视图。
- `infrastructure/ai/route_agent_factory` 装配 AgentScope 轻量编排（与诊断能力的 `Agent` 装配同底座）。

---

## 9. 关键代码骨架

### 9.1 意图路由器（IntentRouter）

```python
class IntentCategory(str, Enum):
    TRACE_FACT = "TRACE_FACT"
    ROOT_CAUSE = "ROOT_CAUSE"
    DOC_LOOKUP = "DOC_LOOKUP"
    DRAFT_REQUEST = "DRAFT_REQUEST"
    UNKNOWN = "UNKNOWN"

class IntentRouter:
    """NL -> 意图分类。规则优先，LLM 兜底。"""

    RULES = [
        (["根因", "5M1E", "为什么不良"], IntentCategory.ROOT_CAUSE),
        (["怎么处置", "SOP", "怎么修", "流程"], IntentCategory.DOC_LOOKUP),
        (["草拟", "生成返工单", "8D"], IntentCategory.DRAFT_REQUEST),
        (["过了哪几站", "用了哪批", "位置"], IntentCategory.TRACE_FACT),
    ]

    def __init__(self, model: Model) -> None:
        self._model = model

    async def classify(self, question: str) -> IntentCategory:
        # 1. 规则优先
        for keywords, intent in self.RULES:
            if any(k in question for k in keywords):
                return intent
        # 2. LLM 兜底（以 Content Block（Pydantic BaseModel）强制枚举返回）
        try:
            msg = await self._model.classify_intent(question, schema=IntentCategory)
            return msg.content_block(IntentCategory)
        except Exception:
            return IntentCategory.UNKNOWN
```

> 示例中的调用形态是**能力描述**（结构化输出 = Content Block + `Msg`），AgentScope 2.0 的方法名以官方文档为准。

### 9.2 路由编排工厂（RouteAgentFactory）

```python
class RouteAgentFactory:
    """按意图装配 E 的轻量编排：意图分诊 -> Toolkit 工具 / 显式服务调用委托 -> 收口。"""

    def __init__(
        self, tool_registry: ToolRegistry, delegator: AgentDelegator, model: Model
    ) -> None:
        self._registry = tool_registry
        self._delegator = delegator
        self._model = model

    def build(self, intent: IntentCategory, tenant: TenantContext,
              budget: ToolCallBudget) -> Agent:
        if intent in (IntentCategory.ROOT_CAUSE, IntentCategory.DRAFT_REQUEST):
            # 委托诊断与草稿：不配检索工具，只注册一个封装显式服务调用的委托 Tool
            return self._delegator.delegation_agent(intent, self._model, tenant)
        if intent == IntentCategory.UNKNOWN:
            return None                      # 由 §10.3 兜底转人工，不进 Agent
        return Agent(
            name="gateway-agent",
            model=self._model,
            toolkit=self._registry.toolkit_for(intent, tenant),  # 只读白名单内的 Tool
            budget=budget,                   # 应用层独立计算工具次数 / 时长 / 证据量
            hooks=[self._permission_gate],   # 叠加层：PermissionEngine 规则闸 + RequireUserConfirmEvent 确认（主闸在工具网关的一次性令牌）
        )
```

- **委托 Tool 的装配**：`delegation_agent` 返回的是一个**只注册了一个委托 Tool** 的单步统一 `Agent`（Tool 内部调 §5.2 的 `invoke_diagnosis` / `invoke_draft`），因此"深度场景 E 不自己推理"由工具面保证，而不是靠某个框架移交关键字。
- **组合原语的真实口径**：`agentscope/pipeline` 与 `agentscope/msghub` 自 2.0.0 起**已删除**，`sequential_pipeline` / `fanout_pipeline` / `MsgHub` 都不可依赖。顺序固定的组合=应用层 `await` 串联；并行=`asyncio.gather` 并发多个**各自独立**的 `Agent.reply()`，结论在应用层显式合并；编排代码在本仓库叫 `flows/`。需要共享上下文时也**没有**共享消息上下文的原语--各分支只读同一份 `evidence_set_id`（关系型证据子集），彼此不看对方中间结论。`GoalPipeline` / `TeamPipeline` 虽存在但标记 experimental（`TeamPipeline` 官方明写成员间不互相说话），只可观察，不进生产。跨能力委托是显式服务调用（`invoke` 语义自实现），不是框架移交。`Agent` / `Toolkit` 的构造参数以官方源码树为准（不存在/已删除项的核对结论见 [ADR-01](../架构设计v2/00-架构决策记录ADR.md)）。

### 9.3 工具执行器（ToolExecutor）

```python
class ToolExecutor:
    """调检索工具，权限校验 + trace。工具以 AgentScope `Tool` 注册进 `Toolkit`（继承 诊断能力口径）。"""

    def __init__(self, registry: ToolRegistry, trace_repo: RouteTraceRepo) -> None:
        self._registry = registry; self._trace_repo = trace_repo

    async def __call__(self, state: GatewayState) -> GatewayState:
        tenant = state["tenant"]
        intent = state["intent"]
        tool_name = self._select_tool(intent)   # 意图 -> 工具映射
        tool = self._registry._descriptors.get(tool_name)
        if tool is None or not tenant.can_access(tool.required_tenant_scopes):
            await self._trace_repo.save_denied(tool_name, tenant)
            state["answer"] = self._fallback("权限不足或工具不可用")
            return state
        try:
            view = await tool.handler(question=state["question"], tenant=tenant)
            await self._trace_repo.save_ok(tool_name, view)
            state["tool_result"] = view
            state["tool_chain"] = [tool_name]
        except Exception as e:
            state["answer"] = self._fallback(f"工具调用失败: {e}")
        return state

    def _select_tool(self, intent: IntentCategory) -> str:
        return {
            IntentCategory.TRACE_FACT: "query_traceability",
            IntentCategory.DOC_LOOKUP: "search_docs",
        }.get(intent, "")
```

### 9.4 启动断言（只读校验 + 下游可达性）

```python
class ReadOnlyToolGate(Exception):
    """启动时发现非只读工具，拒绝启动。"""
class DependencyUnreachableGate(Exception):
    """启动时发现检索能力与诊断 / 草稿能力依赖不可达，拒绝启动（E 是收口，依赖必须就绪）。"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.tool_registry
    # 1. 只读断言（继承 诊断能力）
    registry.validate_on_startup()
    # 2. 依赖可达性断言（E 收口的前提：检索能力与诊断 / 草稿能力必须可达）
    await assert_dependencies_reachable(app.state.acl_clients)
    yield

async def assert_dependencies_reachable(clients: dict) -> None:
    """E 是收口入口，下游检索能力与诊断 / 草稿能力必须可达，否则退化为套壳。"""
    for name, client in clients.items():
        if not await client.ping():
            raise DependencyUnreachableGate(f"依赖不可达: {name}（E 收口要求检索能力与诊断 / 草稿 / 编排能力成型）")
```

- `assert_dependencies_reachable` 体现"E 是收口，依赖必须就绪"的判断--E 不该在检索能力与诊断 / 草稿能力没就绪时启动，否则就是套壳问答。

### 9.5 FastAPI 入口

```python
router = APIRouter(prefix="/agent", tags=["agentic-rag"])

@router.post("/chat", response_model=AgentAnswer)
async def chat(
    req: ChatRequest,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: GatewayService = Depends(get_gateway_service),
) -> AgentAnswer:
    return await svc.chat(req, tenant)

@router.get("/explain/{audit_id}", response_model=AnswerAudit)
async def explain(audit_id: str, tenant: TenantContext = Depends(tenant_from_token)) -> AnswerAudit:
    """回溯某次问答的路由与工具链。"""
    ...
```

- `/agent/chat` 统一问答入口；`/explain` 回溯路由与工具链（收口入口的可观测）。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `gateway_chat_total` | 问答次数（按 intent label） |
| `gateway_route_accuracy` | 路由准确率（用户反馈/评测集，健康度核心） |
| `gateway_tool_call_total` | 工具调用次数（按 route label） |
| `gateway_delegation_total` | 专精 Agent 委托次数（显式服务调用；诊断与草稿 label） |
| `gateway_chat_latency_seconds` | 整问答延迟（Histogram） |
| `gateway_cache_hit_total` | 查询缓存命中 |
| `gateway_low_confidence_total` | 低置信度转人工次数 |
| `gateway_unknown_intent_total` | 未识别意图转人工次数 |
| `gateway_dependency_unreachable_total` | 下游依赖不可达降级次数 |

### 10.2 trace 串联

- `trace_id` 从 E 生成，透传到诊断 / 草稿与检索工具（`traceparent` header）。
- `AnswerAudit` 记录问题/意图/路由/工具链/答案/`trace_id`，工程师可从答案回溯到每项能力的每步工具调用。

### 10.3 兜底

- **路由失败兜底**：`UNKNOWN` 意图 -> 转人工"该问题暂不支持"。
- **下游不可达兜底**：检索能力与诊断 / 草稿故障 -> 降级为"该能力暂时不可用，请联系工程师"或切换备选能力。
- **低置信度兜底**：`confidence < 0.6` -> `needs_human_review=True`，标注"建议人工核对"。
- **委托超时兜底**：诊断能力委托超时 -> 降级为"诊断未完成，已转人工"（继承诊断能力 §8.3）。
- **预算超限兜底**：轻量组合的工具调用次数/时长超限 -> 降级为"建议改用诊断能力深度诊断"或转人工。

---

## 11. 实现步骤

### 阶段一：骨架与意图路由（2 周）

1. 搭 `agent_gateway_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 实现 `IntentRouter`（规则优先 + LLM 兜底）（§9.1）。
3. 实现统一 `Agent` + `Toolkit` 的轻量路由编排（§9.2），单工具直答路径跑通。
4. 实现 `ReadOnlyToolGate` + `assert_dependencies_reachable` 启动断言（§9.4）。

### 阶段二：工具与委托（2-3 周）

5. 实现 `ToolExecutor` + `ToolRegistry`（检索工具封装）（§9.3）。
6. 实现 ACL 客户端（rag-service 检索）（§7.2）。
7. 实现 `AgentDelegator`（对诊断与草稿发显式服务调用委托；`invoke` 语义自研，非框架移交）（§5.2）。
8. 验证委托诊断能力的 trace 串联（`traceparent` 透传）。

### 阶段三：统一输出与可观测（1-2 周）

9. 实现 `AgentAnswer` 统一格式 + `AnswerAudit` 审计（§6.1）。
10. 实现 `/agent/explain` 回溯端点（§9.5）。
11. 接 OpenTelemetry + prometheus 指标（§10.1），路由准确率告警。

### 阶段四：加固、评测与试点（1 周）

12. 沉淀评测集（典型问题 + 预期路由/答案），回归路由准确率。评测栈**自建**：自研 `EvalRunner` + 断言/指标/judge 三层判定 + pass^k + CI 门禁，LLM-as-judge 用独立包 `py-openjudge`。⚠️ 2.0 已删除 `agentscope.evaluate` 且未回归，**不存在"AgentScope 智能体评测/内生评测栈"**，不能把评测挂到框架上。
13. 兜底链路全测（UNKNOWN/下游不可达/低置信度/委托超时/预算超限）。
14. 灰度统一入口试点，收集"路由是否准、答案是否对"反馈。
15. 确认 🔴 决策点（路由规则覆盖、轻量组合 vs 委托阈值、流程编排纳入时机）。

> **前提**：E 的实现必须在检索能力与诊断 / 草稿能力成型后启动（[RAG服务引入路线.md](RAG服务引入路线.md) §3、[AGENT服务引入路线.md](../AGENT服务/AGENT服务引入路线.md) §5）。E 是收口，不是起步。

---

## 12. 约束落地检查清单

- [ ] E 无自有检索/推理能力，全部调两项检索能力 + 委托诊断 / 草稿（§1.3 收口不造新能力）。
- [ ] 所有注册工具 `read_only=True`，`ReadOnlyToolGate` 启动断言生效（继承诊断能力）。
- [ ] `assert_dependencies_reachable` 启动断言：检索能力与诊断 / 草稿能力不可达时拒绝启动（E 收口前提）。
- [ ] E 不调过点引擎放行/拦截 API。
- [ ] 工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承文档检索、制造追溯与诊断能力的版本一致性）。
- [ ] 工具调用前按 `TenantContext` 权限过滤，拒绝记录可观测（继承诊断能力）。
- [ ] 深度诊断显式委托诊断能力（跨能力服务调用，非框架移交），E 不自己多步推理；应用层预算（工具次数 / 时长 / 证据量）限制轻量组合规模。
- [ ] 所有路径收敛到 `AgentAnswer` 统一格式，带 `route_taken` + `tool_chain` + `sources`。
- [ ] `trace_id` 从 E 发起，透传到诊断 / 草稿与检索工具，全链路可回溯。
- [ ] 路由失败/下游不可达/低置信度/委托超时 -> 转人工兜底，不硬答。
- [ ] 答案带 `disclaimer`：辅助信息，最终处置需工程师确认。
- [ ] E 在检索能力与诊断 / 草稿能力成型后启动，不提前做（避免套壳）。

---

## 13. 面试防守 Q&A

**Q：Agentic RAG 和你已经规划的检索能力与三项 Agent 能力是什么关系？是不是重复了？**
A：不重复，E 是收口。文档检索与制造追溯是两项检索能力，诊断 / 草稿 / 编排是三项专精 Agent 能力。用户不该在多个入口间选择该问谁--E 做统一入口 + 意图路由，把问题送到对的地方。E 不重建检索/推理能力，全部调检索工具 + 显式委托诊断 / 草稿专精 Agent（跨能力服务调用，框架无移交原语）。[AGENT服务引入路线.md](../AGENT服务/AGENT服务引入路线.md) §6 明确 Agentic 是 Agent 的收口形态。E 是最后一步，不是第一步--等检索能力与诊断 / 草稿 / 编排能力成型后收口，否则没工具可调，退化为套壳问答。

**Q：E 和诊断能力怎么分工？是不是又重复了？**
A：不重复，是广深分工。E 是"广而浅"的路由收口层（意图分类 -> 选工具 / 显式委托 -> 轻量组合），诊断能力是"深而专"的追溯多步诊断（≤10 步 5M1E 推理）。简单追溯事实（"过了哪几站"）E 调制造追溯工具直答；深度根因诊断（"为什么不良"）E 委托诊断能力。E 的轻量组合只允许 1-2 次工具调用（应用层独立计算次数、时长与证据预算），深度多步委托诊断能力。这 reconcile 了 RAG 引入路线 §3"Agentic 在预算内完成多步组合"和 AGENT 引入路线 §2.1"诊断能力自己决定下一步查什么"--E 的"多步"是相对单次检索的轻量组合，深度推理委托诊断能力。

**Q：E 怎么判断一个问题该走哪项能力？**
A：意图路由是 E 的命脉，规则优先 + LLM 兜底。高频问题关键词命中直定意图（"根因/5M1E"->诊断能力，"怎么处置/SOP"->B，"草拟返工单"->草稿能力），命中不了才走 LLM 结构化输出分类（`IntentCategory` 固化为 Enum）。路由错了全盘皆错，所以路由准确率是 E 的核心健康度指标。UNKNOWN 意图不硬答，转人工。

**Q：E 会不会拖慢过点？**
A：不会。E 不调过点引擎放行/拦截 API，过点 toolset 不暴露 `pass/judge` 类工具（继承诊断能力 §1.2）。过点 P99 ≤200ms 仍由规则引擎保证，E 是管理层/工程师侧的问答入口，与过点执行完全无关。

**Q：E 路由错了怎么办？比如把文档查询误路由到 诊断。**
A：三重兜底。一是路由准确率作为核心指标持续监控 + 评测集回归；二是诊断能力委托有超时（≤60s）和置信度阈值，诊断能力发现问题不属于诊断范畴会返回低置信度，E 据此转人工；三是 `UNKNOWN` 意图和路由失败都转人工不硬答。`AnswerAudit` 记录每次路由决策，工程师可回溯"这个答案为什么走了诊断能力"，反馈调优路由规则。MES 领域错答案零容忍，宁可让人判。

**Q：E 是收口，那它自己有什么独特价值？不就是个路由器吗？**
A：价值恰在收口，不在新能力。一是**统一体验**：用户一个入口问所有问题，不用知道"这个问题该问检索工具还是诊断能力"。二是**路由判断**：意图分类把问题送到对的能力，路由准确率是 E 的核心壁垒。三是**轻量组合**：少数跨能力问题（"SN-001 根因 + 处置 SOP"）E 用轻量 ReAct 组合两项检索工具，不必都委托诊断能力。四是**统一可观测**：所有问答经 E，trace 从 E 发起串联全链路，收口入口的路由/工具链透明可回溯。这些不是"新能力"，但把散落的能力收成一致体验本身就是架构价值。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地，且 E 是这几项能力里最后做的。重点是四条架构判断：① E = 统一入口收口层，不造新能力，全部调两项检索能力 + 显式委托诊断 / 草稿；② 意图路由是核心，规则优先 + LLM 兜底；③ 广深分工--E 轻量组合（应用层工具次数/时长预算），深度诊断委托诊断能力，不重复；④ 继承全部只读红线（只读闸/版本闸/权限闸/不进过点主事务闸）。E 必须在检索能力与诊断 / 草稿 / 编排能力成型后启动，否则退化为套壳。诚实 + 体现架构判断力，比硬吹"已上线 Agentic RAG"得分高。

---

## 14. 一句话定位

"Agentic RAG 是 rag-service 三项内部能力的收口--一个统一入口按问题意图路由到检索工具 + 显式委托诊断 / 草稿专精 Agent。E 不造新能力，全部调文档检索与制造追溯 + 委托诊断能力做深度诊断/草稿；意图路由规则优先 + LLM 兜底，把问题送到对的能力；广深分工--E 只做轻量组合（应用层独立计算工具次数、时长与证据预算），跨上下文深度多步委托诊断能力，不重复；继承全部只读红线（`ReadOnlyToolGate`/版本/权限/不进过点主事务），所有路径收敛到带 `route_taken`+`tool_chain`+`sources` 的统一答案，trace 从 E 发起串联全链路。E 是最后一步，等文档检索与制造追溯与诊断 / 草稿 / 编排能力成型后收口，否则退化为套壳问答。"
