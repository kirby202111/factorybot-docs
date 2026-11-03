# Agentic RAG 实现方案（Python 技术栈：3 意图路由 MVP）

> 本文是 [RAG服务引入路线.md](RAG服务引入路线.md) §3「Agentic 的正确定位」所述 **Agentic 能力**的**实现层落地**，与 [Agentic RAG-详细设计.md](./Agentic RAG-详细设计.md) 的关系：
> - **详细设计**是全意图收口的**设计层**（广）--意图路由、工具与跨能力显式委托、统一输出的全景；
> - **本文**是 3 类意图（追溯事实 + 根因诊断 + 文档查询）的**实现层**（深）--把详细设计的骨架补全到可落地的 MVP，新增**依赖清单、ACL 只读 REST 契约、AgentScope 路由编排代码、Docker 部署、测试策略**等实现层内容，并对个别委托契约按诊断与草稿落地口径细化（如诊断能力委托超时与 trace 透传，§4.3 🔴）。
> 其余意图（草稿生成草稿能力委托）按 §11 相同范式扩展，MVP 不展开。
>
> **技术栈**：Python（FastAPI + AgentScope 2.0 + Pydantic）。Agent 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 REST 解耦，互不侵入。
> **底座口径**：与 [基于AgentScope2.0的架构设计.md](../基于AgentScope2.0的架构设计.md) §3.2 对齐（`agentscope>=2.0.0`、Python 3.12+）--路由收口用 AgentScope 统一 `Agent`（内置 ReAct）+ `Toolkit`，跨请求会话用 `AgentState` + `SessionService`（Redis-backed，2.0.5 起由 `SessionManager` 改名而来），意图分诊由**单 Agent 的 ReAct + `Toolkit` 工具选择**承担，跨能力委托是**显式服务调用**（只传 `evidence_set_id`/`session_id`/`traceparent`，不传消息历史）--框架并未提供 `Handoffs` / `Routing` 这类原语。框架只决定"怎么实现"，不改变本文的只读、版本与证据约束。
> **口径纪律**：Agentic RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Agentic RAG"。

---

## 1. 设计目标与边界

### 1.1 定位（E = rag-service 的 Agentic 收口入口）

[AGENT服务引入路线.md](../AGENT服务/AGENT服务引入路线.md) §6 明确 **rag-service 的 Agentic 能力就是 Agent 的收口形态**。它是统一入口收口层，不造新能力，全部调文档检索与制造追溯的工具 + 用显式服务调用委托诊断与草稿 Agent。这是最后一步，等文档检索与制造追溯 + 诊断能力-编排能力成型后收口（[RAG服务引入路线.md](RAG服务引入路线.md) §3「Agentic 的正确定位」）。

### 1.2 目标（4 意图 MVP）

把文档检索、制造追溯与诊断收口成统一入口，跑通"NL -> 意图路由 -> 工具/委托 -> 统一答案"闭环。**MVP 范围**：聚焦 3 类高频意图：

| 意图 | 路由到 | 形态 | MVP 典型问题 |
|------|--------|------|-------------|
| **`TRACE_FACT`**（追溯事实） | 制造追溯工具 `query_traceability` | 工具直答 | "SN-001 过了哪几站""这批用了哪批锡膏" |
| **`ROOT_CAUSE`**（根因诊断） | 诊断能力（显式服务调用委托） | 委托 | "SN-001 焊接不良根因" |
| **`DOC_LOOKUP`**（文档查询） | 文档检索工具 `search_docs` | 工具直答 | "SPI 报警怎么处置" |

> 其余意图（`DRAFT_REQUEST` 委托草稿能力、轻量组合）按 §11 相同范式扩展，MVP 不展开。

### 1.3 硬边界（一开口就要讲）

| 边界 | 说明 | 落地（MVP 具体动作） |
|------|------|----------------------|
| **收口不造新能力** | E 只做路由 + 委托，不重建检索/推理 | E 无向量库/图谱，全部调两项检索能力 + 委托诊断能力 |
| **只读（继承诊断能力）** | E 全程只读，无写工具 | `ReadOnlyToolGate` 启动断言；写工具不注册（§9.4） |
| **不进过点主事务** | E 不调过点放行/拦截 API | 过点 toolset 不暴露 `pass/judge`（继承诊断能力 §1.2） |
| **版本一致性** | 查工艺带版本锚点（`version`+`version_kind`） | 检索工具强制版本锚点（`version`+`version_kind`）入参，ACL 校验（继承文档检索、制造追溯与诊断能力） |
| **权限隔离** | 工具调用前按 `tenant_scope` 过滤 | 路由 + 工具调用都带 `TenantContext`（继承诊断能力 §4.3） |
| **委托而非重复** | 深度诊断委托诊断能力，E 不多步推理 | E 的步数与工具调用预算只允许轻量组合（最多 1-2 次工具调用），深度场景用显式服务调用委托诊断能力 |
| **可观测兜底** | 答案带工具链 + 来源 + 置信度 | `AnswerAudit` 落库 + `/explain` 回溯；低置信度转人工 |
| **依赖可达性** | E 收口前提：两项检索能力与诊断能力必须可达 | `assert_dependencies_reachable` 启动断言（§9.4） |

### 1.4 与详细设计、检索能力、Agent 三项能力的关系

- **与详细设计**：详细设计给全意图收口全景；本文把 4 类高频意图的路由编排、ACL、代码补全到可落地。
- **与检索能力**：E 封装制造追溯（`query_traceability`）与文档检索（`search_docs`）为工具调用，不重建检索能力。
- **与诊断能力**：深度诊断用**显式服务调用**委托诊断能力（`httpx` 直调 `/agent/diagnose`，只传 `evidence_set_id`/`session_id`/`traceparent`，不传消息历史），E 不自己多步推理。框架无 `Handoffs` 移交机制，委托就是普通的服务间调用。
- **与 草稿与编排**：MVP 不含 草稿委托 / 流程编排（§11 扩展）。

### 1.5 与 Java 技术栈的关系

- E 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只通过 REST 调 rag-service 的检索接口与 agent-service 诊断能力的只读接口。
- 跨语言物理边界天然强制只读（与诊断能力同构，[FactoryAgent-诊断能力.md](../AGENT服务/FactoryAgent/FactoryAgent-诊断能力.md) §1.3）。
- E 是纯编排层，复用检索能力与诊断能力的既有契约，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 版本 | 选型理由 |
|------|------|------|---------|
| 语言 | Python | 3.12+ | 与三项能力同栈，复用 AgentScope + Model 抽象 |
| Web 框架 | **FastAPI** | 0.110+ | 异步、原生 OpenAPI，统一问答入口 |
| Agent 底座 | **AgentScope 2.0** | `agentscope>=2.0.0` | 统一 `Agent` 类（内置 ReAct，`reply` / `reply_stream`）+ `Toolkit` 承载路由 -> 工具/委托 -> 收口：分诊由单 Agent 的 ReAct + `Toolkit` 工具选择承担，跨能力委托为显式服务调用（框架无 `Routing`/`Handoffs` 原语）；与诊断能力同底座 |
| LLM 抽象 | AgentScope `Model` | 2.0+ | provider 可插拔（DashScope / Ollama / OpenAI 兼容），与诊断能力一致 |
| 数据校验 | **Pydantic** | v2 | 意图/工具入参/统一答案 schema 即类型 |
| HTTP 客户端 | **httpx** | 0.27+（异步） | 调 rag-service 检索 + agent-service 诊断只读 REST |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 2.0+ | 路由 trace、答案审计 |
| 会话状态 | AgentScope `AgentState` + `SessionService`（Redis-backed；2.0.5 起由 `SessionManager` 改名） | 2.0+ | 跨请求会话恢复；与 `redis-py` 共用 Redis 实例 |
| 缓存 | **redis-py (async)** | 5.0+ | 相同问题短缓存 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | - | trace 从 E 发起串联 |
| 配置 | pydantic-settings | 2.0+ | 环境变量统一管理 |
| 部署 | 独立微服务 `agent-gateway-service`（uvicorn + gunicorn worker） | - | K8s 部署；MVP 可 docker-compose 本地起 |

### 2.2 为什么 E 用 AgentScope 轻量编排

- E 的核心是"路由 + 轻量组合"：多数问题单工具即答。用 AgentScope 统一 `Agent`（内置 ReAct）+ `Toolkit` 承载"路由 -> 工具/委托 -> 收口"；分诊由单 Agent 的 ReAct + `Toolkit` 工具选择承担（框架无 `Routing`/`Handoffs` 原语）。2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 模块已删除：固定顺序在应用层用 `await` 串联，并行用 `asyncio.gather` 并发多个**各自独立**的 `Agent.reply()`、结论在应用层显式合并；这类编排代码在本仓库归 `flows/` 目录。
- 与诊断能力同底座：可复用 `AgentState` + `SessionService` 的会话恢复与事件系统 / `TracingMiddleware` 的 trace 机制，委托诊断能力时 `traceparent` 透传（§4.3）。
- AgentScope 没有"递归步数上限"这类单一参数：E 的"不做深度推理"靠应用层独立计算**工具调用次数、时长与证据预算**（MVP 上限：最多 1-2 次工具调用、整问答 70s）来保证，预算超限即降级转人工；深度场景用显式服务调用委托诊断能力。

### 2.3 为什么 E 不重建检索能力

- 两项检索能力已建好检索，诊断能力已建好多步诊断。E 重建等于重复造轮子 + 多份事实源。
- E 的价值在"路由判断"，把问题送到对的能力。委托而非重复：深度诊断委托诊断能力，E 不自己推理。
- E 也不依赖 AgentScope 的 `rag/` 模块接检索：该模块虽真实，但适配器只有 Qdrant / MilvusLite / Elasticsearch / MongoDB，**无 ChromaDB 适配器**，且 2.0.0 曾被整体删除、2.0.5 才回归。E 只按 rag-service 的只读 HTTP 契约接入检索，把"框架 RAG 抽象可能被删"的风险挡在适配层外，需 pin 版本 + 降级预案。

### 2.4 部署形态

- E 是统一入口，部署在办公网/车间网均可访问的位置。LLM 视安全策略二选一。
- E 依赖 rag-service 与 agent-service 可达性：下游故障时降级（§10.3），不硬答。
- MVP 用 `docker-compose` 本地起 MySQL + Redis + agent-gateway-service（§9.9）。

### 2.5 依赖清单（pyproject.toml 片段）

```toml
[project]
name = "agent-gateway-service"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "gunicorn>=21.2",
  "pydantic>=2.6",
  "pydantic-settings>=2.2",
  "agentscope>=2.0.0",
  "httpx>=0.27",
  "sqlalchemy[asyncio]>=2.0",
  "asyncmy>=0.2.9",
  "redis>=5.0",
  "opentelemetry-api>=1.24",
  "opentelemetry-instrumentation-fastapi>=0.45b",
  "prometheus-client>=0.20",
]
```

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
│                      │ AgentScope 路由编排  │                       │
│                      │ router->tool/delegate│                       │
│                      └──────────┬──────────┘                       │
│                                 │                                  │
│         ┌───────────────────────┼───────────────────────┐          │
│         ▼                       ▼                       ▼          │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐    │
│  │ IntentRouter │    │ ToolExecutor     │    │  AgentDelegator   │ │
│  │ NL -> 3 意图  │    │ 检索工具调用   │    │ 显式调用委托诊断能力 │ │
│  └──────────────┘    └────────┬─────────┘    └──────┬───────┘    │
│                               │                     │            │
│                       ┌───────▼─────────┐           │            │
│                       │ ToolRegistry    │           │            │
│                       │ (只读白名单)     │           │            │
│                       └─────────────────┘           │            │
│  ┌──────────────────┐  ┌──────────────────┐         │            │
│  │ AnswerAudit(MySQL)│ │ QueryCache(Redis)│         │            │
│  └──────────────────┘  └──────────────────┘         │            │
└─────────────────────────────────────────────────────┼────────────┘
                      │ httpx 只读 REST               │
        ┌─────────────┼───────────────────────────────┼────────┐
        ▼             ▼                 ▼
  ┌──────────┐  ┌──────────┐    ┌──────────┐
  │ 制造追溯  │  │ 文档检索  │    │ 诊断   │
  │ /rag/trace│  │ /rag/docs │    │ /agent/   │
  └──────────┘  └──────────┘    │ diagnose  │
                                └──────────┘
```

### 3.1 关键设计决策

- **路由即核心**：`IntentRouter` 规则优先 + LLM 兜底，路由准确率是健康度核心。
- **工具与委托分离**：`ToolExecutor`（检索工具单步直答）与 `AgentDelegator`（显式服务调用委托诊断能力）解耦（SRP）。
- **统一输出**：所有路径收敛到 `AgentAnswer`。
- **trace 从 E 发起**：`trace_id` 从 E 生成，透传到诊断能力与检索工具。

---

## 4. 意图路由与 Agent 委托（显式服务调用）

### 4.1 意图分类（MVP 3 类）

| 意图 | 路由到 | 形态 |
|------|--------|------|
| `TRACE_FACT` | 制造追溯工具 `query_traceability` | 工具直答 |
| `ROOT_CAUSE` | 诊断能力（显式服务调用委托） | 委托 |
| `DOC_LOOKUP` | 文档检索工具 `search_docs` | 工具直答 |

> 全意图分类见 [详细设计](./Agentic RAG-详细设计.md) §4.1。

### 4.2 路由规则（规则优先 + LLM 兜底）

```text
IntentRouter.classify(question)
   │
   ├─ 1. 规则优先
   │     ├─ 含"根因/5M1E/为什么不良" -> ROOT_CAUSE -> 委托诊断能力
   │     ├─ 含"怎么处置/SOP/怎么修" -> DOC_LOOKUP -> 文档检索工具
   │     └─ 含"过了哪几站/用了哪批/位置" -> TRACE_FACT -> 制造追溯工具
   ├─ 2. LLM 兜底（以 Content Block（Pydantic BaseModel）强制枚举返回）
   └─ 3. 仍不确定 -> UNKNOWN -> 转人工
```

- 规则优先降低对模型依赖；LLM 兜底强约束 Enum；UNKNOWN 转人工不硬答。

### 4.3 诊断能力委托的 trace 透传与超时

> 🔴 **契约待对齐：诊断能力委托的 trace 透传**。MVP 假设诊断能力的 `/agent/diagnose` 接受 `traceparent` header 并透传到其工具调用（诊断能力 §8.2 已声明 OpenTelemetry 透传 `traceparent` 到下游 Java 服务）。E 委托诊断能力时注入 `traceparent`，使 E -> 诊断能力 -> 检索工具/各上下文 REST 的 trace 全链路串联。待与诊断能力实现确认 `traceparent` header 的接收与透传。MVP 兜底：若诊断能力未透传，E 侧仍记录委托的 span，但诊断能力内部 trace 断裂（降级可观测）。
>
> **超时**：诊断能力整会话 ≤60s（[诊断能力](../AGENT服务/FactoryAgent/FactoryAgent-诊断能力.md) §5.1），E 委托超时设 60s（略留余量到 70s 整问答超时）。诊断能力超时返回 `DiagnosisReport.partial`，E 据此转人工。

---

## 5. 工具注册与统一答案

### 5.1 工具注册（MVP 两个检索工具，只读白名单）

| 工具名 | 所属能力 | 入参 | 版本校验 |
|--------|-----------|------|---------|
| `query_traceability` | 制造追溯 | seed, as_of, version, version_kind | 历史回放带版本锚点 |
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

- 两工具均注册为 AgentScope `Tool` 并挂进同一个 `Toolkit`；外部检索服务也可按需用 MCP 接入。
- `read_only=False` 启动时拒绝注册（继承诊断能力 `ReadOnlyToolGate`）。
- 工具调用前按 `TenantContext` 权限过滤（继承诊断能力 §4.3）。
- 检索工具强制版本锚点（`version`+`version_kind`）入参（继承文档检索、制造追溯与诊断能力的版本一致性）。

### 5.2 统一答案（AgentAnswer）

```python
class AnswerSource(BaseModel):
    source_type: str          # "trace_record" / "sop_doc" / "diagnosis_hypothesis"
    ref: str                  # "evidence_id=..." / "SOP:WELD-014@v3" / "audit_id=..."
    route: str                # "traceability" / "doc_search" / "diagnosis"

class AgentAnswer(BaseModel):
    question: str
    intent: str               # 命中的 IntentCategory
    route_taken: str          # "traceability" / "diagnosis" / "doc_search"
    summary: str
    detail: dict              # 能力相关结构化详情
    evidence_set_id: str      # 本次证据集标识，供回查证据集（关系型证据子集，非图引用）
    sources: list[AnswerSource]
    confidence: float
    tool_chain: list[str]     # ["query_traceability"] 或 ["诊断能力:diagnose"]
    trace_id: str
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"
```

- `route_taken` + `tool_chain` 透明，收口入口路由可观测。
- `sources` 强制引用（SOP 带版本、证据 ID、诊断能力假设证据）；证据回查按 `evidence_set_id` 取，不使用图节点引用。
- 统一答案以 AgentScope Content Block（Pydantic BaseModel）挂在 `Msg` 上返回，前端与评测侧可按结构解析。
- `disclaimer` 不可省。

### 5.3 红线继承

E 继承诊断能力全部红线：① 只读闸（`ReadOnlyToolGate`）；② 版本闸（检索工具强制版本锚点 `version`+`version_kind`）；③ 权限闸（`TenantContext` 前置过滤）；④ 不进过点主事务闸（过点 toolset 不暴露 `pass/judge`）。

---

## 6. 实现方案

### 6.1 网关编排服务（GatewayService）

> 下面代码给的是**编排形状**（应用层分诊 + 统一 `Agent` 驱动 + 应用层预算 + `SessionService` 会话恢复）。框架名只写 ADR-01 / 架构设计附录 A **已核实**的原语与最小构造骨架（`Agent(name=..., model=..., toolkit=..., offloader=...)`、`reply` / `reply_stream`）；未核实的参数与方法名不进代码，本文不生造 API。

```python
class BudgetExceededError(Exception):
    """应用层预算守卫抛出（非 AgentScope 内置异常）：工具次数 / 时长 / 证据量超限。"""

class GatewayService:
    def __init__(
        self,
        route_agent_factory: RouteAgentFactory,      # 按意图装配统一 Agent + Toolkit
        budget_policy: BudgetPolicy,                 # 应用层工具次数 / 时长 / 证据预算（非框架参数）
        session_service: SessionService,             # AgentState 跨请求会话恢复（Redis-backed，2.0.5 起替代 SessionManager）
        intent_router: IntentRouter,
        tool_executor: ToolExecutor,
        delegator: AgentDelegator,
        cache: QueryCache,
        audit_repo: AnswerAuditRepo,
        metrics: MetricsCollector,
    ) -> None: ...

    async def chat(self, request: ChatRequest, tenant: TenantContext) -> AgentAnswer:
        # 1. 缓存
        cached = await self._cache.get(request, tenant)
        if cached:
            self._metrics.cache_hit.inc()
            return cached
        # 2. 意图分诊（规则优先 + LLM 兜底；框架无 Routing 原语，分诊是本层自建逻辑）
        intent = await self._intent_router.classify(request.question)
        self._metrics.intent.inc(intent.value)
        # 3. 装配并驱动统一 Agent（内置 ReAct + Toolkit 工具选择）
        #    ROOT_CAUSE 时 Toolkit 内只挂 delegate_diagnosis 一个"显式服务调用"工具（见 §7.3、§9.2）
        #    budget 由应用层独立计算：最多 1-2 次工具调用、证据条数上限、剩余时长
        budget = self._budget_policy.for_intent(intent)
        agent_state = await self._session_service.load(request.session_id)   # 按 session_id 恢复 AgentState（Redis-backed）
        agent = self._route_agent_factory.build(intent, tenant, budget, agent_state)
        try:
            msg = await asyncio.wait_for(
                agent.reply(Msg(name="user", content=request.question, role="user")),
                timeout=70.0,
            )
        except (BudgetExceededError, asyncio.TimeoutError) as e:
            self._metrics.fallback.inc("timeout_or_budget")
            return self._fallback(request, intent, f"问答未完成，已转人工: {e}")
        # 4. 收敛统一答案（AgentAnswer 作为 Content Block 挂在 Msg 上）
        answer = self._build_answer(request, intent, msg)
        # 5. 会话状态落盘 + 审计 + 缓存
        await self._session_service.save(request.session_id, agent.state)
        await self._audit_repo.record(request, intent, answer)
        await self._cache.set(request, tenant, answer)
        return answer
```

- 编排与路由/执行/委托分离（SRP）；超时或预算超限降级转人工。
- E 的"轻量组合"不靠框架的递归上限参数，而靠 `budget` 里独立计算的工具调用次数与时长；超限即抛 `BudgetExceededError`。

### 6.2 ACL 防腐层（检索与诊断能力只读 REST 契约）

| 协作方 | 只读 REST | 用途 | 超时 |
|--------|----------|------|------|
| 制造追溯 | `POST /rag/trace/expand` | 关系型证据子集展开 | 5s |
| 文档检索 | `GET /rag/docs/search` | SOP 片段 | 2s |
| 诊断能力 | `POST /agent/diagnose` | 深度诊断委托 | 60s（🔴 traceparent 透传 §4.3） |

```python
class TraceRagAclClient:
    """制造追溯只读 ACL。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def query_traceability(
        self, seed: dict, as_of: datetime, version: str, version_kind: str, tenant: TenantContext
    ) -> TraceEvidenceSetView:
        resp = await self._http.post(
            "/rag/trace/expand",
            json={"seed": seed, "as_of": as_of.isoformat(), "version": version, "version_kind": version_kind},
            headers=tenant.headers(), timeout=5.0,
        )
        resp.raise_for_status()
        return TraceEvidenceSetMapper.to_view(resp.json())

    async def ping(self) -> bool:
        try:
            r = await self._http.get("/health", timeout=1.0)
            return r.status_code == 200
        except Exception:
            return False


class DelegationClient:
    """诊断 Agent 委托（显式服务调用，非框架移交原语）ACL。🔴 traceparent 透传（§4.3）。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def delegate(
        self, question: str, tenant: TenantContext, traceparent: str,
        evidence_set_id: str = "", session_id: str = ""
    ) -> ReportView:
        resp = await self._http.post(
            "/agent/diagnose",
            # 跨能力委托 = 显式服务调用：只传标识（evidence_set_id/session_id）与 question，不传消息历史
            json={"question": question, "evidence_set_id": evidence_set_id, "session_id": session_id},
            headers={**tenant.headers(), "traceparent": traceparent},  # 透传 trace
            timeout=60.0,
        )
        resp.raise_for_status()
        return ReportMapper.to_view(resp.json())

    async def ping(self) -> bool:
        try:
            r = await self._http.get("/health", timeout=1.0)
            return r.status_code == 200
        except Exception:
            return False
```

- 外部 DTO 不进 E 核心，只暴露 `TraceEvidenceSetView`/`ReportView`（ACL 约束）。
- 所有 ACL 客户端带 `ping()` 供启动断言（§9.4）。

### 6.3 版本一致性保证

继承文档检索、制造追溯与诊断能力：检索工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。E 不另搞版本管理。

---

## 7. 实现方案（路由编排与执行）

### 7.1 路由编排（应用层分诊 + 显式服务调用）

```text
[应用层分诊] 意图分类（已在 GatewayService 调 IntentRouter，intent 随消息透传）
      │
      ├─ TRACE_FACT/DOC_LOOKUP ──▶ [Toolkit 工具] 调检索工具 ──▶ [收口 Agent]
      ├─ ROOT_CAUSE ──▶ [显式服务调用] 诊断 Agent ──▶ [收口 Agent]
      └─ UNKNOWN ──▶ [收口 Agent] 转人工兜底
```

- 2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 模块已删除：顺序组合在应用层用 `await` 串联，并行用 `asyncio.gather` 并发多个**各自独立**的 `Agent.reply()`，结论在应用层显式合并（编排代码在本仓库归 `flows/` 目录）；2.0 也不提供共享消息上下文原语，各分支只读同一份 `evidence_set_id`（关系型证据子集），彼此不看对方中间结论。本 MVP 只需"分诊 -> 单工具/显式调用 -> 收口"三段，用 `await` 串起来即可。
- 步数与工具调用预算由应用层独立计算（最多 1-2 次工具调用 + 时长上限），E 的轻量组合超限即降级；深度场景显式调用诊断 Agent 服务。
- 详见 §9.2 代码。

### 7.2 工具执行器（ToolExecutor）

```python
class ToolExecutor:
    """调检索工具，权限校验 + trace。工具以 AgentScope `Tool` 注册进 `Toolkit`（继承 诊断能力口径）。"""

    def __init__(self, registry: ToolRegistry, trace_repo: RouteTraceRepo, metrics: MetricsCollector) -> None:
        self._registry = registry; self._trace_repo = trace_repo; self._metrics = metrics

    async def __call__(self, state: GatewayState) -> GatewayState:
        tenant = state["tenant"]
        tool_name = self._select_tool(state["intent"])
        tool = self._registry._descriptors.get(tool_name)
        if tool is None or not tenant.can_access(tool.required_tenant_scopes):
            await self._trace_repo.save_denied(tool_name, tenant)
            self._metrics.tool_denied.inc(tool_name)
            state["answer"] = self._fallback("权限不足或工具不可用")
            return state
        t0 = time.perf_counter()
        try:
            view = await tool.handler(question=state["question"], tenant=tenant)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await self._trace_repo.save_ok(tool_name, view, latency_ms)
            state["tool_result"] = view
            state["tool_chain"] = [tool_name]
            self._metrics.tool_ok.inc(tool_name)
        except Exception as e:
            await self._trace_repo.save_error(tool_name, e)
            state["answer"] = self._fallback(f"工具调用失败: {e}")
            self._metrics.tool_error.inc(tool_name)
        return state

    def _select_tool(self, intent: IntentCategory) -> str:
        return {
            IntentCategory.TRACE_FACT: "query_traceability",
            IntentCategory.DOC_LOOKUP: "search_docs",
        }.get(intent, "")
```

### 7.3 Agent 委托器（AgentDelegator，显式服务调用）

```python
class AgentDelegator:
    """把根因诊断委托给诊断 Agent 服务：显式服务调用（httpx -> /agent/diagnose），
    只传 evidence_set_id / session_id / traceparent，不传消息历史；框架不提供 Handoffs 移交原语。
    在本服务里注册为只读 Tool `delegate_diagnosis`，由统一 Agent 的 ReAct + Toolkit 选中；E 不自己多步推理。"""

    def __init__(self, diagnosis_client: DelegationClient, trace_repo: RouteTraceRepo, metrics: MetricsCollector) -> None:
        self._diagnosis = diagnosis_client; self._trace_repo = trace_repo; self._metrics = metrics

    async def __call__(self, state: GatewayState) -> GatewayState:
        tenant = state["tenant"]
        traceparent = state.get("traceparent", "")
        t0 = time.perf_counter()
        try:
            report = await self._diagnosis.delegate(state["question"], tenant, traceparent)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await self._trace_repo.save_ok("诊断能力:diagnose", report, latency_ms)
            state["tool_result"] = report
            state["tool_chain"] = ["诊断能力:diagnose"]
            self._metrics.delegation_ok.inc("诊断能力")
        except asyncio.TimeoutError:
            state["answer"] = self._fallback("诊断超时，已转人工")
            self._metrics.delegation_timeout.inc("诊断能力")
        except Exception as e:
            state["answer"] = self._fallback(f"诊断能力委托失败: {e}")
            self._metrics.delegation_error.inc("诊断能力")
        return state
```

- 委托超时/失败降级转人工（继承诊断能力 §8.3）；`state` 是本服务自定义的编排数据结构，不是框架的图状态对象。

---

## 8. 推荐包结构（Python src layout）

```text
agent_gateway_service/
  app/
    api/
      chat_router.py           # /agent/chat, /agent/explain
      schemas.py
    application/
      gateway_service.py       # GatewayService（按 flows/ 的串联形状驱动，不含框架 pipeline 原语）
      intent_router.py         # IntentRouter（规则优先 + LLM 兜底）
    flows/                     # 编排形状：顺序 await 串联 + asyncio.gather（2.0.0 起 pipeline/msghub 模块已删除）
      route_flow.py            # 分诊 -> 检索工具 / 显式服务调用 -> 收口（§6.1 的三段）
    domain/
      intent.py                # IntentCategory 枚举
      answer.py                # AgentAnswer / AnswerSource
      tool.py                  # ToolDescriptor / ToolRegistry / ReadOnlyToolGate
      tenant.py                # TenantContext
    infrastructure/
      ai/
        route_agent_factory.py # 统一 Agent + Toolkit 装配（应用层分诊 + 工具选择）工厂
        model_factory.py       # AgentScope Model 接入（provider 可插拔）
        budget_policy.py       # 应用层工具次数/时长/证据预算
      session/
        session_service.py     # AgentState + SessionService（Redis-backed；2.0.5 起由 SessionManager 改名）
      acl/
        trace_rag.py           # 制造追溯
        doc_rag.py             # 文档检索
        diagnosis_delegation.py       # 诊断能力委托（显式服务调用；🔴 traceparent 透传 §4.3）
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
  Dockerfile
  docker-compose.yml
```

- `domain/tool.ReadOnlyToolGate` 启动断言（继承诊断能力）。
- `flows/` 只放应用层编排形状：顺序 `await` 串联、并行 `asyncio.gather` 并发**各自独立**的 `Agent.reply()` 后在应用层显式合并结论（2.0 无 `pipeline`/`msghub` 原语可依赖，也没有共享消息上下文）。
- `infrastructure/acl/` 防腐层，调检索与诊断能力，外部 DTO 经 Mapper 转内部视图。

---

## 9. 关键代码骨架

### 9.1 意图路由器（IntentRouter）

```python
class IntentCategory(str, Enum):
    TRACE_FACT = "TRACE_FACT"
    ROOT_CAUSE = "ROOT_CAUSE"
    DOC_LOOKUP = "DOC_LOOKUP"
    UNKNOWN = "UNKNOWN"

class IntentRouter:
    """NL -> 意图分类。规则优先，LLM 兜底。"""

    RULES = [
        (["根因", "5M1E", "为什么不良"], IntentCategory.ROOT_CAUSE),
        (["怎么处置", "SOP", "怎么修", "流程"], IntentCategory.DOC_LOOKUP),
        (["过了哪几站", "用了哪批", "位置"], IntentCategory.TRACE_FACT),
    ]

    def __init__(self, classifier: Agent) -> None:
        # 兜底分诊用一个不挂工具的统一 Agent：
        # Agent(name="intent-classifier", model=DashScopeChatModel(credential=..., model=...), toolkit=Toolkit(tools=[]))
        self._classifier = classifier

    async def classify(self, question: str) -> IntentCategory:
        for keywords, intent in self.RULES:
            if any(k in question for k in keywords):
                return intent
        try:
            # 一次结构化输出：枚举以 Content Block（Pydantic BaseModel）挂在 Msg 上承载
            msg = await self._classifier.reply(
                Msg(name="user", content=f"只输出意图枚举值：{question}", role="user")
            )
            return parse_intent_block(msg)          # 本层解析函数：从 Msg 的 Content Block 取 IntentCategory
        except Exception:
            return IntentCategory.UNKNOWN
```

> 说明：AgentScope 用 Content Block（Pydantic BaseModel）+ `Msg`（带 `usage`、block `id`）承载结构化输出；兜底分诊就是统一 `Agent.reply()` 的一次结构化输出 + 本层解析函数，不调用未核实的 `Model` 专用方法名。

### 9.2 路由编排工厂（RouteAgentFactory）

```python
class RouteAgentFactory:
    """按意图装配 E 的轻量编排 Agent：分诊 -> 工具 / 显式服务调用委托 -> 收口。"""

    def __init__(self, tool_registry: ToolRegistry, delegator: AgentDelegator,
                 model: DashScopeChatModel, offloader: LocalWorkspace) -> None:
        self._registry = tool_registry
        self._delegator = delegator       # 注册为只读 Tool `delegate_diagnosis`（见 §7.3）
        self._model = model
        self._offloader = offloader       # 超大结果卸载 / 上下文压缩（workspace/ 后端）

    def build(self, intent: IntentCategory, tenant: TenantContext,
              budget: ToolCallBudget, agent_state: AgentState | None = None) -> Agent:
        # agent_state 由 §6.1 从 SessionService 按 session_id 载入后交入（本层搬运约定，非框架构造参数）
        toolkit = self._registry.toolkit_for(intent, tenant)   # 只读白名单内的 Tool
        if intent == IntentCategory.ROOT_CAUSE:
            # 深度诊断 E 不自己推理：Toolkit 里只挂 delegate_diagnosis 一个显式服务调用工具
            # （httpx -> /agent/diagnose，只传 evidence_set_id / session_id / traceparent，不传消息历史）
            toolkit = self._registry.toolkit_for_delegation(tenant)
        return Agent(
            name="gateway-agent",
            model=self._model,            # DashScopeChatModel(credential=DashScopeCredential(...), model=...)
            toolkit=toolkit,              # 内置 ReAct 负责"选工具 -> 观察 -> 收敛"
            offloader=self._offloader,
        )
        # budget 不是 Agent 构造参数：工具次数 / 时长 / 证据量由应用层独立计算并在 §6.1 用
        # asyncio.wait_for + BudgetExceededError 兜住；权限闸在工具注册与网关侧
        # （ReadOnlyToolGate + PermissionEngine/PermissionRule，见 §9.4、§5.3），HITL 用 event.RequireUserConfirmEvent。
```

- AgentScope 的统一 `Agent` 内置 ReAct，E 只需限制**可见工具集**与**预算**；上面只写架构设计附录 A 已核实的最小构造骨架（`Agent(name=..., model=..., toolkit=..., offloader=...)`），未核实的构造参数不进代码。
- 2.0 不提供 `Pipeline`（`sequential` / `fanout`）、`MsgHub`、`Handoffs` / `Routing`：顺序组合在应用层 `await` 串联，并行用 `asyncio.gather` 并发多个**各自独立**的 `Agent.reply()` 并在应用层显式合并结论（`GoalPipeline` / `TeamPipeline` 是 experimental，不进生产）；也不提供共享消息上下文原语，各分支只读同一份 `evidence_set_id`（关系型证据子集），彼此不看对方中间结论，跨能力委托是显式服务调用。编排代码在本仓库归 `flows/` 目录。
- `UNKNOWN` 意图不进 `Agent`，直接走 §10.3 的转人工兜底。

### 9.3 工具执行器与委托器

见 §7.2、§7.3。

### 9.4 启动断言（只读 + 依赖可达性）

```python
class ReadOnlyToolGate(Exception):
    """启动时发现非只读工具，拒绝启动。"""
class DependencyUnreachableGate(Exception):
    """启动时发现检索与诊断能力依赖不可达，拒绝启动（E 收口前提）。"""

async def assert_dependencies_reachable(clients: dict[str, object]) -> None:
    """E 是收口入口，下游检索与诊断能力必须可达，否则退化为套壳。"""
    unreachable = []
    for name, client in clients.items():
        if not await client.ping():
            unreachable.append(name)
    if unreachable:
        raise DependencyUnreachableGate(
            f"依赖不可达: {unreachable}（E 收口要求检索与诊断能力成型）"
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.tool_registry
    # 1. 只读断言（继承 诊断能力）
    registry.validate_on_startup()
    # 2. 依赖可达性断言（E 收口前提）
    await assert_dependencies_reachable(app.state.acl_clients)
    yield
```

- `assert_dependencies_reachable` 体现"E 是收口，依赖必须就绪"--E 不该在检索与诊断能力没就绪时启动。

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

### 9.6 配置与部署

```python
# app/config.py
class Settings(BaseSettings):
    audit_dsn: str = "mysql+asyncmy://root:root@mysql:3306/agent_gateway?charset=utf8mb4"
    redis_url: str = "redis://redis:6379/0"
    # rag-service 检索 + agent-service 诊断只读 REST
    trace_rag_base_url: str = "http://rag-service:8000"
    doc_rag_base_url: str = "http://doc-rag-service:8000"
    diagnosis_agent_base_url: str = "http://agent-service:8000"
    # LLM / Embedding provider（AgentScope Model 可插拔）
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    # 应用层预算（非 AgentScope 参数名）：独立计算工具调用次数与时长
    max_tool_calls: int = 2
    max_evidence_items: int = 20
    chat_timeout_seconds: int = 70
    confidence_threshold: float = 0.6
    class Config:
        env_prefix = "GATEWAY_"
```

```yaml
# docker-compose.yml（MVP 本地起）
version: "3.9"
services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: agent_gateway
    ports: ["3309:3306"]
    volumes:
      - ./sql/init.sql:/docker-entrypoint-initdb.d/init.sql
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  agent-gateway-service:
    build: .
    depends_on: [mysql, redis]
    environment:
      GATEWAY_AUDIT_DSN: mysql+asyncmy://root:root@mysql:3306/agent_gateway?charset=utf8mb4
      GATEWAY_REDIS_URL: redis://redis:6379/0
      GATEWAY_TRACE_RAG_BASE_URL: http://rag-service:8000
      GATEWAY_DOC_RAG_BASE_URL: http://doc-rag-service:8000
      GATEWAY_AGENT_BASE_URL: http://agent-service:8000
    ports: ["8004:8000"]
```

- MVP 用 `docker-compose` 本地起 MySQL + Redis + agent-gateway-service，验证"统一入口 -> 路由 -> 检索/诊断能力 -> 统一答案"闭环。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `gateway_chat_total` | 问答次数（按 intent label） |
| `gateway_route_accuracy` | 路由准确率（评测集/用户反馈，健康度核心） |
| `gateway_tool_call_total` | 工具调用次数（按 route label） |
| `gateway_delegation_total` | 诊断能力委托次数（按 status label） |
| `gateway_chat_latency_seconds` | 整问答延迟（Histogram） |
| `gateway_cache_hit_total` | 查询缓存命中 |
| `gateway_low_confidence_total` | 低置信度转人工次数 |
| `gateway_unknown_intent_total` | 未识别意图转人工次数 |
| `gateway_dependency_unreachable_total` | 下游不可达降级次数 |

### 10.2 trace 串联

- 埋点入口用 AgentScope `TracingMiddleware`（E 侧不再自建 obs 层），`trace_id` 从 E 生成，显式调用诊断 Agent 服务时透传 `traceparent`（🔴 §4.3）。
- 链路查看走 OpenTelemetry + Tempo/Jaeger，过程回放用统一 `Agent` 的 `reply_stream` 事件流；**不选 AgentScope Studio**（已归档停止维护，不列为选型）。
- `AnswerAudit` 记录问题/意图/路由/工具链/答案/`trace_id`，`/explain` 可回溯。

### 10.3 兜底

- **路由失败**：`UNKNOWN` 意图 -> 转人工"该问题暂不支持"。
- **下游不可达**：检索/诊断能力故障 -> 降级"该能力暂时不可用"或切换备选能力。
- **低置信度**：`confidence < 0.6` -> `needs_human_review=True`。
- **委托超时**：诊断能力超时 -> 降级"诊断未完成，已转人工"（继承诊断能力 §8.3）。
- **预算超限**：轻量组合的工具调用次数/时长/证据量超预算 -> 降级"建议改用诊断能力深度诊断"或转人工。

---

## 11. 实现步骤

### 阶段一：骨架与意图路由（2 周）

1. 搭 `agent_gateway_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 实现 `IntentRouter`（规则优先 + LLM 兜底）（§9.1）。
3. 实现统一 `Agent` + `Toolkit` 的轻量路由编排（§9.2），单工具直答路径跑通。
4. 实现 `ReadOnlyToolGate` + `assert_dependencies_reachable` 启动断言（§9.4）。

### 阶段二：工具与委托（2-3 周）

5. 实现 `ToolExecutor` + `ToolRegistry`（检索工具封装为 AgentScope `Tool` 并注册进 `Toolkit`）（§7.2）。
6. 实现 ACL 客户端（rag-service 检索）（§6.2）。
7. 实现 `AgentDelegator`（显式服务调用委托诊断能力 + traceparent 透传）（§7.3、§4.3 🔴）。
8. 验证 E -> 诊断能力 -> A/各上下文 REST 的 trace 全链路串联。

### 阶段三：统一输出与可观测（1-2 周）

9. 实现 `AgentAnswer` 统一格式 + `AnswerAudit` 审计（§5.2）。
10. 实现 `/agent/explain` 回溯端点（§9.5）。
11. 接 OpenTelemetry + prometheus 指标（§10.1），路由准确率告警。

### 阶段四：加固、评测与试点（1 周）

12. 沉淀评测集（典型问题 + 预期路由/答案），回归路由准确率。评测栈自建（自研 `EvalRunner` + 三层判定 + pass^k + CI 门禁），judge 用独立包 `py-openjudge`--AgentScope 2.0 **无内生评测**，1.x 的 `agentscope.evaluate` 已删除且未回归。
13. 兜底链路全测（UNKNOWN/下游不可达/低置信度/委托超时/预算超限）。
14. 灰度统一入口试点，收集"路由是否准、答案是否对"反馈。
15. 确认 🔴 决策点（§4.3 traceparent 透传、§11 草稿与编排 纳入时机、路由规则覆盖）。

> **前提**：E 必须在两项检索能力与诊断能力成型后启动（[RAG服务引入路线.md](RAG服务引入路线.md) §3）。E 是收口，不是起步。

---

## 12. 约束落地检查清单

- [ ] E 无自有检索/推理能力，全部调两项检索能力 + 委托诊断能力（§1.3 收口不造新能力）。
- [ ] 所有注册工具 `read_only=True`，`ReadOnlyToolGate` 启动断言生效（继承诊断能力）。
- [ ] `assert_dependencies_reachable` 启动断言：检索与诊断能力不可达时拒绝启动（E 收口前提）。
- [ ] E 不调过点引擎放行/拦截 API，
- [ ] 检索工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承文档检索、制造追溯与诊断能力的版本一致性）。
- [ ] 工具调用前按 `TenantContext` 权限过滤，拒绝记录可观测（继承诊断能力）。
- [ ] 深度诊断用显式服务调用委托诊断能力，E 不自己多步推理；应用层预算（工具次数 / 时长 / 证据量）限制轻量组合规模。
- [ ] 所有路径收敛到 `AgentAnswer` 统一格式，带 `route_taken` + `tool_chain` + `sources`。
- [ ] `trace_id` 从 E 发起，委托诊断能力时透传 `traceparent`（🔴 §4.3），全链路可回溯。
- [ ] 路由失败/下游不可达/低置信度/委托超时 -> 转人工兜底，不硬答。
- [ ] 答案带 `disclaimer`：辅助信息，最终处置需工程师确认。
- [ ] E 在两项检索能力与诊断能力成型后启动，不提前做（避免套壳）。

---

## 13. 面试防守 Q&A

**Q：MVP 选了哪三类意图？为什么？**
A：选了追溯事实（`TRACE_FACT`->制造追溯工具）、根因诊断（`ROOT_CAUSE`->显式服务调用委托诊断能力）、文档查询（`DOC_LOOKUP`->文档检索工具）三类。原因：① 这三类覆盖了检索与诊断能力的核心能力，验证"统一入口 -> 路由 -> 工具/委托 -> 统一答案"完整闭环；② 覆盖了两种路由形态--单步工具直答（检索）与跨能力显式调用（诊断能力），验证 E 的"广深分工"；③ 这三类是工程师最高频场景。其余意图（草稿委托、轻量组合）按相同范式扩展。

**Q：E 和诊断能力怎么分工？是不是重复了？**
A：不重复，广深分工。E 是"广而浅"的路由收口（意图分类 -> 选工具 / 显式服务调用委托 -> 轻量组合），诊断能力是"深而专"的追溯多步诊断（≤10 步 5M1E 推理）。简单追溯事实（"过了哪几站"）调制造追溯工具直答；深度根因诊断（"为什么不良"）E 显式调用诊断能力。E 的轻量组合只允许 1-2 次工具调用（应用层独立计算次数、时长与证据预算），深度多步委托诊断能力。这 reconcile 了 RAG 引入路线 §3"Agentic 在预算内完成多步组合"和 AGENT 引入路线 §2.1"诊断能力自己决定下一步查什么"--E 的"多步"是轻量组合，深度推理委托诊断能力。

**Q：E 怎么判断该走哪项能力？**
A：意图路由规则优先 + LLM 兜底。高频问题关键词命中直定意图（"根因/5M1E"->诊断能力，"怎么处置/SOP"->B，"过了哪几站"->A），命中不了才走 LLM 结构化输出分类（`IntentCategory` 固化为 Enum）。路由准确率是 E 的核心健康度指标。UNKNOWN 转人工不硬答。

**Q：委托诊断能力时怎么保证 trace 不断？**
A：E 委托诊断能力时注入 `traceparent` header，诊断能力接收后透传到其工具调用与下游 Java REST，使 E -> 诊断能力 -> A/各上下文 REST 的 trace 全链路串联（🔴 待与诊断能力实现确认 `traceparent` 接收与透传，§4.3）。MVP 兜底：若诊断能力未透传，E 侧仍记录委托 span，但诊断能力内部 trace 断裂（降级可观测）。`AnswerAudit` 记录 `trace_id`，`/explain` 可回溯。

**Q：E 路由错了怎么办？**
A：三重兜底。一是路由准确率作为核心指标监控 + 评测集回归；二是诊断能力委托有超时（≤60s）和置信度阈值，诊断能力发现问题不属于诊断范畴返回低置信度，E 据此转人工；三是 UNKNOWN 和路由失败都转人工不硬答。`AnswerAudit` 记录每次路由决策，工程师可回溯"为什么走了诊断能力"，反馈调优规则。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地，且 E 是这几项能力里最后做的。重点是四条架构判断：① E = 统一入口收口层，不造新能力，全部调检索工具 + 显式调用委托诊断能力；② 意图路由规则优先 + LLM 兜底；③ 广深分工--E 轻量组合（应用层工具次数/时长预算），深度委托诊断能力；④ 继承全部只读红线 + 依赖可达性启动断言。E 必须在检索与诊断能力成型后启动，否则退化为套壳。诚实 + 体现架构判断力，比硬吹"已上线 Agentic RAG"得分高。

---

## 14. 一句话定位

"Agentic RAG MVP 把文档检索、制造追溯与诊断收口成统一入口--3 类意图（追溯事实/根因诊断/文档查询）规则优先 + LLM 兜底路由：单步意图调检索工具直答，深度根因用显式服务调用委托诊断能力（`traceparent` 透传串联 trace）。E 不造新能力，全部调检索工具 + 显式调用委托诊断能力；广深分工--E 只做轻量组合（应用层独立计算工具次数、时长与证据预算），跨上下文深度多步委托诊断能力；继承全部只读红线（`ReadOnlyToolGate`/版本/权限/不进过点主事务）+ 依赖可达性启动断言，所有路径收敛到带 `route_taken`+`tool_chain`+`sources` 的统一答案。E 是最后一步，等检索与诊断能力成型后收口。"

---

## 15. 与检索能力、Agent 三项能力的契约对齐与待办

| 契约 | 状态 | 待办 |
|------|------|------|
| 诊断能力 `/agent/diagnose` 接受并透传 `traceparent` | 🔴 待对齐 | 与诊断能力实现确认 trace 透传（§4.3） |
| A `/rag/trace/expand` 契约 | 🔴 待对齐 | 与制造追溯读模型确认 expand 端点入参（seed/as_of/version/version_kind）与返回的 `evidence_set_id` |
| B `/rag/docs/search` 契约 | 🔴 待对齐 | 与文档型 RAG 确认 search 端点入参 |
| 草稿委托（`DRAFT_REQUEST`） | ⏳ §11 | 草稿能力就绪后纳入显式服务调用委托 |
| 轻量组合（多工具） | ⏳ §11 | 应用层工具次数/时长预算已预留（默认 1-2 次），按场景放宽 |
| 流程编排纳入 | ⏳ 远期 | 编排能力成型后作为显式服务调用委托目标 |
