# Agentic RAG 详细设计（统一入口收口 + 意图路由 + 子 Agent 委托）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.5 路线 E（Agentic RAG）的落地展开，输出**技术栈、意图路由设计、工具与子 Agent 编排、统一输出、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + LangGraph + Pydantic）。Agent 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 REST / Kafka 解耦，互不侵入。
> **口径纪律**：Agentic RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了 Agentic RAG"。MES 领域对错误答案零容忍，所以本文强调**收口不造新能力 + 路由委托 L1/L2 + 继承全部只读红线**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 定位：E = AGENT 路线 L0 收口型

[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §2.1 明确：**L0 收口型问答 Agent = RAG 路线 E**。E 不是又一个新能力，而是**统一入口收口层**--一个 Agent 入口，按问题路由到 RAG 路线 A/B 的工具 + L1/L2/L3 子 Agent（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.5）。

- **价值在"收口"不在"新能力"**：A/B 是散落的检索能力，L1/L2/L3 是专精 Agent。用户不该在 多个入口间选择该问谁--E 做意图路由，把问题送到对的地方。
- **何时做**：等 A/B + L1-L3 成型后再收口（[RAG服务引入路线.md](../RAG服务引入路线.md) §3、[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §5）。否则 E 没工具可调，退化为套壳问答。**E 是最后一步，不是第一步**。

### 1.2 目标

把 A/B 的检索能力 + L1/L2/L3 的 Agent 能力收口成**一个统一入口**，让用户用自然语言提问，E 自动判断"这问题该用哪条路线答"，路由到对应工具或子 Agent，返回统一格式的答案 + 来源引用 + 工具链。

典型场景：

1. **追溯诊断类**："SN-001 焊接不良根因" -> E 识别为深度诊断 -> **委托 L1 子 Agent**（多步 5M1E 推理）-> 返回根因假设
2. **文档查询类**："SPI 报警怎么处置" -> E 识别为文档检索 -> 调 B 的 `search_docs` 工具 -> 返回 SOP 片段 + 引用
3. **追溯事实类**："SN-001 过了哪几站" -> E 识别为追溯检索 -> 调 A 的 `query_traceability_graph` 工具 -> 返回子图
4. **草稿生成类**："给这批不良草拟返工单" -> E 识别为写意图 -> **委托 L2 子 Agent**（草稿不落库）-> 返回返工单草稿供工程师确认

### 1.3 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **收口不造新能力** | E 只做路由 + 轻量组合 + 委托，不重建检索/推理/草稿能力 | E 无自有向量库/图谱，全部调 A/B + L1/L2/L3 |
| **只读（继承 L1）** | E 全程只读，无写工具；写意图委托 L2 但 L2 也只草拟不落库 | `ReadOnlyToolGate` 启动断言；写工具不注册到 E 的 ToolRegistry |
| **不进过点主事务** | E 不调过点引擎放行/拦截 API | 过点 toolset 不暴露 `pass/judge` 类工具（继承 L1 §1.2） |
| **版本一致性** | 查工艺带版本锚点（`version`+`version_kind`）；文档检索带版本过滤 | 工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承 A/B/L1） |
| **权限隔离** | 工具调用前按 `tenant_scope` 过滤 | 路由 + 工具调用都带 `TenantContext`，前置过滤（继承 L1 §4.3） |
| **可观测兜底** | 每答案带工具链 + 来源引用 + 置信度；低置信度转人工 | trace 从 E 发起串联到子 Agent/工具；置信度阈值兜底 |
| **委托而非重复** | 深度诊断委托 L1，不自己多步推理；写意图委托 L2，不自己草拟 | E 的 `recursion_limit` 小（轻量组合），深度场景路由到 L1 子 Agent |

### 1.4 与 L1/L2/L3 子 Agent 的分工（核心边界）

E 是"广而浅"的收口层，L1/L2/L3 是"深而专"的子 Agent。E 不重复它们的能力，而是**路由 + 委托**：

| 维度 | E（本文，L0 收口） | L1 诊断 | L2 草稿 | L3 编排 |
|------|-------------------|---------|---------|---------|
| 形态 | 意图路由 + 轻量组合 + 委托 | 多步只读 ReAct 诊断 | 写意图草稿（不落库） | 跨上下文流程编排 |
| 推理深度 | 浅（单工具或 1-2 组合） | 深（≤10 步 5M1E 推理） | 中（拉数据 + 草拟） | 深（多步流程 + 人在回路） |
| `recursion_limit` | 小（如 6，轻量组合） | 20（[L1](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §5.1） | 中 | 中 |
| 写动作 | 无（委托 L2） | 无（只读） | 草稿不落库 | 受限写 + confirmation gate |
| 典型问题 | "这问题该用哪条路线答" | "根因是什么" | "草拟返工单" | "换线编排" |

> **reconcile 两条路线文档的张力**：[RAG服务引入路线.md](../RAG服务引入路线.md) §2.5 说 E"能多步推理"，[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §2.1 说 L0"没有跨上下文多步推理"。解法：E 做**轻量 ReAct**（能选工具、能组合 1-2 个工具给答案，比单次 RAG 进一步），但**深度跨上下文多步诊断委托 L1**。E 的"多步"是相对单次检索而言，不是 L1 那种"自己决定下一步查什么"的深度推理。这样既满足"能多步推理"的收口定位，又不与 L1 的专精诊断重复。

### 1.5 与 A/B RAG 的关系

| RAG 路线 | E 如何使用 | 形态 |
|---------|-----------|------|
| **A 追溯型** | 封装为 `query_traceability_graph` 工具，E 直接调（简单追溯）或委托 L1（深度诊断） | 工具 + 子 Agent |
| **B 文档型** | 封装为 `search_docs` 工具，E 直接调 | 工具 |


### 1.6 与 Java 技术栈的关系

- E 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只通过 REST 调 A/B RAG 服务 + L1/L2/L3 Agent 服务的只读接口。
- 跨语言物理边界天然强制只读：E 无法共享 Java 事务/内存，无法进过点主事务、无法旁路应用服务写路径（与 L1 同构，[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §1.3）。
- E 是纯编排层，复用 A/B + L1/L2/L3 的既有契约，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 与 L1/L2/L3 Agent 同栈，复用 LangGraph + LLM 抽象 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI，统一问答 HTTP 入口 |
| Agent 编排 | **LangGraph** | 轻量 StateGraph 做"路由节点 -> 工具/委托节点 -> 收口"；与 L1 同框架，可嵌套调用 L1 子图 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 模型可插拔，与 L1/L2/L3 一致 |
| 数据校验 | **Pydantic v2** | 意图分类/工具入参/统一答案 DTO 的 schema 即类型 |
| HTTP 客户端 | **httpx**（异步） | 调 A/B RAG + L1/L2/L3 Agent 只读 REST |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 会话、路由 trace、统一答案审计 |
| 缓存 | **redis-py (async)** | 相同问题短缓存（同问题重复问不重跑路由/工具） |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 从 E 发起串联到子 Agent/工具 |
| 配置 | pydantic-settings | 环境变量统一管理 |
| 部署 | 独立微服务 `agent-gateway-service`（uvicorn + gunicorn worker） | 与三大服务同网格，K8s 部署 |

### 2.2 为什么 E 用 LangGraph 轻量图而非裸路由

- E 的核心是"路由 + 轻量组合"：多数问题单工具即答，少数需组合 1-2 工具。LangGraph 的 `StateGraph` 把"意图路由节点 -> 工具执行节点 -> 收口节点"做成显式图，支持条件路由（单工具直答 vs 多工具组合 vs 委托子 Agent）。
- 与 L1 同框架的好处：E 可把 L1 的 LangGraph 子图作为"委托节点"嵌入，trace/会话/checkpointer 复用一套机制（§5.3）。
- `recursion_limit` 小（如 6）对应"E 不做深度推理"的硬上限--深度场景路由到 L1，E 自己最多组合 1-2 工具就收口。

### 2.3 为什么 E 不重建检索/推理能力

- A/B 已建好检索能力（向量/图谱），L1/L2/L3 已建好 Agent 能力（诊断/草稿/编排）。E 重建等于重复造轮子 + 多份事实源。
- E 的价值在"路由判断"--把问题送到对的能力，而非自己再做一遍。这符合"整合而非拆分"的纪律：收口成单入口，不另起能力。
- 委托而非重复：深度诊断委托 L1，E 不自己多步推理；写意图委托 L2，E 不自己草拟。E 是编排层，不是能力层。

### 2.4 部署形态（车间网隔离）

- E 是统一入口，部署在办公网/车间网均可访问的位置。LLM 视安全策略二选一（云端 API 或本地化模型），`BaseChatModel` 抽象保证切换零代码改动。
- E 依赖 A/B + L1/L2/L3 服务的可达性：下游服务故障时 E 降级（§10.3），不硬答。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ agent-gateway-service（统一入口，Python + FastAPI + LangGraph）    │
│                                                                    │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐  │
│  │ FastAPI      │─▶│ GatewayService                            │  │
│  │ /agent/chat  │  │  构建 LangGraph 路由图 + 驱动             │  │
│  └──────────────┘  └────────────┬─────────────────────────────┘  │
│                                 │                                  │
│                      ┌──────────▼──────────┐                       │
│                      │ LangGraph 路由图     │                       │
│                      │ router -> tool/delegate ->收敛 │              │
│                      └──────────┬──────────┘                       │
│                                 │                                  │
│         ┌───────────────────────┼───────────────────────┐          │
│         ▼                       ▼                       ▼          │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐    │
│  │ IntentRouter │    │ ToolExecutor     │    │ SubAgentDelegator │ │
│  │ NL -> 意图/路由 │    │ A/B 工具调用   │    │ 委托 L1/L2 子Agent│ │
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
  │ A 追溯RAG │  │ B 文档RAG │    │ L1 诊断   │ │ L2 草稿   │
  │ /rag/trace│  │ /rag/docs │    │ /agent/   │ │ /agent/   │
  └──────────┘  └──────────┘    │ diagnose  │ │ draft    │
                                └──────────┘ └──────────┘
```

### 3.1 关键设计决策

- **路由即核心**：`IntentRouter` 是 E 的命脉--路由错了全盘皆错。规则优先（高频问题关键词命中）+ LLM 兜底分类，降低对模型依赖。
- **工具与委托分离**：`ToolExecutor`（调 A/B 工具，单次或轻量组合）与 `SubAgentDelegator`（委托 L1/L2 子 Agent）解耦。简单查询走工具直答，深度场景走委托--单一职责（SRP）。
- **统一输出**：所有路径（工具直答/委托子 Agent）都收敛到 `AgentAnswer` 统一格式，用户看到一致体验。
- **trace 从 E 发起**：E 是入口，`trace_id` 从 E 生成，透传到 L1/L2/A/B，全链路可回溯。

---

## 4. 意图路由设计

意图路由是 E 的核心。这一节定义"问题 -> 命中哪条路线"的分类规则。

### 4.1 意图分类（IntentCategory）

意图严格对齐 A/B + L1/L2 的能力边界，不另造分类：

| 意图 | 路由到 | 形态 | 典型问题 |
|------|--------|------|---------|
| `TRACE_FACT`（追溯事实） | A 工具 `query_traceability_graph` | 工具直答 | "SN-001 过了哪几站""这批用了哪批锡膏" |
| `ROOT_CAUSE`（根因诊断） | L1 子 Agent | 委托 | "SN-001 焊接不良根因""这批不良 5M1E" |
| `DOC_LOOKUP`（文档查询） | B 工具 `search_docs` | 工具直答 | "SPI 报警怎么处置""首件检验流程" |
| `DRAFT_REQUEST`（草稿生成） | L2 子 Agent | 委托 | "给这批不良草拟返工单""草拟 8D 报告" |
| `UNKNOWN`（未识别） | 转人工 | 兜底 | 路由不了的问题 |

> **TRACE_FACT vs ROOT_CAUSE 的边界**：问"过了哪几站"是事实检索（A 工具直答），问"为什么不良"是根因诊断（委托 L1）。前者一次图谱检索即答，后者需多步 5M1E 推理。E 的路由器按"是否需多步推理"区分：事实类走 A 工具，诊断类委托 L1。

### 4.2 路由规则（规则优先 + LLM 兜底）

```text
IntentRouter.classify(question)
   │
   ├─ 1. 规则优先（高频问题关键词命中）
   │     ├─ 含"根因/5M1E/为什么不良" -> ROOT_CAUSE -> 委托 L1
   │     ├─ 含"怎么处置/SOP/怎么修/流程" -> DOC_LOOKUP -> B 工具
   │     ├─ 含"草拟/生成返工单/8D" -> DRAFT_REQUEST -> 委托 L2
   │     └─ 含"过了哪几站/用了哪批/位置" -> TRACE_FACT -> A 工具
   │
   ├─ 2. LLM 兜底分类（规则命中不了）
   │     with_structured_output(IntentCategory) 强制枚举
   │
   └─ 3. 仍不确定 -> UNKNOWN -> 转人工
```

- **规则优先**：高频问题不走 LLM，确定性强、成本低。只有规则命中不了才走 LLM 意图分类。
- **LLM 兜底强约束**：`IntentCategory` 固化为 Enum，模型只能选枚举值，避免编造意图。
- **UNKNOWN 兜底**：路由不了的问题不硬答，转人工"该问题暂不支持，请联系工程师"。

### 4.3 路由到工具 vs 子 Agent 的决策

| 意图 | 单步还是多步 | 路由 |
|------|------------|------|
| TRACE_FACT / DOC_LOOKUP | 单步（一次工具调用即答） | `ToolExecutor` 直答 |
| ROOT_CAUSE | 多步（深度诊断） | `SubAgentDelegator` 委托 L1 |
| DRAFT_REQUEST | 多步（拉数据 + 草拟） | `SubAgentDelegator` 委托 L2 |
| 少数需组合的（如"SN-001 根因 + 处置 SOP"） | 轻量组合（1-2 工具） | E 的 LangGraph 轻量 ReAct（`recursion_limit=6`） |

- **单步直答**：多数问题一次工具调用即答，E 不做多步。
- **深度委托**：需多步推理的委托 L1/L2，E 不自己推理。
- **轻量组合**：少数问题需组合 1-2 工具（如先查追溯再查 SOP），E 用小 `recursion_limit` 的轻量 ReAct 处理--这是 E 唯一自己做"多步"的场景，且步数严格受限。

---

## 5. 工具注册与子 Agent 委托

### 5.1 工具注册（对齐 A/B，只读白名单）

E 的 `ToolRegistry` 注册 A/B 的检索工具，全部只读：

| 工具名 | 封装的路线 | 入参 | 版本校验 |
|--------|-----------|------|---------|
| `query_traceability_graph` | A 追溯型 | sn/batch_no/wo_id, as_of, version, version_kind | 历史回放带版本锚点 |
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

- `read_only=False` 的工具在 E 的 `ToolRegistry` 启动时直接拒绝注册（继承 L1 `ReadOnlyToolGate`）。
- 工具调用前按 `TenantContext` 权限过滤（继承 L1 §4.3）。
- 工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承 A/B/L1 版本一致性）。

### 5.2 子 Agent 委托（SubAgentDelegator）

`SubAgentDelegator` 把深度场景委托给 L1/L2 子 Agent，E 不自己推理：

```python
class SubAgentDelegator:
    """委托 L1/L2 子 Agent，E 不自己多步推理。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def delegate_l1(self, question: str, tenant: TenantContext) -> L1Report:
        """委托 L1 诊断 Agent（多步 5M1E 推理）。"""
        resp = await self._http.post(
            "/agent/diagnose", json={"question": question},
            headers=tenant.headers(), timeout=60.0,  # L1 整会话 ≤60s
        )
        resp.raise_for_status()
        return L1ReportMapper.to_view(resp.json())

    async def delegate_l2(self, draft_kind: str, context: dict, tenant: TenantContext) -> L2Draft:
        """委托 L2 草稿 Agent（草稿不落库）。"""
        resp = await self._http.post(
            "/agent/draft", json={"kind": draft_kind, "context": context},
            headers=tenant.headers(), timeout=30.0,
        )
        resp.raise_for_status()
        return L2DraftMapper.to_view(resp.json())
```

- **委托不是调用工具**：L1/L2 是子 Agent（多步推理/草拟），E 把整个问题交给它们处理，不是单次工具调用。
- **超时对齐子 Agent**：L1 整会话 ≤60s（[L1](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) §5.1），E 的委托超时设 60s+。
- **结果转统一格式**：L1 的 `DiagnosisReport` / L2 的草稿都经 Mapper 转成 E 的 `AgentAnswer`，用户看到一致体验。

### 5.3 LangGraph 轻量路由图

E 的 LangGraph 图是轻量的"路由 -> 执行/委托 -> 收口"：

```text
[router_node] 意图分类
      │
      ├─ 单步意图 ──▶ [tool_node] 调 A/B 工具 ──▶ [converge_node] 收口
      ├─ 委托意图 ──▶ [delegate_node] 委托 L1/L2 ──▶ [converge_node]
      ├─ 轻量组合 ──▶ [model_node] ↔ [tool_node] (recursion_limit=6) ──▶ [converge_node]
      └─ UNKNOWN ──▶ [converge_node] 转人工兜底
```

- **`recursion_limit=6`**：E 自己最多组合 1-2 工具（一次 model+tool 算 2 步，6 步即 3 次工具调用上限）。超过抛 `GraphRecursionError` 被捕获，降级为"建议改用 L1 深度诊断"或转人工。
- **与会话/trace 复用 L1 机制**：E 的 checkpointer 同样接 SQLAlchemy，trace 透传到子 Agent。

---

## 6. 统一输出与可观测

### 6.1 统一答案（AgentAnswer）

所有路径收敛到 `AgentAnswer`，用户看到一致体验：

```python
class AnswerSource(BaseModel):
    """答案来源引用（可回溯）。"""
    source_type: str          # "trace_node" / "sop_doc" / "l1_hypothesis"
    ref: str                  # "node_id=CheckpointRecord:xxx" / "SOP:WELD-014@v3" / "audit_id=..."
    route: str                # "A" / "B" / "L1" / "L2"

class AgentAnswer(BaseModel):
    question: str
    intent: str               # 命中的 IntentCategory
    route_taken: str          # 实际走的路线 "A" / "L1" / "B+C" ...
    summary: str              # 答案摘要
    detail: dict              # 路线相关的结构化详情（子图/SOP/图表/报告）
    sources: list[AnswerSource]  # 来源引用
    confidence: float
    tool_chain: list[str]     # ["query_traceability_graph", "search_docs"] 或 ["L1:diagnose"]
    trace_id: str
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"
```

- **`route_taken` + `tool_chain` 透明**：用户/工程师能看到"这个答案走了哪条路线、调了哪些工具"--收口入口的路由可观测。
- **`sources` 强制引用**：每个答案带来源（SOP 带版本、追溯节点 ID、L1 假设证据），可点开回溯。
- **`disclaimer` 不可省**：E 是辅助，最终处置需工程师确认。

### 6.2 trace 串联

- E 是入口，`trace_id` 从 E 生成，OpenTelemetry 在 `IntentRouter`、`ToolExecutor`、`SubAgentDelegator` 都注入 span。
- 委托 L1/L2 时透传 `traceparent` header，L1/L2 的工具调用 trace 与 E 的 trace 串联--工程师可从 E 的答案回溯到 L1 的每步工具调用。
- `AnswerAudit` 记录问题/意图/路由/工具链/答案摘要/`trace_id`，可回溯。

---

## 7. 实现方案

### 7.1 网关编排服务（GatewayService）

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
    ) -> None: ...

    async def chat(self, request: ChatRequest, tenant: TenantContext) -> AgentAnswer:
        # 1. 缓存（同问题 + 租户命中即用）
        cached = await self._cache.get(request, tenant)
        if cached:
            return cached
        # 2. 意图路由
        intent = await self._intent_router.classify(request.question)
        # 3. 驱动 LangGraph 路由图
        graph = self._graph_builder.build(intent, tenant)
        final_state = await asyncio.wait_for(
            graph.ainvoke(
                {"question": request.question, "intent": intent, "tenant": tenant},
                config={"recursion_limit": 6, "configurable": {"thread_id": request.session_id}},
            ),
            timeout=70.0,  # 略大于 L1 的 60s 委托超时
        )
        # 4. 收敛统一答案
        answer = self._build_answer(request, intent, final_state)
        # 5. 审计 + 缓存
        await self._audit_repo.record(request, intent, answer)
        await self._cache.set(request, tenant, answer)
        return answer
```

- 编排与路由/执行/委托分离（SRP）。
- 缓存按"问题 + 租户"键，同问题重复问不重跑。

### 7.2 ACL 防腐层（调 A/B + L1/L2）

```python
class TraceRagAclClient:
    """A 追溯型 RAG 只读 ACL。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def query_traceability_graph(
        self, seed: dict, as_of: datetime, tenant: TenantContext
    ) -> TraceSubgraphView:
        resp = await self._http.post(
            "/rag/trace/expand", json={"seed": seed, "as_of": as_of.isoformat()},
            headers=tenant.headers(), timeout=5.0,
        )
        resp.raise_for_status()
        return TraceSubgraphMapper.to_view(resp.json())


class L1DelegationClient:
    """L1 诊断子 Agent 委托 ACL。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def delegate(self, question: str, tenant: TenantContext) -> L1ReportView:
        resp = await self._http.post(
            "/agent/diagnose", json={"question": question},
            headers=tenant.headers(), timeout=60.0,
        )
        resp.raise_for_status()
        return L1ReportMapper.to_view(resp.json())
```

- 外部 DTO 不进 E 核心，只暴露 `TraceSubgraphView`/`L1ReportView`（ACL 约束）。
- 委托超时对齐子 Agent（L1 ≤60s）。

### 7.3 红线继承（只读 + 版本 + 权限）

E 继承 L1 的全部红线，不因"收口"而放松：

1. **只读闸**：`ReadOnlyToolGate` 启动断言，无写工具注册。
2. **版本闸**：工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验。
3. **权限闸**：工具调用前按 `TenantContext` 过滤。
4. **不进过点主事务闸**：过点 toolset 不暴露 `pass/judge`，

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
      acl/                     # 调 A/B RAG + L1/L2 Agent
        trace_rag.py           # A
        doc_rag.py             # B
        l1_delegation.py       # L1 委托
        l2_delegation.py       # L2 委托
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
```

- `domain/tool.ReadOnlyToolGate` 启动断言（继承 L1）。
- `infrastructure/acl/` 是防腐层，调 A/B + L1/L2，外部 DTO 经 Mapper 转内部视图。
- `infrastructure/ai/route_graph_builder` 是 LangGraph 轻量路由图（与 L1 的 `graph_builder` 同框架）。

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

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def classify(self, question: str) -> IntentCategory:
        # 1. 规则优先
        for keywords, intent in self.RULES:
            if any(k in question for k in keywords):
                return intent
        # 2. LLM 兜底（结构化输出，强制枚举）
        try:
            result = await self._llm.with_structured_output(IntentCategory).ainvoke(
                f"判断以下问题属于哪个意图（枚举）：\n{question}"
            )
            return result
        except Exception:
            return IntentCategory.UNKNOWN
```

### 9.2 LangGraph 路由图

```python
class RouteGraphBuilder:
    """构建 E 的轻量路由图：router -> tool/delegate -> converge。"""

    def __init__(
        self, tool_executor: ToolExecutor, delegator: SubAgentDelegator, llm: BaseChatModel
    ) -> None:
        self._tool_executor = tool_executor
        self._delegator = delegator
        self._llm = llm

    def build(self, intent: IntentCategory, tenant: TenantContext) -> CompiledGraph:
        graph = StateGraph(AgentState)
        graph.add_node("router", self._router_node)
        graph.add_node("tool", self._tool_executor)
        graph.add_node("delegate", self._delegator)
        graph.add_node("converge", self._converge_node)
        graph.set_entry_point("router")
        graph.add_conditional_edges(
            "router",
            lambda s: self._route_decision(s["intent"]),
            {
                "tool": "tool",
                "delegate": "delegate",
                "unknown": "converge",
            },
        )
        graph.add_edge("tool", "converge")
        graph.add_edge("delegate", "converge")
        graph.add_edge("converge", END)
        return graph.compile()

    def _route_decision(self, intent: IntentCategory) -> str:
        if intent in (IntentCategory.ROOT_CAUSE, IntentCategory.DRAFT_REQUEST):
            return "delegate"      # 委托 L1/L2
        if intent == IntentCategory.UNKNOWN:
            return "unknown"
        return "tool"              # 单步工具直答
```

### 9.3 工具执行器（ToolExecutor）

```python
class ToolExecutor:
    """调 A/B 工具，权限校验 + trace。继承 L1 ToolNode 模式。"""

    def __init__(self, registry: ToolRegistry, trace_repo: RouteTraceRepo) -> None:
        self._registry = registry; self._trace_repo = trace_repo

    async def __call__(self, state: AgentState) -> AgentState:
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
            IntentCategory.TRACE_FACT: "query_traceability_graph",
            IntentCategory.DOC_LOOKUP: "search_docs",
        }.get(intent, "")
```

### 9.4 启动断言（只读校验 + 下游可达性）

```python
class ReadOnlyToolGate(Exception):
    """启动时发现非只读工具，拒绝启动。"""
class DependencyUnreachableGate(Exception):
    """启动时发现 A/B + L1/L2 依赖不可达，拒绝启动（E 是收口，依赖必须就绪）。"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.tool_registry
    # 1. 只读断言（继承 L1）
    registry.validate_on_startup()
    # 2. 依赖可达性断言（E 收口的前提：A/B + L1/L2 必须可达）
    await assert_dependencies_reachable(app.state.acl_clients)
    yield

async def assert_dependencies_reachable(clients: dict) -> None:
    """E 是收口入口，下游 A/B + L1/L2 必须可达，否则退化为套壳。"""
    for name, client in clients.items():
        if not await client.ping():
            raise DependencyUnreachableGate(f"依赖不可达: {name}（E 收口要求 A/B + L1-L3 成型）")
```

- `assert_dependencies_reachable` 体现"E 是收口，依赖必须就绪"的判断--E 不该在 A/B + L1/L2 没就绪时启动，否则就是套壳问答。

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
| `gateway_delegation_total` | 子 Agent 委托次数（L1/L2 label） |
| `gateway_chat_latency_seconds` | 整问答延迟（Histogram） |
| `gateway_cache_hit_total` | 查询缓存命中 |
| `gateway_low_confidence_total` | 低置信度转人工次数 |
| `gateway_unknown_intent_total` | 未识别意图转人工次数 |
| `gateway_dependency_unreachable_total` | 下游依赖不可达降级次数 |

### 10.2 trace 串联

- `trace_id` 从 E 生成，透传到 L1/L2/A/B（`traceparent` header）。
- `AnswerAudit` 记录问题/意图/路由/工具链/答案/`trace_id`，工程师可从答案回溯到每条路线的每步工具调用。

### 10.3 兜底

- **路由失败兜底**：`UNKNOWN` 意图 -> 转人工"该问题暂不支持"。
- **下游不可达兜底**：A/B/L1/L2 故障 -> 降级为"该路线暂时不可用，请联系工程师"或切换备选路线。
- **低置信度兜底**：`confidence < 0.6` -> `needs_human_review=True`，标注"建议人工核对"。
- **委托超时兜底**：L1 委托超时 -> 降级为"诊断未完成，已转人工"（继承 L1 §8.3）。
- **`recursion_limit` 兜底**：轻量组合超步数 -> 降级为"建议改用 L1 深度诊断"或转人工。

---

## 11. 实现步骤

### 阶段一：骨架与意图路由（2 周）

1. 搭 `agent_gateway_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 实现 `IntentRouter`（规则优先 + LLM 兜底）（§9.1）。
3. 实现 LangGraph 轻量路由图（§9.2），单工具直答路径跑通。
4. 实现 `ReadOnlyToolGate` + `assert_dependencies_reachable` 启动断言（§9.4）。

### 阶段二：工具与委托（2-3 周）

5. 实现 `ToolExecutor` + `ToolRegistry`（A/B 工具封装）（§9.3）。
6. 实现 ACL 客户端（A/B RAG）（§7.2）。
7. 实现 `SubAgentDelegator`（委托 L1/L2）（§5.2）。
8. 验证委托 L1 的 trace 串联（`traceparent` 透传）。

### 阶段三：统一输出与可观测（1-2 周）

9. 实现 `AgentAnswer` 统一格式 + `AnswerAudit` 审计（§6.1）。
10. 实现 `/agent/explain` 回溯端点（§9.5）。
11. 接 OpenTelemetry + prometheus 指标（§10.1），路由准确率告警。

### 阶段四：加固、评测与试点（1 周）

12. 沉淀评测集（典型问题 + 预期路由/答案），回归路由准确率。
13. 兜底链路全测（UNKNOWN/下游不可达/低置信度/委托超时/recursion_limit）。
14. 灰度统一入口试点，收集"路由是否准、答案是否对"反馈。
15. 确认 🔴 决策点（路由规则覆盖、轻量组合 vs 委托阈值、L3 编排纳入时机）。

> **前提**：E 的实现必须在 A/B + L1/L2 成型后启动（[RAG服务引入路线.md](../RAG服务引入路线.md) §3、[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §5）。E 是收口，不是起步。

---

## 12. 约束落地检查清单

- [ ] E 无自有检索/推理能力，全部调 A/B + 委托 L1/L2（§1.3 收口不造新能力）。
- [ ] 所有注册工具 `read_only=True`，`ReadOnlyToolGate` 启动断言生效（继承 L1）。
- [ ] `assert_dependencies_reachable` 启动断言：A/B + L1/L2 不可达时拒绝启动（E 收口前提）。
- [ ] E 不调过点引擎放行/拦截 API。
- [ ] 工艺/文档工具强制版本锚点（`version`+`version_kind`）入参，ACL 层校验（继承 A/B/L1 版本一致性）。
- [ ] 工具调用前按 `TenantContext` 权限过滤，拒绝记录可观测（继承 L1）。
- [ ] 深度诊断委托 L1，E 不自己多步推理；`recursion_limit=6` 限制轻量组合步数。
- [ ] 所有路径收敛到 `AgentAnswer` 统一格式，带 `route_taken` + `tool_chain` + `sources`。
- [ ] `trace_id` 从 E 发起，透传到 L1/L2/A/B，全链路可回溯。
- [ ] 路由失败/下游不可达/低置信度/委托超时 -> 转人工兜底，不硬答。
- [ ] 答案带 `disclaimer`：辅助信息，最终处置需工程师确认。
- [ ] E 在 A/B + L1/L2 成型后启动，不提前做（避免套壳）。

---

## 13. 面试防守 Q&A

**Q：Agentic RAG 和你已经规划的 A/B + L1/L2/L3 是什么关系？是不是重复了？**
A：不重复，E 是收口。A/B 是散落的检索能力，L1/L2/L3 是专精 Agent。用户不该在 多个入口间选择该问谁--E 做统一入口 + 意图路由，把问题送到对的地方。E 不重建检索/推理能力，全部调 A/B 工具 + 委托 L1/L2 子 Agent。[AGENT服务引入路线.md](../../AGENT服务/AGENT服务引入路线.md) §2.1 明确 E = L0 收口型。E 是最后一步，不是第一步--等 A/B + L1-L3 成型后收口，否则没工具可调，退化为套壳问答。

**Q：E 和 L1 诊断型 Agent 怎么分工？是不是又重复了？**
A：不重复，是广深分工。E 是"广而浅"的路由收口层（意图分类 -> 选工具/子 Agent -> 轻量组合），L1 是"深而专"的追溯多步诊断（≤10 步 5M1E 推理）。简单追溯事实（"过了哪几站"）E 调 A 工具直答；深度根因诊断（"为什么不良"）E 委托 L1。E 的 `recursion_limit=6` 只做轻量组合（1-2 工具），深度多步委托 L1。这 reconcile 了 RAG 路线 §2.5"E 能多步推理"和 AGENT 路线 §2.1"L0 没有跨上下文多步推理"--E 的"多步"是相对单次检索的轻量组合，深度推理委托 L1。

**Q：E 怎么判断一个问题该走哪条路线？**
A：意图路由是 E 的命脉，规则优先 + LLM 兜底。高频问题关键词命中直定意图（"根因/5M1E"->L1，"怎么处置/SOP"->B，"草拟返工单"->L2），命中不了才走 LLM 结构化输出分类（`IntentCategory` 固化为 Enum）。路由错了全盘皆错，所以路由准确率是 E 的核心健康度指标。UNKNOWN 意图不硬答，转人工。

**Q：E 会不会拖慢过点？**
A：不会。E 不调过点引擎放行/拦截 API，过点 toolset 不暴露 `pass/judge` 类工具（继承 L1 §1.2）。过点 P99 ≤200ms 仍由规则引擎保证，E 是管理层/工程师侧的问答入口，与过点执行完全无关。

**Q：E 路由错了怎么办？比如把文档查询误路由到 L1 诊断。**
A：三重兜底。一是路由准确率作为核心指标持续监控 + 评测集回归；二是 L1 委托有超时（≤60s）和置信度阈值，L1 发现问题不属于诊断范畴会返回低置信度，E 据此转人工；三是 `UNKNOWN` 意图和路由失败都转人工不硬答。`AnswerAudit` 记录每次路由决策，工程师可回溯"这个答案为什么走了 L1"，反馈调优路由规则。MES 领域错答案零容忍，宁可让人判。

**Q：E 是收口，那它自己有什么独特价值？不就是个路由器吗？**
A：价值恰在收口，不在新能力。一是**统一体验**：用户一个入口问所有问题，不用知道"这个问题该问 A 还是 L1"。二是**路由判断**：意图分类把问题送到对的能力，路由准确率是 E 的核心壁垒。三是**轻量组合**：少数跨路线问题（"SN-001 根因 + 处置 SOP"）E 用轻量 ReAct 组合 A+B 工具，不必都委托 L1。四是**统一可观测**：所有问答经 E，trace 从 E 发起串联全链路，收口入口的路由/工具链透明可回溯。这些不是"新能力"，但把散落的能力收成一致体验本身就是架构价值。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地，且 E 是三条路线里最后做的。重点是四条架构判断：① E = L0 收口型，不造新能力，全部调 A/B + 委托 L1/L2；② 意图路由是核心，规则优先 + LLM 兜底；③ 广深分工--E 轻量组合（`recursion_limit=6`），深度诊断委托 L1，不重复；④ 继承全部只读红线（只读闸/版本闸/权限闸/不进过点主事务闸）。E 必须在 A/B + L1-L3 成型后启动，否则退化为套壳。诚实 + 体现架构判断力，比硬吹"已上线 Agentic RAG"得分高。

---

## 14. 一句话定位

"Agentic RAG 是三条 RAG 路线的收口--一个统一入口按问题意图路由到 A/B 工具 + 委托 L1/L2 子 Agent。E 不造新能力，全部调 A/B 检索 + 委托 L1 深度诊断/L2 草稿；意图路由规则优先 + LLM 兜底，把问题送到对的能力；广深分工--E 只做轻量组合（`recursion_limit=6`），跨上下文深度多步委托 L1，不重复；继承全部只读红线（`ReadOnlyToolGate`/版本/权限/不进过点主事务），所有路径收敛到带 `route_taken`+`tool_chain`+`sources` 的统一答案，trace 从 E 发起串联全链路。E 是最后一步，等 A/B + L1-L3 成型后收口，否则退化为套壳问答。"
