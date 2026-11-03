# Agentic RAG 实现方案（Python 技术栈：检索内多跳编排 MVP）

> 本文是 [RAG服务引入路线.md](RAG服务引入路线.md) §3「Agentic 的正确定位」所述 **Agentic 能力**的**实现层落地**，与 [Agentic RAG-详细设计.md](./Agentic%20RAG-详细设计.md) 的关系：
> - **详细设计**是**设计层**--检索编排、工具注册、统一输出、包结构与约束的全景；
> - **本文**是**实现层**--把设计补全到可落地的 MVP，包含依赖清单、ACL 只读 REST 契约、AgentScope 编排代码、Docker 部署、测试策略与待办契约。
>
> **边界先行**：本文只实现**检索内编排**（文档检索 + 制造追溯），**不实现意图路由**、**不实现跨能力委托**。统一入口与分诊见 [Agent入口网关-意图路由.md](../AGENT服务/Agent入口网关-意图路由.md)。
>
> **技术栈**：Python（FastAPI + AgentScope 2.0 + Pydantic）。与三大 MES 服务（Java/Spring）跨语言共存，通过 REST 解耦，互不侵入。
> **底座口径**：与 [基于AgentScope2.0的架构设计.md](../基于AgentScope2.0的架构设计.md) §3.2 对齐（`agentscope>=2.0.0`、Python 3.12+）--统一 `Agent`（内置 ReAct）+ `Toolkit` 承载检索内工具选择；`AgentState` + `SessionService`（Redis-backed，2.0.5 起由 `SessionManager` 改名而来）承载跨请求会话。框架并未提供 `Handoffs` / `Routing` 这类原语，本文也不需要它们。
> **口径纪律**：Agentic RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Agentic RAG"。

---

## 1. 设计目标与边界

### 1.1 定位（rag-service 的检索内编排）

Agentic RAG 不是入口层，不判断"该找检索还是诊断/草稿"；它只解决一次检索请求内部的编排：**证据够不够、还要不要追加一次查询、追加查哪个上下文、什么时候停**。入口分诊与跨能力委托在网关（[Agent入口网关-意图路由.md](../AGENT服务/Agent入口网关-意图路由.md)）。

### 1.2 MVP 范围

跑通"检索请求 -> 起点判定 -> 单跳/多跳 -> 证据收口 -> `RagAnswer`"闭环，聚焦三种形态：

| 形态 | 触发条件 | MVP 典型问题 |
|------|---------|-------------|
| **单跳追溯** | 有种子（SN/批次/工单），只问事实链 | "SN-001 过了哪几站""这批用了哪批锡膏" |
| **单跳文档** | 纯知识类问题，无制造对象 | "SPI 报警怎么处置""首件检验流程" |
| **多跳组合** | 需要"事实 + 当时版本 + 处置依据" | "这个批次当时用哪版工艺，对应处置要求是什么" |

> 三种形态都只调两个只读工具；MVP 不接第三个数据源，也不做跨能力调用。

### 1.3 硬边界（一开口就要讲）

| 边界 | 说明 | 落地（MVP 具体动作） |
|------|------|----------------------|
| **不做意图分诊** | 不判断"该找检索还是诊断/草稿" | 无分类器、无 `IntentCategory`；入口分派在网关 |
| **不做跨能力委托** | 不调诊断/草稿/编排 | ACL 只有制造追溯与文档检索两个客户端 |
| **只读** | 无写工具 | `ReadOnlyToolGate` 启动断言；写工具不注册（§9.4） |
| **不进过点主事务** | 不调过点放行/拦截 API | 过点 toolset 不暴露 `pass/judge` |
| **版本一致性** | 查工艺带 `version`+`version_kind` | 检索工具强制版本锚点入参，ACL 校验 |
| **权限隔离** | 工具调用前按 `tenant_scope` 过滤 | `TenantContext` 前置过滤 |
| **查询有界** | 不允许无限展开 | 工具次数 ≤2、证据条数 ≤20、检索时长 ≤15s |
| **证据约束** | 制造事实来自记录，处置建议来自适用文档 | `sources` 强制引用；缺证据显式标注 |
| **依赖可达性** | 编排前提：两个检索能力必须可达 | `assert_dependencies_reachable` 启动断言（§9.4） |

### 1.4 与其他文档、检索能力的关系

- **与详细设计**：详细设计给全景（起点判定、停止条件、工具注册、统一输出）；本文补全依赖、ACL、代码、部署与测试。
- **与入口网关**：网关把 `RETRIEVAL_REQUEST` 转成 `RetrievalRequest`（问题 + 种子 + 版本锚点 + `session_id`）调本文；本文回 `RagAnswer`，由网关转成统一答案。**不传消息历史**。
- **与制造追溯 / 文档检索**：封装为两个只读工具，不重建检索能力。
- **与诊断 / 草稿 / 编排**：MVP 不接触；需要这些能力的问题由网关分派，不进本文。

### 1.5 与 Java 技术栈的关系

- 本文模块用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只通过 REST 调制造追溯与文档检索的只读接口。
- 跨语言物理边界天然强制只读（与诊断能力同构，[FactoryAgent-诊断能力.md](../AGENT服务/FactoryAgent/FactoryAgent-诊断能力.md) §1.3）。
- 复用两项检索能力的既有契约，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 版本 | 选型理由 |
|------|------|------|---------|
| 语言 | Python | 3.12+ | 与 agent-service 同栈，复用 AgentScope + Model 抽象 |
| Web 框架 | **FastAPI** | 0.110+ | 异步、原生 OpenAPI |
| Agent 底座 | **AgentScope 2.0** | `agentscope>=2.0.0` | 统一 `Agent` 类（内置 ReAct）+ `Toolkit` 承载检索内工具选择；2.0 无 `Routing`/`Handoffs`，本文也不需要 |
| LLM 抽象 | AgentScope `Model` | 2.0+ | provider 可插拔（DashScope / Ollama / OpenAI 兼容） |
| 数据校验 | **Pydantic** | v2 | 工具入参 / `RagAnswer` / 状态 schema 即类型 |
| HTTP 客户端 | **httpx** | 0.27+（异步） | 调制造追溯与文档检索只读 REST |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 2.0+ | 检索 trace、答案审计 |
| 会话状态 | AgentScope `AgentState` + `SessionService`（Redis-backed；2.0.5 起由 `SessionManager` 改名） | 2.0+ | 多跳过程与跨请求续跑 |
| 缓存 | **redis-py (async)** | 5.0+ | 相同检索请求短缓存 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | - | trace 从检索入口发起 |
| 配置 | pydantic-settings | 2.0+ | 环境变量统一管理 |
| 部署 | rag-service 内 `agentic` 模块（可独立进程 `agentic-rag-service`） | - | 归属 rag-service 能力边界，不对外承担统一入口 |

### 2.2 为什么用 AgentScope 轻量编排

- 固定查询计划覆盖不了现场：先追溯拿到工艺版本，才知道该查哪版 SOP。需要模型在**极小工具面**内选下一步。
- 但动作空间被硬限制：`Toolkit` 只有两个只读工具，预算 ≤2 次；模型不能自由探索任意关系。
- 2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 已删除：顺序组合用 `await` 串联，并行用 `asyncio.gather` 并发独立 `Agent.reply()` 后在应用层合并；编排代码归 `flows/`。
- 与诊断能力同底座：可复用 `AgentState` + `SessionService` 与 `TracingMiddleware`，但**不复用其工具集**。

### 2.3 为什么不做意图分诊与委托

- **定位**：Agentic RAG 只决定"检索内还查不查"；判断"该找哪项能力"是跨能力职责（[RAG服务引入路线.md](RAG服务引入路线.md) §3）。
- **依赖方向**：若本文委托诊断，rag-service 会反向依赖 agent-service；网关又依赖 rag-service，形成环。放在网关，依赖保持单向。
- **变化频率**：入口分诊随渠道/话术/权限策略变化；检索编排随数据模型/证据契约变化，不应绑在一起。
- **结构保证**：`infrastructure/acl/` 只有两个检索客户端，物理上不存在诊断/草稿调用路径。

### 2.4 部署形态

- 作为 rag-service 的 `agentic` 模块部署（可独立进程/独立扩缩容），只暴露 `/rag/agentic/query` 与 `/rag/explain/{audit_id}`。
- 不提供 `/agent/chat`；统一问答入口在网关。
- LLM 视安全策略二选一（云端 API 或本地模型）。
- 依赖制造追溯与文档检索可达；不可达时启动断言失败。
- MVP 用 `docker-compose` 本地起 MySQL + Redis + agentic-rag-service（§9.6）。

### 2.5 依赖清单（pyproject.toml 片段）

```toml
[project]
name = "agentic-rag-service"
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
│ agentic-rag-service（rag-service 的检索编排模块）                  │
│                                                                    │
│  ┌──────────────────┐  ┌────────────────────────────────────────┐ │
│  │ FastAPI          │─▶│ AgenticRagService                      │ │
│  │ /rag/agentic/query│  │  起点判定 -> 单跳/多跳 -> 证据收口      │ │
│  └──────────────────┘  └───────────────┬────────────────────────┘ │
│                                        │                          │
│                      ┌─────────────────▼──────────────────┐       │
│                      │ flows/retrieval_flow.py            │       │
│                      │ 单跳直调 / Agent ReAct 追加检索     │       │
│                      └─────────────────┬──────────────────┘       │
│                                        │                          │
│                 ┌──────────────────────┴───────────────┐          │
│                 ▼                                      ▼          │
│        ┌──────────────────┐                   ┌──────────────────┐ │
│        │ ToolExecutor     │                   │ RetrievalPlanner │ │
│        │ 权限/版本/trace  │                   │ 起点 + 停止条件   │ │
│        └────────┬─────────┘                   └──────────────────┘ │
│         ┌───────▼─────────┐                                        │
│         │ ToolRegistry    │ (只读白名单: query_traceability / search_docs)
│         └─────────────────┘                                        │
│  ┌──────────────────┐  ┌──────────────────┐                        │
│  │ AnswerAudit(MySQL)│ │ QueryCache(Redis)│                        │
│  └──────────────────┘  └──────────────────┘                        │
└───────────────────────────────────────┬────────────────────────────┘
                    │ httpx 只读 REST
        ┌───────────┴───────────┐
        ▼                       ▼
  ┌──────────────┐        ┌──────────────┐
  │ 制造追溯      │        │ 文档检索      │
  │ /rag/trace/  │        │ /rag/docs/   │
  │ expand       │        │ search       │
  └──────────────┘        └──────────────┘
```

### 3.1 关键设计决策

- **编排即工具选择**：无意图分类；只回答"还查不查、查哪个"。
- **单跳优先**：能一次调用答的不进 ReAct，省成本也省延迟。
- **预算硬约束**：工具次数/时长/证据量由应用层独立计算，超限即停。
- **统一输出**：单跳与多跳都收敛到 `RagAnswer`。
- **trace 从检索入口发起**：`trace_id` 在本模块生成，透传到两个检索工具。

---

## 4. 检索编排（起点、多跳、停止）

### 4.1 起点判定（单跳 vs 多跳）

```text
plan_start(request)
   ├─ 有种子 且 无文档类词(处置/SOP/流程/标准/怎么修) -> 单跳 query_traceability
   ├─ 无种子 且 无追溯类词(过点/物料/批次/设备/人员/工单) -> 单跳 search_docs
   ├─ 种子缺失但问题指向制造对象 -> 先检索线索，再决定是否追溯（多跳）
   └─ 其余 -> 多跳：Agent 在 Toolkit 内选择（≤2 次调用）
```

- 这是**检索起点启发式**，不是意图分类：判错只影响多查/少查一次，不改变能力边界。
- 单跳路径不调 LLM（工具参数由请求字段直接给出）；只有多跳进入 ReAct。

### 4.2 多跳工具选择

```text
Agent.reply(question + 已解析的 seed/version 锚点)
   ├─ query_traceability -> 过点/物料/设备/人员/工艺版本
   ├─ search_docs        -> 按已解析版本召回 SOP/检验标准/历史 8D
   └─ 观察 -> 证据是否足够？足够停止；不足追加一次（受预算约束）
```

- 工具面极小：只有两个只读工具，不允许任意 SQL 或无界关联。
- 版本先解析后检索：先拿 `version`+`version_kind`，再查对应版本文档。
- 并行只用于互不依赖的补充查询：`asyncio.gather` 并发独立调用，结论在应用层合并。

### 4.3 停止条件

1. **证据充分**：所需槽位齐（事实链、版本锚点、处置依据）。
2. **无新增证据**：一次调用没有带来新证据。
3. **预算耗尽**：工具次数 / 时长 / 证据条数到上限。
4. **冲突**：版本冲突、权限拒绝、租户范围不匹配。

停止时证据不完整：`needs_human_review=True`，`detail["missing"]` 说明缺口，不编造。

### 4.4 预算

| 预算项 | MVP 默认 | 说明 |
|--------|---------|------|
| 工具调用次数 | ≤2 | 单跳 1 次，多跳最多再追加 1 次 |
| 证据条数 | ≤20 | 超出截断并标注 |
| 单次检索请求时长 | ≤15s（🔴 待压测标定） | 超时返回已得证据 + 转人工提示 |

预算由应用层 `BudgetPolicy` 独立计算；超限抛 `BudgetExceededError`（非框架异常）。

> **口径对齐**：[RAG服务引入路线.md](RAG服务引入路线.md) §3 给的上限是「默认最多 6 个编排步骤」，并列出 4 个只读工具。本文 MVP 按「单跳优先」收紧到 **2 次工具调用 + 2 个工具**，差异是刻意的：路线给的是能力上限，MVP 给的是首版灰度预算，压测标定后可放开（受同一 `BudgetPolicy` 约束，不改代码结构）。

---

## 5. 工具注册与统一答案

### 5.1 工具注册（两个只读工具）

| 工具名 | 所属能力 | 入参 | 版本校验 |
|--------|-----------|------|---------|
| `query_traceability` | 制造追溯 | sn/batch_no/wo_id, as_of, version, version_kind | 历史回放带版本锚点 |
| `search_docs` | 文档检索 | query, version, version_kind | 强制版本锚点过滤 |

```python
class ToolDescriptor(BaseModel):
    name: str
    description: str
    route: str                        # "traceability" / "doc_search"
    read_only: bool                   # 必须 True
    args_schema: type[BaseModel]
    required_tenant_scopes: list[str]
```

- 两工具均注册为 AgentScope `Tool` 并挂进同一个 `Toolkit`；外部检索服务也可按需用 MCP 接入。
- `read_only=False` 启动时拒绝注册（继承诊断能力 `ReadOnlyToolGate`）。
- 工具调用前按 `TenantContext` 权限过滤。
- 检索工具强制版本锚点（`version`+`version_kind`）入参。

### 5.2 统一答案（RagAnswer）

```python
class AnswerSource(BaseModel):
    source_type: str          # "trace_record" / "sop_doc"
    ref: str                  # "evidence_id=..." / "SOP:WELD-014@v3"
    route: str                # "traceability" / "doc_search"

class RagAnswer(BaseModel):
    question: str
    retrieval_path: str          # "traceability" / "doc_search" / "doc_search+traceability" / "NONE"
    summary: str
    detail: dict                 # 结构化证据（事实链 / SOP 片段 / 缺失项）
    evidence_set_id: str
    sources: list[AnswerSource]
    confidence: float
    tool_chain: list[str]        # ["query_traceability", "search_docs"]
    stop_reason: str             # "enough" / "no_new_evidence" / "budget" / "conflict"
    trace_id: str
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"
```

- `retrieval_path` + `tool_chain` 透明：能看出单跳/多跳、调了什么、为何停止。
- `sources` 强制引用；证据回查按 `evidence_set_id` 取，不使用图节点引用。
- 不含"意图"字段：意图属于网关的 `AgentAnswer`。
- `disclaimer` 不可省。

### 5.3 红线继承

① 只读闸（`ReadOnlyToolGate`）；② 版本闸（`version`+`version_kind`）；③ 权限闸（`TenantContext` 前置过滤）；④ 不进过点主事务闸（过点 toolset 不暴露 `pass/judge`）。

---

## 6. 实现方案

### 6.1 检索编排服务（AgenticRagService）

> 下面代码给的是**编排形状**（起点判定 + 单跳直调 / 多跳 Agent + 应用层预算 + `SessionService` 会话恢复）。框架名只写 ADR-01 / 架构设计附录 A 已核实的原语与最小构造骨架（`Agent(name=..., model=..., toolkit=..., offloader=...)`、`reply` / `reply_stream`）；未核实的参数与方法名不进代码。

```python
class BudgetExceededError(Exception):
    """应用层预算守卫（非 AgentScope 内置异常）：工具次数 / 时长 / 证据量超限。"""

class AgenticRagService:
    def __init__(
        self,
        agent_factory: RetrievalAgentFactory,        # 装配统一 Agent + Toolkit（两个只读工具）
        budget_policy: BudgetPolicy,                 # 应用层预算
        session_service: SessionService,             # AgentState 跨请求恢复（Redis-backed）
        tool_executor: ToolExecutor,
        cache: QueryCache,
        audit_repo: AnswerAuditRepo,
        metrics: MetricsCollector,
    ) -> None: ...

    async def query(self, request: RetrievalRequest, tenant: TenantContext) -> RagAnswer:
        # 1. 缓存（问题 + 租户 + 版本锚点）
        cached = await self._cache.get(request, tenant)
        if cached:
            self._metrics.cache_hit.inc()
            return cached

        budget = self._budget_policy.for_request(request)
        state = await self._session_service.load(request.session_id)   # 可空
        start = plan_start(request)                                     # §4.1

        try:
            if start.tool_name:                                         # 单跳：0 次 LLM
                state = await self._tool_executor.run(
                    start.tool_name, request, tenant, budget, state,
                )
            else:                                                       # 多跳：Agent ReAct
                agent = self._agent_factory.build(tenant, budget, state)
                msg = await asyncio.wait_for(
                    agent.reply(Msg(name="user", content=request.question, role="user")),
                    timeout=budget.timeout_seconds,
                )
                state = self._from_msg(state, msg)
        except (BudgetExceededError, asyncio.TimeoutError) as e:
            self._metrics.fallback.inc("timeout_or_budget")
            return self._partial_answer(request, state, f"检索未完成: {e}")

        answer = self._build_answer(request, state)
        await self._session_service.save(request.session_id, state)
        await self._audit_repo.record(request, answer)
        await self._cache.set(request, tenant, answer)
        return answer
```

- 编排与工具执行分离（SRP）；超时/预算超限返回已得证据 + 转人工提示。
- 单跳路径不经过 Agent，避免为简单问题付 ReAct 成本。

### 6.2 ACL 防腐层（只读 REST 契约）

| 协作方 | 只读 REST | 用途 | 超时 |
|--------|----------|------|------|
| 制造追溯 | `POST /rag/trace/expand` | 关系型证据子集展开 | 5s |
| 文档检索 | `GET /rag/docs/search` | SOP / 标准 / 8D 片段 | 2s |

```python
class TraceRagAclClient:
    """制造追溯只读 ACL。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def query_traceability(
        self, seed: dict, as_of: datetime, version: str, version_kind: str,
        tenant: TenantContext,
    ) -> TraceEvidenceSetView:
        resp = await self._http.post(
            "/rag/trace/expand",
            json={"seed": seed, "as_of": as_of.isoformat(),
                  "version": version, "version_kind": version_kind},
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


class DocRagAclClient:
    """文档检索只读 ACL。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def search_docs(
        self, query: str, version: str, version_kind: str, tenant: TenantContext,
    ) -> DocEvidenceSetView:
        resp = await self._http.get(
            "/rag/docs/search",
            params={"query": query, "version": version, "version_kind": version_kind},
            headers=tenant.headers(), timeout=2.0,
        )
        resp.raise_for_status()
        return DocEvidenceSetMapper.to_view(resp.json())

    async def ping(self) -> bool:
        try:
            r = await self._http.get("/health", timeout=1.0)
            return r.status_code == 200
        except Exception:
            return False
```

- 外部 DTO 不进核心，只暴露 `TraceEvidenceSetView` / `DocEvidenceSetView`（ACL 约束）。
- 所有 ACL 客户端带 `ping()` 供启动断言（§9.4）。
- **没有诊断/草稿客户端**：这是"不做跨能力委托"的结构性保证。

### 6.3 版本一致性保证

继承文档检索与制造追溯：工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。本文不另搞版本管理；历史问题必须按当时版本检索。

---

## 7. 编排执行（单跳与多跳）

### 7.1 编排形状

```text
[检索请求] 起点判定（§4.1）
      │
      ├─ 单跳 ──▶ ToolExecutor.run(tool_name) ──▶ 证据收口
      ├─ 多跳 ──▶ Agent ReAct（Toolkit 两个只读工具，≤2 次）──▶ 证据收口
      └─ 冲突/超限 ──▶ 返回已得证据 + 转人工提示
```

- 单跳 = 应用层直接 `await`，不进入 Agent。
- 多跳 = 一个 Agent 的 ReAct 循环；不做多 Agent 协同（本 MVP 无此需求）。

### 7.2 工具执行器（ToolExecutor）

```python
class ToolExecutor:
    """执行单个只读检索工具：权限 / 预算 / trace 校验。"""

    def __init__(self, registry: ToolRegistry, trace_repo: RetrievalTraceRepo,
                 metrics: MetricsCollector) -> None:
        self._registry = registry
        self._trace_repo = trace_repo
        self._metrics = metrics

    async def run(self, tool_name: str, request: RetrievalRequest,
                  tenant: TenantContext, budget: ToolCallBudget,
                  state: RetrievalState) -> RetrievalState:
        tool = self._registry.get(tool_name)
        if tool is None or not tenant.can_access(tool.required_tenant_scopes):
            await self._trace_repo.save_denied(tool_name, tenant)
            self._metrics.tool_denied.inc(tool_name)
            state.denied = True
            state.stop_reason = "conflict"
            return state
        if state.tool_calls >= budget.max_tool_calls:
            state.stop_reason = "budget"
            return state
        t0 = time.perf_counter()
        try:
            view = await tool.handler(request=request, tenant=tenant)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await self._trace_repo.save_ok(tool_name, view, latency_ms)
            state.evidence.append(view)
            state.tool_chain.append(tool_name)
            state.tool_calls += 1
            self._metrics.tool_ok.inc(tool_name)
        except Exception as e:
            await self._trace_repo.save_error(tool_name, e)
            self._metrics.tool_error.inc(tool_name)
            state.stop_reason = "conflict"
            state.error = str(e)
        return state
```

- 工具名由起点判定或多跳 Agent 给出，**不再从"意图"映射**。
- 预算在调用前校验，超限直接停止并标记 `stop_reason="budget"`。

---

## 8. 推荐包结构（Python src layout）

```text
agentic_rag_service/
  app/
    api/
      query_router.py          # POST /rag/agentic/query, GET /rag/explain/{audit_id}
      schemas.py
    application/
      agentic_rag_service.py   # 编排入口
      retrieval_planner.py     # 起点判定 + 停止条件 + 预算
      flows/
        retrieval_flow.py      # 单跳直调 / 多跳 ReAct 的编排形状
    domain/
      retrieval.py             # RetrievalRequest / RagAnswer / RetrievalState
      answer.py                # AnswerSource
      tool.py                  # ToolDescriptor / ToolRegistry / ReadOnlyToolGate
      tenant.py                # TenantContext
    infrastructure/
      ai/
        retrieval_agent_factory.py  # 统一 Agent + Toolkit 装配
        model_factory.py            # AgentScope Model 接入
        budget_policy.py            # 应用层工具次数 / 时长 / 证据预算
      session/
        session_service.py          # AgentState + SessionService（Redis-backed）
      acl/
        trace_rag.py                # 制造追溯只读
        doc_rag.py                  # 文档检索只读
      persistence/
        models.py                   # answer_audit / retrieval_trace
        audit_repo.py
      redis_/
        query_cache.py
      obs/
        tracing.py                  # TracingMiddleware 接线
        metrics.py                  # 检索侧业务指标
    config.py
    main.py                         # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
  Dockerfile
  docker-compose.yml
```

- `infrastructure/acl/` 只有两个检索客户端；没有诊断/草稿客户端。
- `application/retrieval_planner.py` 承载起点判定与停止条件，便于单测。

---

## 9. 关键代码骨架

### 9.1 起点判定（RetrievalPlanner）

```python
class StartPlan(BaseModel):
    tool_name: str | None      # 非空 = 单跳直调；None = 进入多跳
    reason: str

TRACE_HINTS = ("过点", "物料", "批次", "设备", "人员", "工单", "锡膏")
DOC_HINTS = ("处置", "SOP", "流程", "标准", "怎么修", "作业指导")

def plan_start(request: RetrievalRequest) -> StartPlan:
    """只看请求形态决定检索起点：不是意图分类。"""
    q = request.question
    if request.seed and not any(h in q for h in DOC_HINTS):
        return StartPlan(tool_name="query_traceability", reason="有种子且无文档诉求")
    if not request.seed and not any(h in q for h in TRACE_HINTS):
        return StartPlan(tool_name="search_docs", reason="无种子且无追溯诉求")
    return StartPlan(tool_name=None, reason="证据缺口需多跳组合")
```

> 起点判定没有 LLM、没有 Enum、不产出"意图"；判错只影响多查/少查一次。

### 9.2 检索 Agent 装配（RetrievalAgentFactory）

```python
class RetrievalAgentFactory:
    """装配检索内多跳 Agent：Toolkit 只有两个只读检索工具。"""

    def __init__(self, tool_registry: ToolRegistry, delegator_free_model: Model,
                 offloader: LocalWorkspace) -> None:
        self._registry = tool_registry
        self._model = delegator_free_model
        self._offloader = offloader

    def build(self, tenant: TenantContext, budget: ToolCallBudget,
              state: RetrievalState | None = None) -> Agent:
        return Agent(
            name="agentic-rag",
            model=self._model,
            toolkit=self._registry.toolkit_for(tenant),   # 只读白名单
            offloader=self._offloader,
            budget=budget,                                # 应用层独立计算
        )
```

- 没有按意图分支，也没有委托 Tool。
- `agentscope/pipeline` / `msghub` 自 2.0.0 起已删除；顺序组合用应用层 `await`，并行用 `asyncio.gather` 并发独立 `Agent.reply()` 后显式合并。`GoalPipeline` / `TeamPipeline` 标记 experimental，不进生产。构造参数以官方源码树为准（核对结论见 [ADR-01](../架构设计v2/00-架构决策记录ADR.md)）。

### 9.3 工具执行器

见 §7.2。

### 9.4 启动断言（只读 + 依赖可达性）

```python
class ReadOnlyToolGate(Exception):
    """启动时发现非只读工具，拒绝启动。"""
class DependencyUnreachableGate(Exception):
    """启动时发现检索能力依赖不可达，拒绝启动。"""

async def assert_dependencies_reachable(clients: dict[str, object]) -> None:
    """编排前提：两个检索能力必须可达。"""
    unreachable = []
    for name, client in clients.items():
        if not await client.ping():
            unreachable.append(name)
    if unreachable:
        raise DependencyUnreachableGate(f"依赖不可达: {unreachable}（检索编排要求检索能力就绪）")

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.tool_registry
    # 1. 只读断言
    registry.validate_on_startup()
    # 2. 依赖可达性断言
    await assert_dependencies_reachable(app.state.acl_clients)
    yield
```

### 9.5 FastAPI 入口

```python
router = APIRouter(prefix="/rag", tags=["agentic-rag"])

@router.post("/agentic/query", response_model=RagAnswer)
async def agentic_query(
    req: RetrievalRequest,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: AgenticRagService = Depends(get_agentic_rag_service),
) -> RagAnswer:
    return await svc.query(req, tenant)

@router.get("/explain/{audit_id}", response_model=RetrievalAudit)
async def explain(audit_id: str, tenant: TenantContext = Depends(tenant_from_token)) -> RetrievalAudit:
    """回溯某次检索的路径、工具链与停止原因。"""
    ...
```

- 只有检索编排接口；`/explain` 回溯检索过程。

### 9.6 配置与部署

```python
# app/config.py
class Settings(BaseSettings):
    audit_dsn: str = "mysql+asyncmy://root:root@mysql:3306/agentic_rag?charset=utf8mb4"
    redis_url: str = "redis://redis:6379/0"
    # 两项检索能力只读 REST
    trace_rag_base_url: str = "http://rag-service:8000"
    doc_rag_base_url: str = "http://doc-rag-service:8000"
    # LLM provider（AgentScope Model 可插拔）
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    # 应用层预算（非 AgentScope 参数名）
    max_tool_calls: int = 2
    max_evidence_items: int = 20
    query_timeout_seconds: int = 15
    confidence_threshold: float = 0.6
    class Config:
        env_prefix = "AGENTIC_RAG_"
```

```yaml
# docker-compose.yml（MVP 本地起）
version: "3.9"
services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: agentic_rag
    ports: ["3309:3306"]
    volumes:
      - ./sql/init.sql:/docker-entrypoint-initdb.d/init.sql
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  agentic-rag-service:
    build: .
    depends_on: [mysql, redis]
    environment:
      AGENTIC_RAG_AUDIT_DSN: mysql+asyncmy://root:root@mysql:3306/agentic_rag?charset=utf8mb4
      AGENTIC_RAG_REDIS_URL: redis://redis:6379/0
      AGENTIC_RAG_TRACE_RAG_BASE_URL: http://rag-service:8000
      AGENTIC_RAG_DOC_RAG_BASE_URL: http://doc-rag-service:8000
    ports: ["8005:8000"]
```

- MVP 用 `docker-compose` 本地起 MySQL + Redis + agentic-rag-service，验证"检索请求 -> 单跳/多跳 -> 证据收口"闭环。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `agentic_query_total` | 检索请求次数（按 `retrieval_path`） |
| `agentic_hop_total` | 工具调用次数分布（Histogram，观察单跳/多跳比例） |
| `agentic_tool_call_total` | 工具调用（按 tool / status） |
| `agentic_stop_reason_total` | 停止原因分布（enough / no_new_evidence / budget / conflict） |
| `agentic_query_latency_seconds` | 检索延迟（Histogram） |
| `agentic_cache_hit_total` | 缓存命中 |
| `agentic_low_confidence_total` | 低置信度转人工 |
| `agentic_dependency_unreachable_total` | 下游不可达降级 |

### 10.2 trace 串联

- 埋点入口用 AgentScope `TracingMiddleware`（不自建 obs 层），`trace_id` 从检索入口生成。
- 调制造追溯/文档检索时透传 `traceparent`，使检索入口 → 两个能力 → 下游 REST 的 trace 串联。
- 链路查看走 OpenTelemetry + Tempo/Jaeger；过程回放用 `reply_stream`；**不选 AgentScope Studio**（已归档）。
- `AnswerAudit` 记录问题 / 检索路径 / 工具链 / 停止原因 / 证据摘要 / `trace_id`，`/rag/explain` 可回溯。

### 10.3 兜底

- **下游不可达**：制造追溯或文档检索故障 → 降级"该能力暂时不可用"或返回已得证据 + 转人工。
- **低置信度**：`confidence < 0.6` → `needs_human_review=True`。
- **证据不足**：停止时槽位不齐 → `detail["missing"]` 列缺口，不编造。
- **预算/超时**：返回已得证据 + `stop_reason` + 转人工提示。
- **冲突**：版本冲突 / 权限拒绝 → 停止并显式说明，不跨版本硬答。

---

## 11. 实现步骤

### 阶段一：骨架与单跳（2 周）

1. 搭 `agentic_rag_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 实现 `ToolRegistry` + `ToolExecutor` + `ReadOnlyToolGate` 启动断言（§7.2、§9.4）。
3. 实现 `plan_start` 与两条单跳路径，跑通单跳直答。

### 阶段二：多跳编排（2-3 周）

4. 实现 `RetrievalAgentFactory` + 多跳 ReAct（§9.2），限制工具面与预算。
5. 实现停止条件（证据充分 / 无新增 / 预算 / 冲突）（§4.3）。
6. 实现 ACL 客户端（制造追溯 / 文档检索）（§6.2）。

### 阶段三：统一输出与可观测（1-2 周）

7. 实现 `RagAnswer` + `AnswerAudit`（§5.2、§10.2）。
8. 实现 `/rag/explain/{audit_id}` 回溯端点（§9.5）。
9. 接 OpenTelemetry + prometheus 指标；单跳/多跳比例与停止原因看板。

### 阶段四：加固与评测（1 周）

10. 沉淀评测集（典型检索问题 + 预期证据/停止原因），回归多跳准确率与证据召回率。评测栈自建（自研 `EvalRunner` + 断言/指标/judge 三层 + pass^k + CI 门禁），judge 用独立包 `py-openjudge`--AgentScope 2.0 **无内生评测**。
11. 兜底链路全测（下游不可达 / 低置信度 / 证据不足 / 预算超限 / 版本冲突）。
12. 确认 🔴 决策点（检索超时阈值、多跳上限、证据条数上限）。

> **前提**：必须在文档检索与制造追溯成型后启动；没有两个工具，编排就是空转。

---

## 12. 约束落地检查清单

- [ ] 无意图分类器、无 `IntentCategory`、无跨能力委托客户端（§1.3）。
- [ ] 所有注册工具 `read_only=True`，`ReadOnlyToolGate` 启动断言生效。
- [ ] `assert_dependencies_reachable` 启动断言：两个检索能力不可达时拒绝启动。
- [ ] 不调过点引擎放行/拦截 API。
- [ ] 检索工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。
- [ ] 工具调用前按 `TenantContext` 权限过滤，拒绝记录可观测。
- [ ] 应用层预算（工具次数 / 时长 / 证据量）硬约束多跳规模。
- [ ] 所有路径收敛到 `RagAnswer`，带 `retrieval_path` + `tool_chain` + `stop_reason` + `sources`。
- [ ] `trace_id` 从检索入口发起，透传到两个检索能力。
- [ ] 下游不可达 / 低置信度 / 证据不足 / 预算超限 / 版本冲突 -> 显式降级，不硬答。
- [ ] 答案带 `disclaimer`：辅助信息，最终处置需工程师确认。
- [ ] 不对外提供 `/agent/chat`；统一入口在网关。

---

## 13. 面试防守 Q&A

**Q：Agentic RAG 和入口网关怎么分工？**
A：网关回答"这问题该找检索、诊断还是草稿"，Agentic RAG 回答"这次检索还要再查什么"。意图路由与跨能力委托在网关（[Agent入口网关-意图路由.md](../AGENT服务/Agent入口网关-意图路由.md)）；本模块只有检索工具选择。依赖是单向的：网关 → RAG / agent-service，不成环。

**Q：为什么 Agentic RAG 不做意图路由？**
A：定位不允许。RAG 引入路线 §3 说 Agentic 不是所有请求必经的入口层，只决定"什么时候再查一次、查什么、什么时候停止"。把分诊放进来，rag-service 就成入口层，还要反向依赖 agent-service 才能委托诊断，两个服务互相依赖；而且入口话术变化快、检索契约变化慢，节奏不同。

**Q：多跳会不会不可控？**
A：三层约束：工具面只有两个只读工具；预算硬限制（≤2 次调用、≤20 条证据、≤15s）；停止条件明确（证据充分 / 无新增 / 预算 / 冲突）。另外单跳优先，多数问题根本不进 ReAct。

**Q：和普通 RAG 的区别？**
A：普通 RAG 是"一次检索 + 一次生成"；Agentic RAG 多的是"证据不够时再查一次"的判断与组合，但动作空间被限制在只读白名单和预算内，不是自由探索。

**Q：为什么是单跳优先？**
A：成本和延迟。单跳不调 LLM，工具参数由请求字段直接给出；多跳才进 ReAct。多数现场问题是单跳能答的，没必要为它们付多跳成本。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地。重点是四条判断：① Agentic RAG 只做检索编排，不做入口分诊与跨能力委托；② 单跳优先、多跳有预算与停止条件；③ 只读、版本锚点、权限、不进过点四条红线全继承；④ trace 从检索入口发起，可回溯检索路径与停止原因。

---

## 14. 一句话定位

"Agentic RAG 是 rag-service 的检索内编排：给定问题、种子与版本锚点，在文档检索与制造追溯两个只读工具之间判断是否追加检索、查什么、何时停止，最后收敛成带来源引用的 `RagAnswer`。单跳优先、多跳受 2 次工具调用预算约束；不做意图分诊、不做跨能力委托；全程只读、版本锚定、不进过点主事务。"

---

## 15. 契约对齐与待办

| 契约 | 状态 | 待办 |
|------|------|------|
| 制造追溯 `/rag/trace/expand` | 🔴 待对齐 | 确认入参（seed/as_of/version/version_kind）与返回的 `evidence_set_id` |
| 文档检索 `/rag/docs/search` | 🔴 待对齐 | 确认 search 端点入参与版本过滤语义 |
| 入口网关 `RETRIEVAL_REQUEST` -> `RetrievalRequest` | 🔴 待对齐 | 与网关确认种子/版本锚点透传字段（[Agent入口网关-意图路由.md](../AGENT服务/Agent入口网关-意图路由.md) §6） |
| 多跳上限与超时阈值 | ⏳ 待压测 | 默认 ≤2 次 / ≤15s，按现场数据调整 |
| 证据条数上限 | ⏳ 待压测 | 默认 ≤20，按 token 成本调整 |
