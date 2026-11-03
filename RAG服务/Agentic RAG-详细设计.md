# Agentic RAG 详细设计（检索内多跳编排 + 证据收口）

> 本文是 [RAG服务引入路线.md](RAG服务引入路线.md) §3「Agentic 的正确定位」所述 **Agentic 能力**的落地展开，输出**技术栈、检索编排设计、工具注册、统一输出、包结构、关键代码骨架与约束落地**。
>
> **边界先行**：Agentic RAG **只做检索内编排**——在文档检索与制造追溯两个只读工具之间决定"是否追加检索、查什么、何时停止"。它**不做意图分诊**、**不做跨能力委托**。统一入口、意图路由与诊断/草稿委托归 [Agent入口网关-意图路由.md](../AGENT服务/Agent入口网关-意图路由.md)。
>
> **技术栈**：Python（FastAPI + AgentScope 2.0 + Pydantic）。与三大 MES 服务（Java/Spring）跨语言共存，通过 REST / Kafka 解耦，互不侵入。
> **底座口径**：与 [基于AgentScope2.0的架构设计.md](../基于AgentScope2.0的架构设计.md) §3.2 与附录速查表、[架构设计v2/00-架构决策记录ADR.md](../架构设计v2/00-架构决策记录ADR.md) ADR-01 对齐（`agentscope>=2.0.0`、Python 3.12+）--统一 `Agent`（内置 ReAct）+ `Toolkit` 承载检索内工具选择；`AgentState` + `SessionService`（Redis-backed；2.0.5 起旧名 `SessionManager` 已失效）承载跨请求会话。框架只决定"怎么实现"，不改变本文的只读、版本与证据约束。
> **口径纪律**：Agentic RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Agentic RAG"。

---

## 1. 设计目标与边界

### 1.1 定位：rag-service 的检索内编排能力

Agentic RAG 解决的是**一次检索请求内部**的问题：证据够不够、还要不要追加一次查询、追加查哪个上下文、什么时候停。它不解决"这个问题该由哪项能力回答"——那是入口网关的职责。

- **不造新检索能力**：文档检索（向量召回 + 元数据过滤 + 重排）与制造追溯（关系型读模型 + 固定关联计划）是两项既有能力，Agentic 只负责在它们之上做追加检索与证据组合。
- **按需启用**：简单问题一次工具调用即答，不进入多跳；只有证据缺口明确时才多查一步。
- **何时做**：等文档检索与制造追溯成型后再引入编排（[RAG服务引入路线.md](RAG服务引入路线.md) §5 第三阶段）。先有工具，后有编排。

### 1.2 目标

给定一次检索请求（自然语言问题 + 可选种子 + 版本锚点），输出**带来源引用的证据答案**：

| 形态 | 触发条件 | 典型问题 |
|------|---------|---------|
| **单跳文档检索** | 纯知识类问题，无制造对象 | "SPI 报警怎么处置""首件检验流程" |
| **单跳追溯** | 有明确种子（SN/批次/工单），只问事实链 | "SN-001 过了哪几站""这批用了哪批锡膏" |
| **多跳组合** | 需要"事实 + 当时版本 + 处置依据" | "这个批次当时用哪版工艺，对应处置要求是什么" |

- **单跳是默认**：多数问题一次调用即答，不付多跳成本。
- **多跳有界**：MVP 最多 2 次工具调用，由应用层独立计算次数、时长与证据量。
- **证据不足显式标注**：缺失关系不猜，`needs_human_review=True` 并说明缺什么。

> MVP 只接 `query_traceability` 与 `search_docs` 两个只读工具；[RAG服务引入路线.md](RAG服务引入路线.md) §3 列出的 `record_expand` / `version_resolve` 是后续扩展位，先由追溯工具的固定关联计划覆盖，不单开工具体。

### 1.3 硬边界

| 边界 | 说明 | 落地 |
|------|------|------|
| **不做意图分诊** | 不判断"该找检索还是诊断/草稿"，只判断"检索内还查不查" | 无 `IntentCategory`、无分类器；入口分派在网关 |
| **不做跨能力委托** | 不调诊断/草稿/编排能力，不持有其客户端 | ACL 只有制造追溯与文档检索两个只读客户端 |
| **只读** | 无写工具、不领用写令牌 | `ReadOnlyToolGate` 启动断言；写工具拒绝注册 |
| **不进过点主事务** | 不调过点引擎放行/拦截 API | 过点 toolset 不暴露 `pass/judge` 类工具 |
| **版本一致性** | 查工艺带 `version`+`version_kind`；文档检索带版本过滤 | 工具入参强制版本锚点，ACL 层校验 |
| **权限隔离** | 工具调用前按 `tenant_scope` 过滤 | `TenantContext` 前置过滤 |
| **查询有界** | 不允许无限展开 | 工具次数 ≤2、证据条数上限、总时长上限；超限即停 |
| **证据约束** | 制造事实必须来自记录，处置建议必须来自适用文档 | 统一 `sources` 强制引用；缺证据不编 |

### 1.4 与入口网关的分工

| 维度 | 入口网关（agent-gateway-service） | Agentic RAG（本文） |
|------|-----------------------------------|---------------------|
| 回答什么问题 | "这问题该找谁" | "这次检索还要再查什么" |
| 分类对象 | 能力（检索 / 诊断 / 草稿） | 工具（追溯 / 文档） |
| 执行形态 | 确定性 HTTP 分派 + 委托 | ReAct 工具选择 + 单/多跳 |
| 输出 | `AgentAnswer`（统一答案） | `RagAnswer`（证据答案） |

> 网关把 `RETRIEVAL_REQUEST` 交给本文；本文不感知用户在网关侧说的是"诊断"还是"检索"，也不回传意图。

### 1.5 与两项检索能力的关系

| rag-service 检索能力 | 本文如何使用 | 形态 |
|---------|-----------|------|
| **制造追溯** | 封装为 `query_traceability` 工具，按种子 + 固定关联计划取证据 | 工具 |
| **文档检索** | 封装为 `search_docs` 工具，按 query + 版本锚点召回 | 工具 |

> **向量库口径风险注**：向量召回由文档检索能力自持，本文只调 `search_docs`、不碰向量库。若文档检索侧经 AgentScope RAG 模块接入向量库，须记住该模块**没有 ChromaDB 适配器**（已核实可选后端为 Qdrant / MilvusLite / Elasticsearch / MongoDB），且 `rag/` 模块在 **2.0.0 曾被整体删除、2.0.5 起才回归**--必须 pin 到具体小版本并保留降级预案（证据见 ADR-01 的 RAG 模块行）。

### 1.6 与 Java 技术栈的关系

- 本文模块用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只通过 REST 调制造追溯与文档检索的只读接口。
- 跨语言物理边界天然强制只读：无法共享 Java 事务/内存，无法进过点主事务、无法旁路应用服务写路径。
- 复用两项检索能力的既有契约，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.12+ | 与 agent-service 同栈，复用 AgentScope + Model 抽象 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI，提供检索编排接口 |
| Agent 底座 | **AgentScope 2.0**（`agentscope>=2.0.0`） | 统一 `Agent` 类（内置 ReAct）+ `Toolkit` 做"工具选择 -> 观察 -> 追加/停止" |
| LLM 抽象 | AgentScope `Model` | provider 可插拔（DashScope / Ollama / OpenAI 兼容） |
| 数据校验 | **Pydantic v2** | 工具入参 / `RagAnswer` / 状态 DTO 的 schema 即类型 |
| HTTP 客户端 | **httpx**（异步） | 调制造追溯与文档检索只读 REST |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 检索 trace、答案审计 |
| 会话状态 | AgentScope `AgentState` + `SessionService`（Redis-backed） | 多跳过程与跨请求续跑；⚠️ 旧名 `SessionManager` 自 2.0.5 起失效 |
| 缓存 | **redis-py (async)** | 相同检索请求短缓存 |
| 可观测 | AgentScope `TracingMiddleware` + **OpenTelemetry Python** + **Tempo/Jaeger** + `prometheus-client` | trace 从检索入口发起；过程回放走 `reply_stream`。⚠️ 不选已归档的 AgentScope Studio |
| 编排组合 | **应用层 Python**：顺序 `await` 串联 + `asyncio.gather` 并发独立 `Agent.reply()` | 2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 已删除；编排代码落在 `flows/` |
| 评测 | 自研 `EvalRunner` + 独立包 `py-openjudge` | 2.0 已删除 `agentscope.evaluate` 且未回归，评测必须自建 |
| 配置 | pydantic-settings | 环境变量统一管理 |
| 部署 | rag-service 内的 `agentic` 模块（可独立进程 `agentic-rag-service`） | 归属 rag-service 能力边界，不对外承担统一入口 |

### 2.2 为什么用 AgentScope 轻量编排而非固定查询计划

- **固定计划覆盖不了现场**：文档与追溯的组合顺序会随证据缺口变化（先追溯拿到工艺版本，才知道该查哪版 SOP），需要模型在只读白名单内选择下一步。
- **但只给一个很小的动作空间**：`Toolkit` 里只有两个只读工具，预算 1-2 次调用，模型不能自由探索任意关系。
- **2.0 没有 Pipeline/Handoffs**：`agentscope/pipeline`、`agentscope/msghub` 自 2.0.0 起已删除；顺序组合用应用层 `await`，确需并行用 `asyncio.gather` 并发各自独立的 `Agent.reply()`，结论在应用层显式合并。没有共享消息上下文原语，各分支只读同一份 `evidence_set_id`。

### 2.3 为什么不做意图分诊、不做跨能力委托

- **定位**：这两件事需要判断"该找哪项能力"，是跨能力职责；放在检索模块会让 rag-service 变成入口层（[RAG服务引入路线.md](RAG服务引入路线.md) §3 明确 Agentic 不是所有请求必经的入口）。
- **依赖方向**：若本文委托诊断/草稿，rag-service 会反向依赖 agent-service；而入口网关又依赖 rag-service，形成环。把委托放在网关，依赖保持单向。
- **变化频率**：入口分诊随渠道、话术、权限策略变化；检索编排随数据模型与证据契约变化。两者节奏不同，不应绑在一个服务里。

### 2.4 部署形态（车间网隔离）

- 作为 rag-service 的 `agentic` 模块部署（可独立进程/独立扩缩容），只暴露检索编排接口 `/rag/agentic/query` 与回溯接口 `/rag/explain/{audit_id}`。
- 不对外提供 `/agent/chat`：统一问答入口在网关。
- LLM 视安全策略二选一（云端 API 或本地模型），`Model` 抽象保证切换零代码改动。
- 依赖制造追溯与文档检索可达；不可达时启动断言失败（§9.4）。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ agentic-rag-service（rag-service 的检索编排模块，FastAPI + AgentScope）│
│                                                                    │
│  ┌──────────────────┐  ┌────────────────────────────────────────┐ │
│  │ POST             │─▶│ AgenticRagService                      │ │
│  │ /rag/agentic/query│  │  起点判定 -> 单跳 / 多跳 -> 证据收口   │ │
│  └──────────────────┘  └───────────────┬────────────────────────┘ │
│                                        │                          │
│                      ┌─────────────────▼──────────────────┐       │
│                      │ 检索编排（flows/retrieval_flow.py） │       │
│                      │ 单跳直调 / Agent ReAct 追加检索      │       │
│                      └─────────────────┬──────────────────┘       │
│                                        │                          │
│                 ┌──────────────────────┴───────────────┐          │
│                 ▼                                      ▼          │
│        ┌──────────────────┐                   ┌──────────────────┐ │
│        │ ToolExecutor     │                   │ 停止条件判定      │ │
│        │ 权限/版本/trace  │                   │ 证据充分/无新增/  │ │
│        └────────┬─────────┘                   │ 预算耗尽/冲突     │ │
│                 │                             └──────────────────┘ │
│         ┌───────▼─────────┐                                        │
│         │ ToolRegistry    │  (只读白名单: query_traceability / search_docs)
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

- **编排即工具选择**：没有意图分类；编排只回答"还查不查、查哪个"。
- **单跳优先**：能用一次调用答的，不进入 ReAct，省成本也省延迟。
- **预算硬约束**：工具次数、时长、证据量由应用层独立计算，超限即停，不依赖框架参数。
- **统一输出**：单跳与多跳都收敛到 `RagAnswer`，网关与评测侧按结构解析。
- **trace 从检索入口发起**：`trace_id` 在本模块生成，透传到两个检索工具。

---

## 4. 检索编排设计

### 4.1 起点判定（单跳 vs 多跳）

判定只看请求形态，不看"用户想问哪类能力"：

```text
plan_start(request)
   ├─ 有种子(seed) 且 问题不含文档类词(处置/SOP/流程/标准/怎么修)
   │     -> 单跳：query_traceability
   ├─ 无种子 且 问题不含追溯类词(过点/物料/批次/设备/人员/工单)
   │     -> 单跳：search_docs
   ├─ 种子缺失但问题明确指向制造对象
   │     -> 先 search_docs 解析线索，再决定是否追溯（多跳）
   └─ 其余 -> 多跳：Agent 在 Toolkit 内选择（≤2 次调用）
```

- 这是**检索起点启发式**，不是意图分类：判错只影响"多查一次"或"少查一次"，不影响能力边界，也不改变网关的分派结果。
- 单跳路径不调用 LLM（工具调用的参数由请求字段直接给出），只有多跳才进入 Agent ReAct。

### 4.2 多跳工具选择（ReAct）

```text
Agent.reply(question + 已解析的 seed/version 锚点)
   │
   ├─ 选 query_traceability -> 得到过点/物料/设备/人员/工艺版本
   ├─ 选 search_docs      -> 按已解析版本召回 SOP/检验标准/历史 8D
   └─ 观察结果 -> 证据是否足够？
         ├─ 足够 -> 结束
         └─ 不足 -> 追加一次（受预算约束）
```

- **工具面极小**：只有两个只读工具，模型无法探索任意 SQL 或无界关联。
- **版本先解析后检索**：先拿到 `version`+`version_kind`，再查对应版本文档，避免"用当前版本回答历史问题"。
- **并行只用于互不依赖的补充查询**：`asyncio.gather` 并发两个独立 `Agent.reply()` 或两次工具调用，结论在应用层合并；不共享中间推理。

### 4.3 停止条件

满足任一即停：

1. **证据充分**：所需槽位齐（事实链、版本锚点、处置依据按请求类型定义）。
2. **无新增证据**：连续一次工具调用没有带来新证据。
3. **预算耗尽**：工具次数 / 时长 / 证据条数达到上限。
4. **冲突**：版本冲突、权限拒绝、租户范围不匹配。

停止时若证据不完整：`needs_human_review=True`，`detail["missing"]` 说明缺什么，不编造。

### 4.4 预算

| 预算项 | MVP 默认 | 说明 |
|--------|---------|------|
| 工具调用次数 | ≤2 | 单跳 1 次，多跳最多再追加 1 次 |
| 证据条数 | ≤20 | 超出截断并标注 |
| 单次检索请求时长 | ≤15s（🔴 待压测标定） | 超时返回已得证据 + 转人工提示 |

预算由应用层 `BudgetPolicy` 独立计算；超限抛 `BudgetExceededError`，**不依赖框架的"递归上限"参数**（AgentScope 无此单一参数）。
> [RAG服务引入路线.md](RAG服务引入路线.md) §3 给的编排步数上限是 6；MVP 按"单跳优先、最多追加一次"收紧到 2，证据变复杂时再按场景放宽（放宽必须同时评估工具次数、时长与 token）。

---

## 5. 工具注册（只读白名单）

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

- `read_only=False` 的工具在 `ToolRegistry` 启动时直接拒绝注册（继承诊断能力 `ReadOnlyToolGate`）。
- 工具调用前按 `TenantContext` 权限过滤；拒绝记录可观测。
- 工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。
- 工具均注册为 AgentScope `Tool` 并挂进同一个 `Toolkit`；外部检索服务可按需用 MCP 接入。

---

## 6. 统一输出（RagAnswer）

```python
class AnswerSource(BaseModel):
    """答案来源引用（可回溯）。"""
    source_type: str          # "trace_record" / "sop_doc"
    ref: str                  # "evidence_id=CheckpointRecord:xxx" / "SOP:WELD-014@v3"
    route: str                # "traceability" / "doc_search"

class RagAnswer(BaseModel):
    question: str
    retrieval_path: str          # "traceability" / "doc_search" / "doc_search+traceability" / "NONE"
    summary: str                 # 证据摘要（不编造）
    detail: dict                 # 结构化证据（事实链 / SOP 片段 / 缺失项）
    evidence_set_id: str         # 本次证据集标识，供回查
    sources: list[AnswerSource]  # 来源引用
    confidence: float
    tool_chain: list[str]        # ["query_traceability", "search_docs"]
    stop_reason: str             # "enough" / "no_new_evidence" / "budget" / "conflict"
    trace_id: str
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"
```

- **`retrieval_path` + `tool_chain` 透明**：能看出这次检索走了单跳还是多跳、调了哪些工具、为何停止。
- **`sources` 强制引用**：SOP 带版本、追溯证据带 ID；回查按 `evidence_set_id` 取证据集，不使用图节点引用。
- **`disclaimer` 不可省**：检索答案是辅助信息，最终处置需工程师确认。
- 本模块**不返回"意图"字段**：意图属于网关的 `AgentAnswer`，检索侧只描述检索过程。

---

## 7. 实现方案

### 7.1 检索编排服务（AgenticRagService）

```python
class BudgetExceededError(Exception):
    """应用层预算守卫（非框架内置异常）：工具次数 / 时长 / 证据量超限。"""

class AgenticRagService:
    def __init__(
        self,
        agent_factory: RetrievalAgentFactory,   # 装配统一 Agent + Toolkit（两个只读检索工具）
        budget_policy: BudgetPolicy,            # 应用层预算
        session_service: SessionService,        # AgentState 会话恢复（Redis-backed）
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
        start = plan_start(request)                                     # §4.1 起点判定

        try:
            if start.tool_name:                                         # 单跳：不调 LLM
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

- 编排与工具执行分离（SRP）；超时或预算超限返回已得证据 + 转人工提示。
- 单跳路径 0 次 LLM；多跳才进入 ReAct。

### 7.2 ACL 防腐层（只读 REST）

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

### 7.3 版本一致性

继承文档检索与制造追溯：工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。本文不另搞版本管理；历史问题必须按当时版本检索，不允许"用当前版本回答历史问题"。

---

## 8. 推荐包结构（Python src layout）

```text
agentic_rag_service/
  app/
    api/
      query_router.py          # POST /rag/agentic/query, GET /rag/explain/{audit_id}
      schemas.py
    application/
      agentic_rag_service.py   # 编排入口（起点判定 -> 单跳/多跳 -> 收口）
      retrieval_planner.py     # 起点判定 + 停止条件 + 预算计算
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
        model_factory.py            # AgentScope Model 接入（provider 可插拔）
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
        tracing.py                  # TracingMiddleware 接线（不自研 trace 层）
        metrics.py                  # 检索侧业务指标
    config.py
    main.py                         # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
```

- `infrastructure/acl/` 只含两个检索客户端——**没有诊断/草稿客户端**，这是"不做跨能力委托"的结构性保证。
- `domain/tool.ReadOnlyToolGate` 启动断言；`application/retrieval_planner.py` 承载起点判定与停止条件，便于单测。

---

## 9. 关键代码骨架

### 9.1 检索起点判定（RetrievalPlanner）

```python
class StartPlan(BaseModel):
    tool_name: str | None      # 非空 = 单跳直调；None = 进入多跳 ReAct
    reason: str

TRACE_HINTS = ("过点", "物料", "批次", "设备", "人员", "工单", "锡膏")
DOC_HINTS = ("处置", "SOP", "流程", "标准", "怎么修", "作业指导")

def plan_start(request: RetrievalRequest) -> StartPlan:
    """只看请求形态决定检索起点：不是意图分类，判错只影响多查/少查一次。"""
    q = request.question
    if request.seed and not any(h in q for h in DOC_HINTS):
        return StartPlan(tool_name="query_traceability", reason="有种子且无文档诉求")
    if not request.seed and not any(h in q for h in TRACE_HINTS):
        return StartPlan(tool_name="search_docs", reason="无种子且无追溯诉求")
    return StartPlan(tool_name=None, reason="证据缺口需多跳组合")
```

> 起点判定是**启发式**，不是分类模型：没有 LLM、没有 Enum、不产出"意图"；它只决定第一次调哪个工具。

### 9.2 检索 Agent 装配（RetrievalAgentFactory）

```python
class RetrievalAgentFactory:
    """装配检索内多跳 Agent：Toolkit 只有两个只读检索工具。"""

    def __init__(self, tool_registry: ToolRegistry, model: Model) -> None:
        self._registry = tool_registry
        self._model = model

    def build(self, tenant: TenantContext, budget: ToolCallBudget,
              state: RetrievalState | None = None) -> Agent:
        return Agent(
            name="agentic-rag",
            model=self._model,
            toolkit=self._registry.toolkit_for(tenant),   # 只读白名单内的两个工具
            budget=budget,                                # 应用层独立计算
            hooks=[self._permission_gate],                # 叠加层：PermissionEngine 规则闸
        )
```

- **没有按意图分支**：工具集恒定是两个检索工具，选择权在 ReAct。
- **没有委托 Tool**：诊断/草稿不在本模块的 Toolkit 里，结构上不可能被"误调"。
- `agentscope/pipeline` / `msghub` 自 2.0.0 起已删除；顺序组合用应用层 `await`，并行用 `asyncio.gather` 并发独立 `Agent.reply()` 后显式合并。`GoalPipeline` / `TeamPipeline` 虽存在但标记 experimental，不进生产。`Agent` / `Toolkit` 构造参数以官方源码树为准（核对结论见 [ADR-01](../架构设计v2/00-架构决策记录ADR.md)）。

### 9.3 工具执行器（ToolExecutor）

```python
class ToolExecutor:
    """执行单个只读检索工具：权限 / 版本 / trace / 预算校验。"""

    def __init__(self, registry: ToolRegistry, trace_repo: RetrievalTraceRepo) -> None:
        self._registry = registry
        self._trace_repo = trace_repo

    async def run(self, tool_name: str, request: RetrievalRequest,
                  tenant: TenantContext, budget: ToolCallBudget,
                  state: RetrievalState) -> RetrievalState:
        tool = self._registry.get(tool_name)
        if tool is None or not tenant.can_access(tool.required_tenant_scopes):
            await self._trace_repo.save_denied(tool_name, tenant)
            state.denied = True
            state.stop_reason = "conflict"
            return state
        if state.tool_calls >= budget.max_tool_calls:
            state.stop_reason = "budget"
            return state
        try:
            view = await tool.handler(request=request, tenant=tenant)
            await self._trace_repo.save_ok(tool_name, view)
            state.evidence.append(view)
            state.tool_chain.append(tool_name)
            state.tool_calls += 1
            return state
        except Exception as e:
            await self._trace_repo.save_error(tool_name, e)
            state.stop_reason = "conflict"
            state.error = str(e)
            return state
```

- 工具名由调用方（单跳起点判定或 ReAct）给出，不再从"意图"映射。
- 预算在工具执行前校验，超限直接停止并标记 `stop_reason="budget"`。

### 9.4 启动断言（只读 + 依赖可达性）

```python
class ReadOnlyToolGate(Exception):
    """启动时发现非只读工具，拒绝启动。"""
class DependencyUnreachableGate(Exception):
    """启动时发现检索能力依赖不可达，拒绝启动。"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.tool_registry
    # 1. 只读断言
    registry.validate_on_startup()
    # 2. 依赖可达性断言（编排的前提：两个检索能力必须可达）
    await assert_dependencies_reachable(app.state.acl_clients)
    yield

async def assert_dependencies_reachable(clients: dict) -> None:
    for name, client in clients.items():
        if not await client.ping():
            raise DependencyUnreachableGate(f"依赖不可达: {name}（检索编排要求检索能力就绪）")
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

- 只有检索编排接口，没有统一问答入口；`/explain` 回溯检索过程。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `agentic_query_total` | 检索请求次数（按 `retrieval_path` label） |
| `agentic_hop_total` | 每次请求的工具调用次数（Histogram，观察单跳/多跳比例） |
| `agentic_tool_call_total` | 工具调用次数（按 tool / status） |
| `agentic_stop_reason_total` | 停止原因分布（enough / no_new_evidence / budget / conflict） |
| `agentic_query_latency_seconds` | 检索延迟（Histogram） |
| `agentic_cache_hit_total` | 缓存命中 |
| `agentic_low_confidence_total` | 低置信度转人工 |
| `agentic_dependency_unreachable_total` | 下游不可达降级 |

### 10.2 trace 串联

- `trace_id` 从检索入口生成，`TracingMiddleware` 在 `RetrievalPlanner`、`ToolExecutor`、多跳 Agent 都注入 span。
- 调制造追溯/文档检索时透传 `traceparent`，使检索入口 → 两个能力 → 下游 REST 的 trace 串联。
- `AnswerAudit` 记录问题 / 检索路径 / 工具链 / 停止原因 / 证据摘要 / `trace_id`，`/rag/explain/{audit_id}` 可回溯。

### 10.3 兜底

- **下游不可达**：制造追溯或文档检索故障 → 降级"该能力暂时不可用"或返回已得证据 + 转人工。
- **低置信度**：`confidence < 0.6` → `needs_human_review=True`，标注"建议人工核对"。
- **证据不足**：停止时槽位不齐 → `detail["missing"]` 列出缺口，不编造。
- **预算/超时**：达到工具次数或时长上限 → 返回已得证据 + `stop_reason` + 转人工提示。
- **冲突**：版本冲突 / 权限拒绝 → 停止并显式说明，不跨版本硬答。

---

## 11. 实现步骤

### 阶段一：骨架与单跳（2 周）

1. 搭 `agentic_rag_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 实现 `ToolRegistry` + `ToolExecutor` + `ReadOnlyToolGate` 启动断言（§9.3、§9.4）。
3. 实现 `plan_start` 起点判定与两条单跳路径（追溯 / 文档），跑通单跳直答。

### 阶段二：多跳编排（2-3 周）

4. 实现 `RetrievalAgentFactory` + 多跳 ReAct（§9.2），限制工具面与预算。
5. 实现停止条件（证据充分 / 无新增 / 预算 / 冲突）（§4.3）。
6. 实现 ACL 客户端（制造追溯 / 文档检索）（§7.2）。

### 阶段三：统一输出与可观测（1-2 周）

7. 实现 `RagAnswer` + `AnswerAudit`（§6、§10.2）。
8. 实现 `/rag/explain/{audit_id}` 回溯端点（§9.5）。
9. 接 OpenTelemetry + prometheus 指标；单跳/多跳比例与停止原因看板。

### 阶段四：加固与评测（1 周）

10. 沉淀评测集（典型检索问题 + 预期证据/停止原因），回归多跳准确率与证据召回率。
11. 兜底链路全测（下游不可达 / 低置信度 / 证据不足 / 预算超限 / 版本冲突）。
12. 确认 🔴 决策点（单次检索超时阈值、多跳上限、证据条数上限）。

> **前提**：必须在文档检索与制造追溯能力成型后启动；没有两个工具，编排就是空转。

---

## 12. 约束落地检查清单

- [ ] 无意图分类器、无 `IntentCategory`、无跨能力委托客户端（§1.3）。
- [ ] 所有注册工具 `read_only=True`，`ReadOnlyToolGate` 启动断言生效。
- [ ] `assert_dependencies_reachable` 启动断言：制造追溯与文档检索不可达时拒绝启动。
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

**Q：Agentic RAG 和入口网关是什么关系？**
A：分工关系。网关回答"这问题该找检索、诊断还是草稿"，Agentic RAG 回答"这次检索还要再查什么"。意图路由与跨能力委托在网关（[Agent入口网关-意图路由.md](../AGENT服务/Agent入口网关-意图路由.md)）；本模块只有检索工具选择。这样依赖是单向的：网关 → RAG / agent-service，不会成环。

**Q：为什么 Agentic RAG 不做意图路由？**
A：定位不允许。RAG 引入路线 §3 说 Agentic 不是所有请求必须经过的入口层，只决定"什么时候再查一次、查什么、什么时候停止"。把分诊放进来，rag-service 就变成了入口，还要反向依赖 agent-service 才能委托诊断，两个服务互相依赖。入口分诊随渠道和话术变，检索编排随数据和证据契约变，节奏也不同。

**Q：Agentic RAG 和诊断能力怎么分工？**
A：本模块只做**检索内**的追加查询与证据组合（≤2 次工具调用），不做跨上下文 5M1E 推理。需要多步诊断的问题由网关直接委托诊断能力；本模块不持有诊断客户端，结构上不会越界。

**Q：多跳会不会不可控？**
A：三层约束：工具面只有两个只读工具；预算硬限制（≤2 次调用、≤20 条证据、≤15s）；停止条件明确（证据充分 / 无新增 / 预算 / 冲突）。另外单跳优先——多数问题根本不进 ReAct。

**Q：和普通 RAG 有什么区别？**
A：普通 RAG 是"一次检索 + 一次生成"；Agentic RAG 多的是"证据不够时再查一次"的判断与组合，但动作空间被限制在只读白名单和预算内，不是自由探索。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地。重点是四条判断：① Agentic RAG 只做检索编排，不做入口分诊与跨能力委托；② 单跳优先，多跳有预算和停止条件；③ 只读、版本锚点、权限、不进过点四条红线全继承；④ trace 从检索入口发起，可回溯每次检索路径与停止原因。

---

## 14. 一句话定位

"Agentic RAG 是 rag-service 的检索内编排：给定问题、种子与版本锚点，在文档检索与制造追溯两个只读工具之间判断是否追加检索、查什么、何时停止，最后收敛成带来源引用的 `RagAnswer`。它不做意图分诊、不做跨能力委托——入口分派归网关，深度诊断归诊断能力。单跳优先，多跳受 2 次工具调用预算约束，全程只读、版本锚定、不进过点主事务。"
