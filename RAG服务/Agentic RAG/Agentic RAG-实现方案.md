# Agentic RAG 实现方案（Python 技术栈：3 意图路由 MVP）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.5 路线 E（Agentic RAG）的**实现层落地**，与 [Agentic RAG-详细设计.md](./Agentic RAG-详细设计.md) 的关系：
> - **详细设计**是全意图收口的**设计层**（广）--意图路由、工具与子 Agent 委托、统一输出的全景；
> - **本文**是 3 类意图（追溯事实 + 根因诊断 + 文档查询）的**实现层**（深）--把详细设计的骨架补全到可落地的 MVP，新增**依赖清单、ACL 只读 REST 契约、LangGraph 路由图代码、Docker 部署、测试策略**等实现层内容，并对个别委托契约按 L1/L2 落地口径细化（如 L1 委托超时与 trace 透传，§4.3 🔴）。
> 其余意图（草稿生成 L2 委托）按 §11 相同范式扩展，MVP 不展开。
>
> **技术栈**：Python（FastAPI + LangGraph + Pydantic）。Agent 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 REST 解耦，互不侵入。
> **口径纪律**：Agentic RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Agentic RAG"。

---

## 1. 设计目标与边界

### 1.1 定位（E = AGENT 路线 L0 收口型）

[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §2.1 明确 **L0 收口型问答 Agent = RAG 路线 E**。E 是统一入口收口层，不造新能力，全部调 A/B 工具 + 委托 L1/L2 子 Agent。E 是最后一步，等 A/B + L1-L3 成型后收口（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。

### 1.2 目标（4 意图 MVP）

把 A/B 检索 + L1 诊断收口成统一入口，跑通"NL -> 意图路由 -> 工具/委托 -> 统一答案"闭环。**MVP 范围**：聚焦 3 类高频意图：

| 意图 | 路由到 | 形态 | MVP 典型问题 |
|------|--------|------|-------------|
| **`TRACE_FACT`**（追溯事实） | A 工具 `query_traceability_graph` | 工具直答 | "SN-001 过了哪几站""这批用了哪批锡膏" |
| **`ROOT_CAUSE`**（根因诊断） | L1 子 Agent | 委托 | "SN-001 焊接不良根因" |
| **`DOC_LOOKUP`**（文档查询） | B 工具 `search_docs` | 工具直答 | "SPI 报警怎么处置" |

> 其余意图（`DRAFT_REQUEST` 委托 L2、轻量组合）按 §11 相同范式扩展，MVP 不展开。

### 1.3 硬边界（一开口就要讲）

| 边界 | 说明 | 落地（MVP 具体动作） |
|------|------|----------------------|
| **收口不造新能力** | E 只做路由 + 委托，不重建检索/推理 | E 无向量库/图谱，全部调 A/B + 委托 L1 |
| **只读（继承 L1）** | E 全程只读，无写工具 | `ReadOnlyToolGate` 启动断言；写工具不注册（§9.4） |
| **不进过点主事务** | E 不调过点放行/拦截 API | 过点 toolset 不暴露 `pass/judge`（继承 L1 §1.2） |
| **版本一致性** | 查工艺带版本锚点（`version`+`version_kind`） | A/B 工具强制版本锚点（`version`+`version_kind`）入参，ACL 校验（继承 A/B/L1） |
| **权限隔离** | 工具调用前按 `tenant_scope` 过滤 | 路由 + 工具调用都带 `TenantContext`（继承 L1 §4.3） |
| **委托而非重复** | 深度诊断委托 L1，E 不多步推理 | E `recursion_limit=6`（轻量组合），深度场景委托 L1 |
| **可观测兜底** | 答案带工具链 + 来源 + 置信度 | `AnswerAudit` 落库 + `/explain` 回溯；低置信度转人工 |
| **依赖可达性** | E 收口前提：A/B + L1 必须可达 | `assert_dependencies_reachable` 启动断言（§9.4） |

### 1.4 与详细设计、A/B、L1/L2/L3 的关系

- **与详细设计**：详细设计给全意图收口全景；本文把 4 类高频意图的路由图、ACL、代码补全到可落地。
- **与 A/B**：E 封装 A(`query_traceability_graph`)/B(`search_docs`) 为工具调用，不重建检索能力。
- **与 L1**：深度诊断委托 L1 子 Agent（调 `/agent/diagnose`），E 不自己多步推理。
- **与 L2/L3**：MVP 不含 L2 草稿委托 / L3 编排（§11 扩展）。

### 1.5 与 Java 技术栈的关系

- E 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只通过 REST 调 A/B RAG 服务 + L1 Agent 服务的只读接口。
- 跨语言物理边界天然强制只读（与 L1 同构，[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §1.3）。
- E 是纯编排层，复用 A/B + L1 的既有契约，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 版本 | 选型理由 |
|------|------|------|---------|
| 语言 | Python | 3.11+ | 与 L1/L2/L3 同栈，复用 LangGraph + LLM 抽象 |
| Web 框架 | **FastAPI** | 0.110+ | 异步、原生 OpenAPI，统一问答入口 |
| Agent 编排 | **LangGraph** | 0.1+ | 轻量 StateGraph 做路由 -> 工具/委托 -> 收口；与 L1 同框架 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 0.2+ | 模型可插拔，与 L1 一致 |
| 数据校验 | **Pydantic** | v2 | 意图/工具入参/统一答案 schema 即类型 |
| HTTP 客户端 | **httpx** | 0.27+（异步） | 调 A/B RAG + L1 Agent 只读 REST |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 2.0+ | 会话、路由 trace、答案审计 |
| 缓存 | **redis-py (async)** | 5.0+ | 相同问题短缓存 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | - | trace 从 E 发起串联 |
| 配置 | pydantic-settings | 2.0+ | 环境变量统一管理 |
| 部署 | 独立微服务 `agent-gateway-service`（uvicorn + gunicorn worker） | - | K8s 部署；MVP 可 docker-compose 本地起 |

### 2.2 为什么 E 用 LangGraph 轻量图

- E 的核心是"路由 + 轻量组合"：多数问题单工具即答。LangGraph `StateGraph` 把"路由节点 -> 工具/委托节点 -> 收口节点"做成显式图，支持条件路由。
- 与 L1 同框架：可复用 trace/checkpointer 机制，委托 L1 时 `traceparent` 透传（§4.3）。
- `recursion_limit=6` 对应"E 不做深度推理"硬上限--深度场景委托 L1。

### 2.3 为什么 E 不重建检索能力

- A/B 已建好检索能力，L1 已建好诊断能力。E 重建等于重复造轮子 + 多份事实源。
- E 的价值在"路由判断"，把问题送到对的能力。委托而非重复：深度诊断委托 L1，E 不自己推理。

### 2.4 部署形态

- E 是统一入口，部署在办公网/车间网均可访问的位置。LLM 视安全策略二选一。
- E 依赖 A/B + L1 可达性：下游故障时降级（§10.3），不硬答。
- MVP 用 `docker-compose` 本地起 MySQL + Redis + agent-gateway-service（§9.9）。

### 2.5 依赖清单（pyproject.toml 片段）

```toml
[project]
name = "agent-gateway-service"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "gunicorn>=21.2",
  "pydantic>=2.6",
  "pydantic-settings>=2.2",
  "langgraph>=0.1",
  "langchain-core>=0.2",
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
│ agent-gateway-service（统一入口，Python + FastAPI + LangGraph）    │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ GatewayService                            │  │
│  │ /agent/chat  │  │  构建路由图 + 驱动                         │  │
│  └──────────────┘  └────────────┬─────────────────────────────┘  │
│                                 │                                  │
│                      ┌──────────▼──────────┐                       │
│                      │ LangGraph 路由图     │                       │
│                      │ router->tool/delegate│                       │
│                      └──────────┬──────────┘                       │
│                                 │                                  │
│         ┌───────────────────────┼───────────────────────┐          │
│         ▼                       ▼                       ▼          │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐    │
│  │ IntentRouter │    │ ToolExecutor     │    │ SubAgentDelegator │ │
│  │ NL -> 3 意图  │    │ A/B 工具调用    │    │ 委托 L1 子Agent   │ │
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
  │ A 追溯RAG │  │ B 文档RAG │    │ L1 诊断   │
  │ /rag/trace│  │ /rag/docs │    │ /agent/   │
  └──────────┘  └──────────┘    │ diagnose  │
                                └──────────┘
```

### 3.1 关键设计决策

- **路由即核心**：`IntentRouter` 规则优先 + LLM 兜底，路由准确率是健康度核心。
- **工具与委托分离**：`ToolExecutor`（A/B 单步直答）与 `SubAgentDelegator`（委托 L1）解耦（SRP）。
- **统一输出**：所有路径收敛到 `AgentAnswer`。
- **trace 从 E 发起**：`trace_id` 从 E 生成，透传到 L1/A/B。

---

## 4. 意图路由与子 Agent 委托

### 4.1 意图分类（MVP 3 类）

| 意图 | 路由到 | 形态 |
|------|--------|------|
| `TRACE_FACT` | A 工具 `query_traceability_graph` | 工具直答 |
| `ROOT_CAUSE` | L1 子 Agent | 委托 |
| `DOC_LOOKUP` | B 工具 `search_docs` | 工具直答 |

> 全意图分类见 [详细设计](./Agentic RAG-详细设计.md) §4.1。

### 4.2 路由规则（规则优先 + LLM 兜底）

```text
IntentRouter.classify(question)
   │
   ├─ 1. 规则优先
   │     ├─ 含"根因/5M1E/为什么不良" -> ROOT_CAUSE -> 委托 L1
   │     ├─ 含"怎么处置/SOP/怎么修" -> DOC_LOOKUP -> B 工具
   │     └─ 含"过了哪几站/用了哪批/位置" -> TRACE_FACT -> A 工具
   ├─ 2. LLM 兜底（with_structured_output(IntentCategory) 强制枚举）
   └─ 3. 仍不确定 -> UNKNOWN -> 转人工
```

- 规则优先降低对模型依赖；LLM 兜底强约束 Enum；UNKNOWN 转人工不硬答。

### 4.3 L1 委托的 trace 透传与超时

> 🔴 **契约待对齐：L1 委托的 trace 透传**。MVP 假设 L1 的 `/agent/diagnose` 接受 `traceparent` header 并透传到其工具调用（L1 §8.2 已声明 OpenTelemetry 透传 `traceparent` 到下游 Java 服务）。E 委托 L1 时注入 `traceparent`，使 E -> L1 -> A/B/各上下文 REST 的 trace 全链路串联。待与 L1 实现确认 `traceparent` header 的接收与透传。MVP 兜底：若 L1 未透传，E 侧仍记录委托的 span，但 L1 内部 trace 断裂（降级可观测）。
>
> **超时**：L1 整会话 ≤60s（[L1](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §5.1），E 委托超时设 60s（略留余量到 70s 整问答超时）。L1 超时返回 `DiagnosisReport.partial`，E 据此转人工。

---

## 5. 工具注册与统一答案

### 5.1 工具注册（MVP A/B 两工具，只读白名单）

| 工具名 | 封装的路线 | 入参 | 版本校验 |
|--------|-----------|------|---------|
| `query_traceability_graph` | A 追溯型 | seed, as_of, version, version_kind | 历史回放带版本锚点 |
| `search_docs` | B 文档型 | query, version, version_kind | 强制版本锚点过滤 |

```python
class ToolDescriptor(BaseModel):
    name: str
    description: str
    route: str                        # "A" / "B"
    read_only: bool                   # E 必须 True
    args_schema: type[BaseModel]
    required_tenant_scopes: list[str]
```

- `read_only=False` 启动时拒绝注册（继承 L1 `ReadOnlyToolGate`）。
- 工具调用前按 `TenantContext` 权限过滤（继承 L1 §4.3）。
- A/B 工具强制版本锚点（`version`+`version_kind`）入参（继承 A/B/L1 版本一致性）。

### 5.2 统一答案（AgentAnswer）

```python
class AnswerSource(BaseModel):
    source_type: str          # "trace_node" / "sop_doc" / "l1_hypothesis"
    ref: str                  # "node_id=..." / "SOP:WELD-014@v3" / "audit_id=..."
    route: str                # "A" / "B" / "L1"

class AgentAnswer(BaseModel):
    question: str
    intent: str               # 命中的 IntentCategory
    route_taken: str          # "A" / "L1" / "B"
    summary: str
    detail: dict              # 路线相关结构化详情
    sources: list[AnswerSource]
    confidence: float
    tool_chain: list[str]     # ["query_traceability_graph"] 或 ["L1:diagnose"]
    trace_id: str
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"
```

- `route_taken` + `tool_chain` 透明，收口入口路由可观测。
- `sources` 强制引用（SOP 带版本、节点 ID、L1 假设证据）。
- `disclaimer` 不可省。

### 5.3 红线继承

E 继承 L1 全部红线：① 只读闸（`ReadOnlyToolGate`）；② 版本闸（A/B 工具强制版本锚点 `version`+`version_kind`）；③ 权限闸（`TenantContext` 前置过滤）；④ 不进过点主事务闸（过点 toolset 不暴露 `pass/judge`）。

---

## 6. 实现方案

### 6.1 网关编排服务（GatewayService）

```python
class GatewayService:
    def __init__(
        self,
        graph_builder: RouteGraphBuilder,
        intent_router: IntentRouter,
        tool_executor: ToolExecutor,
        delegator: SubAgentDelegator,
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
        # 2. 意图路由
        intent = await self._intent_router.classify(request.question)
        self._metrics.intent.inc(intent.value)
        # 3. 驱动路由图
        graph = self._graph_builder.build(intent, tenant)
        try:
            final_state = await asyncio.wait_for(
                graph.ainvoke(
                    {"question": request.question, "intent": intent, "tenant": tenant},
                    config={"recursion_limit": 6, "configurable": {"thread_id": request.session_id}},
                ),
                timeout=70.0,
            )
        except (GraphRecursionError, asyncio.TimeoutError) as e:
            self._metrics.fallback.inc("timeout_or_recursion")
            return self._fallback(request, intent, f"问答未完成，已转人工: {e}")
        # 4. 收敛统一答案
        answer = self._build_answer(request, intent, final_state)
        # 5. 审计 + 缓存
        await self._audit_repo.record(request, intent, answer)
        await self._cache.set(request, tenant, answer)
        return answer
```

- 编排与路由/执行/委托分离（SRP）；超时/recursion 降级转人工。

### 6.2 ACL 防腐层（A/B + L1 只读 REST 契约）

| 协作方 | 只读 REST | 用途 | 超时 |
|--------|----------|------|------|
| A 追溯型 RAG | `POST /rag/trace/expand` | 追溯子图 | 5s |
| B 文档型 RAG | `GET /rag/docs/search` | SOP 片段 | 2s |
| L1 诊断 Agent | `POST /agent/diagnose` | 深度诊断委托 | 60s（🔴 traceparent 透传 §4.3） |

```python
class TraceRagAclClient:
    """A 追溯型 RAG 只读 ACL。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def query_traceability_graph(
        self, seed: dict, as_of: datetime, version: str, version_kind: str, tenant: TenantContext
    ) -> TraceSubgraphView:
        resp = await self._http.post(
            "/rag/trace/expand",
            json={"seed": seed, "as_of": as_of.isoformat(), "version": version, "version_kind": version_kind},
            headers=tenant.headers(), timeout=5.0,
        )
        resp.raise_for_status()
        return TraceSubgraphMapper.to_view(resp.json())

    async def ping(self) -> bool:
        try:
            r = await self._http.get("/health", timeout=1.0)
            return r.status_code == 200
        except Exception:
            return False


class L1DelegationClient:
    """L1 诊断子 Agent 委托 ACL。🔴 traceparent 透传（§4.3）。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def delegate(
        self, question: str, tenant: TenantContext, traceparent: str
    ) -> L1ReportView:
        resp = await self._http.post(
            "/agent/diagnose", json={"question": question},
            headers={**tenant.headers(), "traceparent": traceparent},  # 透传 trace
            timeout=60.0,
        )
        resp.raise_for_status()
        return L1ReportMapper.to_view(resp.json())

    async def ping(self) -> bool:
        try:
            r = await self._http.get("/health", timeout=1.0)
            return r.status_code == 200
        except Exception:
            return False
```

- 外部 DTO 不进 E 核心，只暴露 `TraceSubgraphView`/`L1ReportView`（ACL 约束）。
- 所有 ACL 客户端带 `ping()` 供启动断言（§9.4）。

### 6.3 版本一致性保证

继承 A/B/L1：A/B 工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。E 不另搞版本管理。

---

## 7. 实现方案（路由图与执行）

### 7.1 LangGraph 路由图

```text
[router_node] 意图分类（已在 GatewayService 调 IntentRouter，节点透传 intent）
      │
      ├─ TRACE_FACT/DOC_LOOKUP ──▶ [tool_node] 调 A/B ──▶ [converge_node]
      ├─ ROOT_CAUSE ──▶ [delegate_node] 委托 L1 ──▶ [converge_node]
      └─ UNKNOWN ──▶ [converge_node] 转人工兜底
```

- `recursion_limit=6`：E 最多轻量组合 1-2 工具，深度场景委托 L1。
- 详见 §9.2 代码。

### 7.2 工具执行器（ToolExecutor）

```python
class ToolExecutor:
    """调 A/B 工具，权限校验 + trace。继承 L1 ToolNode 模式。"""

    def __init__(self, registry: ToolRegistry, trace_repo: RouteTraceRepo, metrics: MetricsCollector) -> None:
        self._registry = registry; self._trace_repo = trace_repo; self._metrics = metrics

    async def __call__(self, state: AgentState) -> AgentState:
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
            IntentCategory.TRACE_FACT: "query_traceability_graph",
            IntentCategory.DOC_LOOKUP: "search_docs",
        }.get(intent, "")
```

### 7.3 子 Agent 委托器（SubAgentDelegator）

```python
class SubAgentDelegator:
    """委托 L1 子 Agent，E 不自己多步推理。"""

    def __init__(self, l1_client: L1DelegationClient, trace_repo: RouteTraceRepo, metrics: MetricsCollector) -> None:
        self._l1 = l1_client; self._trace_repo = trace_repo; self._metrics = metrics

    async def __call__(self, state: AgentState) -> AgentState:
        tenant = state["tenant"]
        traceparent = state.get("traceparent", "")
        t0 = time.perf_counter()
        try:
            report = await self._l1.delegate(state["question"], tenant, traceparent)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await self._trace_repo.save_ok("L1:diagnose", report, latency_ms)
            state["tool_result"] = report
            state["tool_chain"] = ["L1:diagnose"]
            self._metrics.delegation_ok.inc("L1")
        except asyncio.TimeoutError:
            state["answer"] = self._fallback("L1 诊断超时，已转人工")
            self._metrics.delegation_timeout.inc("L1")
        except Exception as e:
            state["answer"] = self._fallback(f"L1 委托失败: {e}")
            self._metrics.delegation_error.inc("L1")
        return state
```

- 委托超时/失败降级转人工（继承 L1 §8.3）。

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
    domain/
      intent.py                # IntentCategory 枚举
      answer.py                # AgentAnswer / AnswerSource
      tool.py                  # ToolDescriptor / ToolRegistry / ReadOnlyToolGate
      tenant.py                # TenantContext
    infrastructure/
      ai/
        route_graph_builder.py # LangGraph 路由图工厂
        llm_factory.py
      acl/
        trace_rag.py           # A 追溯型
        doc_rag.py             # B 文档型
        l1_delegation.py       # L1 委托（🔴 traceparent 透传 §4.3）
      persistence/
        models.py              # answer_audit / route_trace
        audit_repo.py
      redis_/
        query_cache.py
      obs/
        tracing.py
        metrics.py
    config.py
    main.py                    # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
  Dockerfile
  docker-compose.yml
```

- `domain/tool.ReadOnlyToolGate` 启动断言（继承 L1）。
- `infrastructure/acl/` 防腐层，调 A/B + L1，外部 DTO 经 Mapper 转内部视图。

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

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def classify(self, question: str) -> IntentCategory:
        for keywords, intent in self.RULES:
            if any(k in question for k in keywords):
                return intent
        try:
            return await self._llm.with_structured_output(IntentCategory).ainvoke(
                f"判断以下问题属于哪个意图（枚举）：\n{question}"
            )
        except Exception:
            return IntentCategory.UNKNOWN
```

### 9.2 LangGraph 路由图

```python
class RouteGraphBuilder:
    """构建 E 的轻量路由图：router -> tool/delegate -> converge。"""

    def __init__(self, tool_executor: ToolExecutor, delegator: SubAgentDelegator) -> None:
        self._tool_executor = tool_executor
        self._delegator = delegator

    def build(self, intent: IntentCategory, tenant: TenantContext) -> CompiledGraph:
        graph = StateGraph(AgentState)
        graph.add_node("router", self._router_node)
        graph.add_node("tool", self._tool_executor)
        graph.add_node("delegate", self._delegator)
        graph.add_node("converge", self._converge_node)
        graph.set_entry_point("router")
        graph.add_conditional_edges(
            "router", lambda s: self._route_decision(s["intent"]),
            {"tool": "tool", "delegate": "delegate", "unknown": "converge"},
        )
        graph.add_edge("tool", "converge")
        graph.add_edge("delegate", "converge")
        graph.add_edge("converge", END)
        return graph.compile()

    async def _router_node(self, state: AgentState) -> AgentState:
        return state  # intent 已在 GatewayService 分类，节点透传

    def _route_decision(self, intent: IntentCategory) -> str:
        if intent == IntentCategory.ROOT_CAUSE:
            return "delegate"
        if intent == IntentCategory.UNKNOWN:
            return "unknown"
        return "tool"

    async def _converge_node(self, state: AgentState) -> AgentState:
        if "answer" not in state:
            state["answer"] = self._build_from_result(state)
        return state
```

### 9.3 工具执行器与委托器

见 §7.2、§7.3。

### 9.4 启动断言（只读 + 依赖可达性）

```python
class ReadOnlyToolGate(Exception):
    """启动时发现非只读工具，拒绝启动。"""
class DependencyUnreachableGate(Exception):
    """启动时发现 A/B + L1 依赖不可达，拒绝启动（E 收口前提）。"""

async def assert_dependencies_reachable(clients: dict[str, object]) -> None:
    """E 是收口入口，下游 A/B + L1 必须可达，否则退化为套壳。"""
    unreachable = []
    for name, client in clients.items():
        if not await client.ping():
            unreachable.append(name)
    if unreachable:
        raise DependencyUnreachableGate(
            f"依赖不可达: {unreachable}（E 收口要求 A/B + L1 成型）"
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.tool_registry
    # 1. 只读断言（继承 L1）
    registry.validate_on_startup()
    # 2. 依赖可达性断言（E 收口前提）
    await assert_dependencies_reachable(app.state.acl_clients)
    yield
```

- `assert_dependencies_reachable` 体现"E 是收口，依赖必须就绪"--E 不该在 A/B + L1 没就绪时启动。

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
    # A/B RAG + L1 Agent 只读 REST
    trace_rag_base_url: str = "http://rag-service:8000"
    doc_rag_base_url: str = "http://doc-rag-service:8000"
    l1_agent_base_url: str = "http://agent-service:8000"
    # LLM
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    # 路由图
    recursion_limit: int = 6
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
      GATEWAY_L1_AGENT_BASE_URL: http://agent-service:8000
    ports: ["8004:8000"]
```

- MVP 用 `docker-compose` 本地起 MySQL + Redis + agent-gateway-service，验证"统一入口 -> 路由 -> A/B/L1 -> 统一答案"闭环。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `gateway_chat_total` | 问答次数（按 intent label） |
| `gateway_route_accuracy` | 路由准确率（评测集/用户反馈，健康度核心） |
| `gateway_tool_call_total` | 工具调用次数（按 route label） |
| `gateway_delegation_total` | L1 委托次数（按 status label） |
| `gateway_chat_latency_seconds` | 整问答延迟（Histogram） |
| `gateway_cache_hit_total` | 查询缓存命中 |
| `gateway_low_confidence_total` | 低置信度转人工次数 |
| `gateway_unknown_intent_total` | 未识别意图转人工次数 |
| `gateway_dependency_unreachable_total` | 下游不可达降级次数 |

### 10.2 trace 串联

- `trace_id` 从 E 生成，委托 L1 时透传 `traceparent`（🔴 §4.3）。
- `AnswerAudit` 记录问题/意图/路由/工具链/答案/`trace_id`，`/explain` 可回溯。

### 10.3 兜底

- **路由失败**：`UNKNOWN` 意图 -> 转人工"该问题暂不支持"。
- **下游不可达**：A/B/L1 故障 -> 降级"该路线暂时不可用"或切换备选路线。
- **低置信度**：`confidence < 0.6` -> `needs_human_review=True`。
- **委托超时**：L1 超时 -> 降级"诊断未完成，已转人工"（继承 L1 §8.3）。
- **`recursion_limit`**：轻量组合超步数 -> 降级"建议改用 L1 深度诊断"或转人工。

---

## 11. 实现步骤

### 阶段一：骨架与意图路由（2 周）

1. 搭 `agent_gateway_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 实现 `IntentRouter`（规则优先 + LLM 兜底）（§9.1）。
3. 实现 LangGraph 轻量路由图（§9.2），单工具直答路径跑通。
4. 实现 `ReadOnlyToolGate` + `assert_dependencies_reachable` 启动断言（§9.4）。

### 阶段二：工具与委托（2-3 周）

5. 实现 `ToolExecutor` + `ToolRegistry`（A/B 工具封装）（§7.2）。
6. 实现 ACL 客户端（A/B RAG）（§6.2）。
7. 实现 `SubAgentDelegator`（委托 L1 + traceparent 透传）（§7.3、§4.3 🔴）。
8. 验证 E -> L1 -> A/各上下文 REST 的 trace 全链路串联。

### 阶段三：统一输出与可观测（1-2 周）

9. 实现 `AgentAnswer` 统一格式 + `AnswerAudit` 审计（§5.2）。
10. 实现 `/agent/explain` 回溯端点（§9.5）。
11. 接 OpenTelemetry + prometheus 指标（§10.1），路由准确率告警。

### 阶段四：加固、评测与试点（1 周）

12. 沉淀评测集（典型问题 + 预期路由/答案），回归路由准确率。
13. 兜底链路全测（UNKNOWN/下游不可达/低置信度/委托超时/recursion_limit）。
14. 灰度统一入口试点，收集"路由是否准、答案是否对"反馈。
15. 确认 🔴 决策点（§4.3 traceparent 透传、§11 L2/L3 纳入时机、路由规则覆盖）。

> **前提**：E 必须在 A/B + L1 成型后启动（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。E 是收口，不是起步。

---

## 12. 约束落地检查清单

- [ ] E 无自有检索/推理能力，全部调 A/B + 委托 L1（§1.3 收口不造新能力）。
- [ ] 所有注册工具 `read_only=True`，`ReadOnlyToolGate` 启动断言生效（继承 L1）。
- [ ] `assert_dependencies_reachable` 启动断言：A/B + L1 不可达时拒绝启动（E 收口前提）。
- [ ] E 不调过点引擎放行/拦截 API，
- [ ] A/B 工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承 A/B/L1 版本一致性）。
- [ ] 工具调用前按 `TenantContext` 权限过滤，拒绝记录可观测（继承 L1）。
- [ ] 深度诊断委托 L1，E 不自己多步推理；`recursion_limit=6` 限制轻量组合步数。
- [ ] 所有路径收敛到 `AgentAnswer` 统一格式，带 `route_taken` + `tool_chain` + `sources`。
- [ ] `trace_id` 从 E 发起，委托 L1 时透传 `traceparent`（🔴 §4.3），全链路可回溯。
- [ ] 路由失败/下游不可达/低置信度/委托超时 -> 转人工兜底，不硬答。
- [ ] 答案带 `disclaimer`：辅助信息，最终处置需工程师确认。
- [ ] E 在 A/B + L1 成型后启动，不提前做（避免套壳）。

---

## 13. 面试防守 Q&A

**Q：MVP 选了哪三类意图？为什么？**
A：选了追溯事实（`TRACE_FACT`->A 工具）、根因诊断（`ROOT_CAUSE`->委托 L1）、文档查询（`DOC_LOOKUP`->B 工具）三类。原因：① 这三类覆盖了 A/B + L1 的核心能力，验证"统一入口 -> 路由 -> 工具/委托 -> 统一答案"完整闭环；② 覆盖了两种路由形态--单步工具直答（A/B）与子 Agent 委托（L1），验证 E 的"广深分工"；③ 这三类是工程师最高频场景。其余意图（L2 草稿委托、轻量组合）按相同范式扩展。

**Q：E 和 L1 怎么分工？是不是重复了？**
A：不重复，广深分工。E 是"广而浅"的路由收口（意图分类 -> 选工具/子 Agent -> 轻量组合），L1 是"深而专"的追溯多步诊断（≤10 步 5M1E 推理）。简单追溯事实（"过了哪几站"）E 调 A 工具直答；深度根因诊断（"为什么不良"）E 委托 L1。E `recursion_limit=6` 只做轻量组合，深度多步委托 L1。这 reconcile 了 RAG 路线 §2.5"E 能多步推理"和 AGENT 路线 §2.1"L0 没有跨上下文多步推理"--E 的"多步"是轻量组合，深度推理委托 L1。

**Q：E 怎么判断该走哪条路线？**
A：意图路由规则优先 + LLM 兜底。高频问题关键词命中直定意图（"根因/5M1E"->L1，"怎么处置/SOP"->B，"过了哪几站"->A），命中不了才走 LLM 结构化输出分类（`IntentCategory` 固化为 Enum）。路由准确率是 E 的核心健康度指标。UNKNOWN 转人工不硬答。

**Q：委托 L1 时怎么保证 trace 不断？**
A：E 委托 L1 时注入 `traceparent` header，L1 接收后透传到其工具调用与下游 Java REST，使 E -> L1 -> A/各上下文 REST 的 trace 全链路串联（🔴 待与 L1 实现确认 `traceparent` 接收与透传，§4.3）。MVP 兜底：若 L1 未透传，E 侧仍记录委托 span，但 L1 内部 trace 断裂（降级可观测）。`AnswerAudit` 记录 `trace_id`，`/explain` 可回溯。

**Q：E 路由错了怎么办？**
A：三重兜底。一是路由准确率作为核心指标监控 + 评测集回归；二是 L1 委托有超时（≤60s）和置信度阈值，L1 发现问题不属于诊断范畴返回低置信度，E 据此转人工；三是 UNKNOWN 和路由失败都转人工不硬答。`AnswerAudit` 记录每次路由决策，工程师可回溯"为什么走了 L1"，反馈调优规则。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地，且 E 是三条路线里最后做的。重点是四条架构判断：① E = L0 收口型，不造新能力，全部调 A/B + 委托 L1；② 意图路由规则优先 + LLM 兜底；③ 广深分工--E 轻量组合（`recursion_limit=6`），深度委托 L1；④ 继承全部只读红线 + 依赖可达性启动断言。E 必须在 A/B + L1 成型后启动，否则退化为套壳。诚实 + 体现架构判断力，比硬吹"已上线 Agentic RAG"得分高。

---

## 14. 一句话定位

"Agentic RAG MVP 把 A/B 检索 + L1 诊断收口成统一入口--3 类意图（追溯事实/根因诊断/文档查询）规则优先 + LLM 兜底路由：单步意图调 A/B 工具直答，深度根因委托 L1 子 Agent（`traceparent` 透传串联 trace）。E 不造新能力，全部调 A/B + 委托 L1；广深分工--E 只做轻量组合（`recursion_limit=6`），跨上下文深度多步委托 L1；继承全部只读红线（`ReadOnlyToolGate`/版本/权限/不进过点主事务）+ 依赖可达性启动断言，所有路径收敛到带 `route_taken`+`tool_chain`+`sources` 的统一答案。E 是最后一步，等 A/B + L1 成型后收口。"

---

## 15. 与 A/B、L1/L2/L3 的契约对齐与待办

| 契约 | 状态 | 待办 |
|------|------|------|
| L1 `/agent/diagnose` 接受并透传 `traceparent` | 🔴 待对齐 | 与 L1 实现确认 trace 透传（§4.3） |
| A `/rag/trace/expand` 契约 | 🔴 待对齐 | 与追溯型 RAG 确认 expand 端点入参（seed/as_of/version/version_kind） |
| B `/rag/docs/search` 契约 | 🔴 待对齐 | 与文档型 RAG 确认 search 端点入参 |
| L2 草稿委托（`DRAFT_REQUEST`） | ⏳ §11 | L2 就绪后纳入委托 |
| 轻量组合（多工具 ReAct） | ⏳ §11 | `recursion_limit=6` 已预留，按场景启用 |
| L3 编排纳入 | ⏳ 远期 | L3 成型后作为子 Agent 委托 |
