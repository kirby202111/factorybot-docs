# FactoryAgent 总体设计与实现方案（Python 技术栈）

> 本文是 MES Agent 服务（`agent-service`）的总纲：**一个 Agent，三项能力**——诊断、草稿编写、流程编排。
> 面向场景：面试中被问"你这套 Agent 怎么设计 / 为什么不是多个 Agent 分级"时，输出**能力矩阵 + 统一运行时 + 写边界 + 渐进启用**。
> **与 RAG 的关系**：Agent 是 [RAG服务引入路线.md](../../RAG服务/RAG服务引入路线.md) 路线 E（Agentic RAG 收口）的展开。RAG 解决"问得到"，Agent 解决"做得完"——多步规划、主动触发、可控写回。
> **口径纪律**：说"规划 / 设计取舍"，不说"已上线 Agent"；不编业务效果数字。MES 对误写零容忍（错发一张返工单可能批量报废），写动作的闸门始终在人手里。
> **配套**：[诊断能力](FactoryAgent-诊断能力.md)、[草稿编写能力](FactoryAgent-草稿编写能力.md)、[流程编排能力](FactoryAgent-流程编排能力.md)、[LangGraph 统一架构与流程图](FactoryAgent-LangGraph统一架构与流程图.md)、[能力边界与选型决策卡](FactoryAgent-能力边界与选型决策卡.md)、[现场痛点与能力嵌入点](FactoryAgent-现场痛点与能力嵌入点.md)。

---

## 0. 一句话定位

**一套 `agent-service`，一张 `ToolRegistry`，一种 `ToolPolicyGate`，三条能力入口**——诊断负责"把证据变成假设"，草稿编写负责"把假设变成可下达的处置草稿"，流程编排负责"把处置塞进跨上下文长流程并在关键步停下来等人"。三者不是三个 Agent、不是三个等级，而是**同一个 Agent 在不同图形态下暴露的不同功能**。

---

## 1. 为什么不做能力分级：从"三层 Agent"到"一个 Agent 的三项能力"

### 1.1 分级的三个实际代价

早期设计把 Agent 拆成"诊断 / 草稿 / 编排"三层，按能力递增排序、按层验收。落到实现，这层硬边界带来三个具体问题：

| 代价 | 具体表现 |
|------|---------|
| **能力被割裂，真实需求横跨三层** | 客诉 8D = 编排（隔离 gate）+ 诊断（5M1E 排序）+ 草稿（8D 正文），一次请求跨三层；换线 mismatch = 编排骨架 + 诊断根因 + 草稿处置卡。硬分层只能靠"高层调低层"的单向依赖硬接，接不上的部分（如编排里要直接判根因）就被迫写第二份实现 |
| **基础设施三套重复** | 每层各有一套 gate（只读闸门 / 无写 client 闸门 / 写工具闸门）、一套会话表、一套指标前缀、一套评测口径；跨层共用只能复制粘贴，观测上看不到一条完整链路 |
| **验收口径被层级绑架** | 按"层"验收等于按"自主度"验收，容易变成"做到更高级=更好"的攀比，而不是"这个决策点是否真的需要 LLM"。层内的确定性步骤被默认交给模型，恰恰是 MES 最不能接受的 |

### 1.2 统一后的模型

**能力（capability）是图的形态 + 工具切片，不是部署单元。**

| 能力 | 图的形态 | 需要开放规划 | 写权限 |
|------|---------|-------------|--------|
| **诊断** `diagnosis` | 开放式 ReAct 子图（`model ↔ tool` 循环） | 是：取证路径由中间结果驱动 | 无（只读 toolset，ACL 只有 `query/fetch/search`） |
| **草稿编写** `drafting` | 固定链 + 策略分派（取证据 → 检索 → 结构化生成） | 否：步骤固定，按草稿类型分派 Builder | 无写 client；只产 `Draft`（`requires_confirmation` 恒真） |
| **流程编排** `orchestration` | supervisor 代码图（代码节点 + 能力调用节点 + barrier + gate） | 否：步骤顺序与判定规则显式在图里 | 受限写：写工具 `requires_confirmation` + `writes_via`，过 confirmation gate |

三个共享物：

- **同一个运行时** `AgentRuntime`：能力注册表 + 图装配 + 会话驱动 + 打断恢复。
- **同一个工具注册表** `ToolRegistry`：`ToolDescriptor` 带 `capability` 标签，按能力切片下发；不按"层"复制。
- **同一个策略闸门** `ToolPolicyGate`：只读断言、无写 client 断言、写需确认断言、无放行工具断言，一处实现、启动态校验。

### 1.3 依赖关系：双向可组合

分级模型里依赖是单向的（编排 → 草稿 → 诊断）。统一后变成**能力互相调用**：

```text
diagnosis (子图)  ◀──── drafting  取根因假设作为草稿语义骨架
        ▲                 ▲
        │                 │
        └──── orchestration ─────┘   在非确定决策点按需调用，代码节点不调
```

- **编排调用诊断**：换线 mismatch 后要判根因——直接 `ainvoke(diagnosis_subgraph, 结构化异常入参)`，不需要"把诊断层搬进编排层"。
- **编排调用草稿**：隔离集 / 处置卡 / 8D 正文——直接调 `DraftService.draft(kind=...)`。
- **草稿调用诊断**：草稿的 `intent` 与 `payload` 需要根因语义时，复用诊断输出的 `DiagnosisReport`（含 `subgraph_ref` 与证据引用），不重复推理。

### 1.4 保留"渐进启用"，去掉"能力分层"

统一的是**架构**，不是**上线节奏**。能力仍然按价值与风险先后启用，但这是**启用顺序**，不是能力本身的等级：

1. **先诊断**：纯只读，最坏情况是"没诊断出来"，复用 RAG 追溯能力即可起量。
2. **再草稿编写**：只产草稿不落库，写风险仍为零，把"诊断结论 → 可下达处置"补上。
3. **最后流程编排**：引入受限写与 gate，风险最高，必须等前两项的置信度标定、证据链回溯、评测集沉淀跑通。

> 这个顺序只说明"哪块先做"，不说明"哪块更高级"。**判断一个步骤该不该用 LLM，用的是三问（输入是否开放 / 是否需推理生成 / 分支是否难穷举），与它在哪项能力里无关**——详见 [能力边界与选型决策卡](FactoryAgent-能力边界与选型决策卡.md)。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 类型提示 + Pydantic 校验，AI 生态最成熟 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI、与 Pydantic 无缝，做三能力统一 HTTP 入口 |
| 图编排 | **LangGraph** | 显式状态图；同一套 `StateGraph` 同时表达 ReAct 子图、固定链、supervisor 编排图；`interrupt` / `Command(resume=…)` 原生支持人在回路 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 模型可插拔（Claude / 通义千问 / DeepSeek / 本地化模型），`with_structured_output` 强约束结构化输出 |
| 数据校验 | **Pydantic v2** | 工具入参 / 报告 / 草稿 / 动作卡的 schema 即类型，自动生成 JSON Schema 给模型 tool-calling |
| HTTP 客户端 | **httpx**（异步） | 调各上下文只读 REST、调 RAG 服务、调上下文应用服务写接口 |
| 消息 | **aiokafka** | 订阅领域事件（`ProcessRouteActivated`、`equipment.fault` 等）做主动触发；动作卡离线兜底 topic |
| 实时推送 | **WebSocket (SSE)** | 编排的动作卡实时推给责任人在线 UI |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 会话、步骤记录、报告、草稿存档、gate 决策、工具调用 trace |
| 缓存 | **redis-py (async)** | 工具结果短期缓存、限流、confirmation token 存储 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 串联、指标告警 |
| 配置 | pydantic-settings | 环境变量 / 配置文件统一管理 |
| 部署 | 独立微服务 `agent-service`（uvicorn + gunicorn worker） | 与三大 MES 服务同网格，K8s 部署 |

### 2.2 为什么全套用 LangGraph，而不是"诊断用图、草稿用函数"两套编排

- **一种编程模型，三种图形态**：诊断是 `model ↔ tool` 循环子图，草稿是"取证据 → 检索 → 生成"的固定链，编排是"代码节点 + 能力调用节点 + gate"的 supervisor 图。三者都是 `StateGraph`，共用 checkpointer、`recursion_limit`、`interrupt`、trace 注入点。少一套编排机制，就少一套边界与坑。
- **子图可组合**：诊断子图能被编排直接 `ainvoke` 复用；草稿链能被编排作为节点调用。若草稿改用裸 async 函数，跨能力复用时状态契约、打断恢复、trace 都要各写一份。
- **`recursion_limit` 即"最大步数"红线**：框架级硬上限，不是口头约束。
- 不选 AutoGen（conversational 模式适合多 agent 对话，不适合严格步骤编排 + 代码节点）/ CrewAI（抽象偏高，难做细粒度权限拦截与 gate 状态机）。

### 2.3 为什么不引入 Java 侧 Spring AI

- MES 主体是 Java/Spring，但 Agent 场景 Python 生态（LangGraph、模型 SDK、评测工具）明显更成熟。
- 跨语言的物理边界反而是好事：Agent 进程无法共享 Java 事务 / 内存，天然不能进过点主事务、不能旁路应用服务写路径。两侧只通过 REST + Kafka 契约解耦，不造新管道。

### 2.4 与 Java 主体服务的关系

- Agent 服务**不替换**三大服务的 Java/Spring 栈，只通过 REST 调只读接口与受限写接口、订阅 Kafka 领域事件。
- 复用 [实现说明](../../实现说明/) 既有 topic、REST 只读接口、领域事件 envelope，不造新契约。

---

## 3. 统一架构

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│ agent-service（独立微服务，Python + FastAPI + LangGraph）                          │
│                                                                                  │
│  FastAPI 统一入口                                                                 │
│   POST /agent/diagnose           ┐                                               │
│   POST /agent/draft              ├──▶ AgentRuntime（能力注册表 + 图装配 + 会话驱动）│
│   POST /agent/orchestration/...  ┘         │                                     │
│   POST /agent/orchestration/{id}/confirm   │                                     │
│                                            ▼                                     │
│        ┌───────────────────────────┬───────────────────────────┬──────────────┐ │
│        │ 图形态①开放式 ReAct        │ 图形态②固定链 + 策略分派    │ 图形态③supervisor│
│        │ diagnosis 子图             │ drafting 链                │ 编排图          │ │
│        │ model ↔ tool 循环          │ 取证据→检索→结构化生成      │ 代码节点+能力节点│ │
│        └───────────┬───────────────┴───────────┬───────────────┴──────┬───────┘ │
│                    │                           │                      │         │
│                    │        ┌──────────────────┴──────────────────────┘         │
│                    ▼        ▼                                                  │
│        ┌──────────────────────────────────────────────────────────────────┐    │
│        │ ToolRegistry（能力切片下发）                                        │    │
│        │  ToolDescriptor{ capability, read_only, requires_confirmation,     │    │
│        │                  writes_via, required_tenant_scopes }             │    │
│        │  ToolPolicyGate：只读断言 / 无写 client / 写需确认 / 无放行工具      │    │
│        └──────┬──────────┬──────────┬──────────┬──────────┬────────────────┘    │
│               ▼          ▼          ▼          ▼          ▼                      │
│          ACL 过点    ACL 工艺    ACL 物料    ACL 设备    RAG / 图服务              │
│          （只读）    （只读+版本）（只读）  （只读）    （只读检索/回查）           │
│               │          │          │          │          │                      │
│               └──────────┴────┬─────┴──────────┴──────────┘                      │
│                               ▼                                                  │
│           写路径（只有编排能力可用）：ACL 写 client + confirmation token            │
│           -> 各上下文正常应用服务（聚合根不变式 + 事务发件箱）                        │
└───────────────────────────────┬──────────────────────────────────────────────────┘
                                ▼
        生产执行服务(Java) / 制造资源服务(Java) / 设备管理服务(Java) / RAG 服务
                                ▲
                                │ 领域事件订阅（主动触发：异常巡检 / SOP 升版草拟 / 故障复产）
                          aiokafka
```

### 3.1 关键设计决策

- **一个 runtime，三种图形态**：能力差异体现在"图长什么样、拿哪些工具、有没有写权限"，而不是"起几个服务、各建一套底座"。
- **能力切片而非层级继承**：`ToolRegistry.tools_for(capability, tenant)` 按能力 + 租户双维下发，诊断拿不到写工具，编排拿不到放行工具。**边界靠注册表 + 启动断言，不靠调用方自觉**。
- **ACL 防腐层**：每个上下文一个 ACL 客户端 + Mapper，外部 DTO 不进 Agent 核心，只暴露内部视图（`PassRecordView`、`ProcessRouteView`、`TraceNodeView`）。外部 schema 变化不污染能力实现。
- **状态外置**：会话状态、步骤记录、gate 决策全部落 MySQL，`SqlSaver` checkpointer 支持进程重启续跑，不依赖模型侧会话。
- **同一条 trace 贯穿**：一次请求一个 `trace_id`，工具调用、LLM 调用、gate 决策、写路径全部挂同一 trace，`traceparent` 透传下游 Java 服务。

---

## 4. 能力编排模型

### 4.1 能力声明（`CapabilitySpec`）

每个能力在注册表里声明"我能写什么、我要哪些工具、我用哪种图"，启动时校验：

```python
class GraphShape(str, Enum):
    OPEN_REACT = "open_react"        # 诊断：开放式取证循环
    FIXED_CHAIN = "fixed_chain"      # 草稿：固定步骤 + 策略分派
    SUPERVISOR = "supervisor"        # 编排：代码节点 + 能力调用节点 + gate

class CapabilitySpec(BaseModel):
    name: str                        # diagnosis / drafting / orchestration
    graph_shape: GraphShape
    writes_allowed: bool             # 诊断/草稿 False，编排 True（且仅限声明 writes_via 的工具）
    max_recursion: int               # 诊断 20 / 草稿 8 / 编排 40
    session_timeout_s: float         # 诊断 60 / 草稿 90 / 编排 3600（含人确认等待）
    tool_tags: list[str]             # 按标签取工具切片，如 ["read:pass", "read:process"]
```

### 4.2 三种调用形态

| 调用方 | 形态 | 说明 |
|--------|------|------|
| 工程师 UI / 外部系统 | `POST /agent/diagnose`、`/agent/draft`、`/agent/orchestration/{scenario}/start` | 能力作为**入口**被直接调用 |
| 编排图的非确定节点 | `AgentRuntime.invoke("diagnosis", payload)` / `invoke("drafting", ...)` | 能力作为**子图**被编排调用，共享 session 与 trace |
| 事件订阅器 | `AgentRuntime.invoke("diagnosis", auto_payload)` | 能力作为**主动触发**被调用（异常巡检、SOP 升版草拟） |

> **关键**：三种形态共用一套会话表与 trace，编排里调诊断时，那条 ReAct 循环的每一步工具调用都挂在编排会话的同一个 `trace_id` 下——这是"一个 Agent"在可观测上的直接体现，也是分级模型做不到的。

### 4.3 统一会话与步骤模型

```sql
agent_session
  - session_id (PK)
  - capability (DIAGNOSIS / DRAFTING / ORCHESTRATION)
  - scenario   (按能力填：自由诊断 / draft_kind / changeover|fault_response|complaint_8d|process_change)
  - parent_session_id (FK, 可空)      -- 编排调诊断/草稿时指向父会话
  - business_ref (JSON)               -- work_order_id / batch_id / asset_id / serial_no
  - tenant_context (JSON)
  - status (RUNNING / SUSPENDED / DONE / TIMEOUT / FAILED)
  - current_step
  - created_at / updated_at

agent_step_record
  - record_id (PK)
  - session_id (FK)
  - capability                        -- 哪个能力产生的这步
  - step
  - node_type (CODE / AGENT)          -- 代码节点还是 LLM 节点
  - status (PENDING / RUNNING / GATE_WAITING / CONFIRMED / FAILED)
  - action_card_payload (JSON)
  - agent_hypothesis (JSON) / agent_confidence
  - gate_decision (PASS / REJECT / RETRY) / gate_decided_by / gate_decided_at
  - tool_call_traces (JSON)
  - occurred_at
```

`node_type` + `capability` 两个字段让"这次会话调了几次 LLM、哪几步是代码"可量化——这是"懂什么时候不用 AI"的可验证证据，而不是口号。

---

## 5. 统一工具注册与策略闸门

### 5.1 工具与上下文的映射

**限界上下文边界天然就是工具边界**（[领域总览.md](../../领域模型/领域总览.md) §2），不另造一套工具分类：

| 上下文 | 只读工具（示例） | 写工具（仅编排能力可见） |
|--------|-----------------|------------------------|
| 过点执行 | `query_pass_records` / `query_test_results` | 无（放行/拦截工具**永不注册**） |
| 工单管理 | `query_work_order` / `query_wo_progress` | 无 |
| 工艺管理 | `query_process_route`（强制版本） / `query_route_audit` | `draft_sop_publish`（草拟，人确认后发布） |
| 物料 | `query_material_batch` / `query_bom_version` / `query_kit_status` | 无 |
| 在制品追踪 | `query_wip_position` | 无 |
| 设备数据接入 | `query_device_params` / `query_equipment_telemetry` | 无 |
| 设备工装台账 | `query_asset_status` / `query_stencil_lending` / `query_stencil_master_history` | `draft_isolation_card`（草拟隔离集，人确认后下达） |
| 返修 / 返工 | `query_repair_history` / `query_rework_orders` | `draft_rework_order`（草拟返工单） |
| RAG 服务 | `query_traceability_graph` / `fetch_subgraph_nodes` / `search_docs` | 无 |

### 5.2 工具元数据

```python
class ToolDescriptor(BaseModel):
    name: str
    description: str                   # 给模型看的语义说明
    bounded_context: str               # 权限过滤 + 可观测维度
    capabilities: list[str]            # 哪些能力可见（诊断/草稿/编排可重叠）
    read_only: bool
    requires_confirmation: bool = False
    writes_via: str | None = None       # "返工上下文.application.release_batch_rework"
    args_schema: type[BaseModel]
    required_tenant_scopes: list[str]
```

### 5.3 一处闸门，四类断言

```python
class ToolPolicyGate(Exception):
    """任一断言失败，服务拒绝启动。"""

def validate_tool_policy(registry: ToolRegistry, specs: dict[str, CapabilitySpec]) -> None:
    for d in registry.all():
        # ① 写工具必须声明确认 + 落库路径
        if not d.read_only and not (d.requires_confirmation and d.writes_via):
            raise ToolPolicyGate(f"写工具必须 requires_confirmation + writes_via: {d.name}")
        # ② 只读工具不得偷偷带写路径
        if d.read_only and d.writes_via:
            raise ToolPolicyGate(f"只读工具不得声明 writes_via: {d.name}")
        # ③ 声明无写权限的能力，不得持有写工具
        for cap in d.capabilities:
            if not specs[cap].writes_allowed and not d.read_only:
                raise ToolPolicyGate(f"{cap} 不具备写权限，不得持有写工具: {d.name}")
        # ④ 放行 / 拦截类能力从架构层不存在于任何能力
        if d.name.startswith(("pass_judge", "force_release", "release_", "block_pass")):
            raise ToolPolicyGate(f"放行/拦截工具禁止注册: {d.name}")

def validate_acl_clients(clients: dict[str, object], specs: dict[str, CapabilitySpec]) -> None:
    """只读能力的 ACL 客户端不得出现写动词。"""
    write_verbs = ("create", "update", "delete", "submit", "release", "issue", "save")
    for cap, client in clients.items():
        if specs[cap].writes_allowed:
            continue
        for attr in dir(client):
            if callable(getattr(client, attr)) and not attr.startswith("_"):
                if attr.lower().startswith(write_verbs):
                    raise ToolPolicyGate(f"{cap} 不得持有写 client: {type(client).__name__}.{attr}")
```

四条断言分别覆盖：写动作越界、只读伪装、能力越权、误触放行。**红线靠启动失败兜底，不靠评审时口头承诺。**

---

## 6. 统一写边界：三级闸门

写风险不按"能力等级"分档，按**三个独立维度**共同约束：

| 维度 | 约束 | 落地 |
|------|------|------|
| **能力级** | `CapabilitySpec.writes_allowed`；诊断 False、草稿 False、编排 True | 启动断言：无写权限能力不得持写工具 |
| **工具级** | 写工具必须 `requires_confirmation=True` + `writes_via` 声明落库路径 | `ToolPolicyGate` 断言 |
| **运行级** | 写调用必须带 confirmation token（绑定 `action:target` + 过期），无 token 直接拒绝 | `WriteAclClient` 校验 + `confirmation_store`（redis） |

再叠加两条不可协商的硬约束：

- **不进过点主事务**：放行 / 拦截类工具从架构层不注册（断言 ④）；编排只编排人侧动作，过点 P99 ≤200ms 的判定仍走规则引擎。
- **不旁路应用服务写**：所有落库走各上下文正常应用服务，过聚合根不变式 + 事务发件箱，与人工点按钮完全同路径——Agent 只是"帮人填草稿 + 帮人排序步骤"。

---

## 7. 推荐包结构

```text
agent_service/
  app/
    api/
      diagnosis_router.py          # POST /agent/diagnose
      draft_router.py              # POST /agent/draft, GET /agent/draft/{id}/evidence
      orchestration_router.py      # POST /agent/orchestration/{scenario}/start, /{session_id}/confirm
      schemas.py
    application/                   # 能力服务层：一个能力一个 service
      runtime.py                   # AgentRuntime：能力注册表、图装配、会话驱动、子图调用
      diagnosis_service.py         # 诊断：驱动 ReAct 子图
      draft_service.py             # 草稿：分派 + 取证据 + 生成
      builders/                    # 草稿策略（返工单 / 8D / SOP / 处置卡 / 隔离集卡）
        base.py
        rework_order.py
        eight_d.py
        sop.py
        disposition_card.py
        isolation_card.py
      orchestration_service.py     # 编排：会话生命周期、supervisor 图驱动
      action_card_dispatcher.py    # 动作卡双通道（WebSocket + Kafka）
      session_manager.py
    domain/
      capability.py                # CapabilitySpec / GraphShape（能力声明与写策略）
      session.py / report.py       # AgentSession / DiagnosisReport / Hypothesis / FiveM1ECategory
      draft.py / evidence.py       # Draft / DraftKind / TraceNodeView
      orchestration.py             # OrchestrationSession / Step / ActionCard / GateDecision
      tool.py                      # ToolDescriptor / ToolRegistry
      gate.py                      # ToolPolicyGate（四类断言）
      tenant.py
    ai/                            # 三种图形态同处一层，强调"一个 Agent"
      diagnosis_graph.py           # 图形态①：ReAct 子图
      draft_chain.py               # 图形态②：固定链
      supervisor_graph.py          # 图形态③：编排图
      code_nodes/                  # 编排代码节点（确定性，不调 LLM）
        plan.py / query_compare.py / barrier.py / gate.py / write_via_appservice.py
      agent_nodes/                 # 编排中的非确定节点（内部调诊断/草稿能力）
        root_cause.py / fault_impact.py / traceability.py / draft_nodes.py
      scenarios/                   # 场景装配：换线 / 故障复产 / 客诉 8D / 工艺变更
      prompts/                     # 各能力 system prompt（版本化）
      llm_factory.py
    infrastructure/
      acl/                         # 每上下文一个 client + Mapper
        pass_execution.py / process_management.py / material.py / device_data.py
        tooling.py / equipment_telemetry.py / rework_write.py
      rag/                         # RagSearchClient / SubgraphClient
      kafka/                       # 事件订阅 + 动作卡 producer
      persistence/                 # SQLAlchemy 模型 + Repository + checkpointer
      redis_/                      # 工具缓存 / 限流 / confirmation_store
      obs/                         # OTel exporter + prometheus 指标
    config.py
    main.py                        # lifespan：ToolPolicyGate + ACL 断言
  tests/
  pyproject.toml
```

分层职责（避免写成面向过程的流水账）：`api` 只做协议，`application` 编排能力流程，`domain` 放能力声明与领域模型，`ai` 放图装配（代码节点与 agent 节点物理分开），`infrastructure` 做防腐与适配。

---

## 8. 统一的关键代码骨架

### 8.1 能力注册与运行时

```python
# app/application/runtime.py
class AgentRuntime:
    """一个运行时承载三项能力：按能力取工具切片 + 装配图 + 驱动会话。"""

    def __init__(
        self,
        specs: dict[str, CapabilitySpec],
        registry: ToolRegistry,
        graphs: dict[str, CompiledGraph],       # 能力 -> 图（已在启动时装配）
        session_manager: SessionManager,
        metrics: MetricsCollector,
    ) -> None:
        self._specs = specs
        self._registry = registry
        self._graphs = graphs
        self._sessions = session_manager
        self._metrics = metrics

    async def invoke(
        self,
        capability: str,
        payload: dict,
        tenant: TenantContext,
        parent_session_id: str | None = None,
    ) -> dict:
        spec = self._specs[capability]
        session = await self._sessions.create(capability, payload, tenant, parent_session_id)
        try:
            state = await asyncio.wait_for(
                self._graphs[capability].ainvoke(
                    {**payload, "tenant": tenant, "session_id": session.id},
                    config={
                        "recursion_limit": spec.max_recursion,
                        "configurable": {"thread_id": session.id},
                    },
                ),
                timeout=spec.session_timeout_s,
            )
            await self._sessions.finish(session, state)
            return state
        except (GraphRecursionError, asyncio.TimeoutError) as exc:
            await self._sessions.mark_timeout(session, str(exc))
            self._metrics.session_timeout.inc(capability)
            return {"status": "TIMEOUT", "reason": f"超出步数或时长上限，已转人工: {exc}"}
```

- `graphs` 在启动时按能力装配一次，运行时不再重建——能力差异是**装配差异**，不是运行期分支。
- 编排调诊断时，`invoke("diagnosis", ..., parent_session_id=orch_session.id)`：同一 trace、同一租户上下文、可观测上能串成一条链。

### 8.2 启动断言

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    specs = app.state.capability_specs
    registry = app.state.tool_registry
    validate_tool_policy(registry, specs)                 # 写/只读/越权/放行 四类断言
    validate_acl_clients(app.state.acl_clients, specs)    # 无写权限能力不得持写 client
    app.state.runtime = AgentRuntime(
        specs=specs,
        registry=registry,
        graphs=build_all_graphs(registry, app.state.deps),  # 三种图形态一次装配
        session_manager=app.state.session_manager,
        metrics=app.state.metrics,
    )
    yield

app = FastAPI(title="MES FactoryAgent", lifespan=lifespan)
```

### 8.3 主动触发（能力可被事件调用）

```python
# app/infrastructure/kafka/listeners.py
class DomainEventTrigger:
    """订阅只读领域事件，按事件类型调用对应能力。不消费任何写命令。"""

    ROUTES = {
        "equipment.fault": ("orchestration", "fault_response"),
        "process.route.activated": ("drafting", "SOP"),
        "defect_rate.spike": ("diagnosis", "auto"),
    }

    async def run(self, consumer: AIOKafkaConsumer) -> None:
        async for msg in consumer:
            capability, scenario = self.ROUTES[msg.topic]
            event = json.loads(msg.value)
            await self._runtime.invoke(
                capability,
                {"scenario": scenario, "event": event},
                TenantContext.from_event(event),
            )
```

- 主动触发只是"调用方换成事件"，能力实现不变——这是统一模型最直接的收益。
- 主动产出的报告 / 草稿同样走人工确认，不自动落库。

---

## 9. 可观测与兜底（各能力共用底座）

事实源唯一，详见 [Agent可观测性-设计与实现方案.md](../Agent可观测性-设计与实现方案.md)。要点：

- **指标**：通用 `agent_session_total{capability}` / `agent_tool_call_*` / `agent_llm_invocation_total{capability}` / `agent_step_total{capability,node_type}`；能力特有指标挂同一命名空间，不按层级另起一套。
- **链路**：一次请求一个 `trace_id`，`agent_step_record` 是给工程师 UI 的平铺证据链，Tempo/Jaeger 给 SRE 看火焰图，同源 `trace_id` 串联。
- **兜底**：置信度低 → `needs_human_review` 不推操作工；工具连续失败 3 次终止；`recursion_limit` / 超时转人工；编排 gate 超 deadline 挂起 `SUSPENDED`。**宁可拦下让人判，不硬答。**

---

## 10. 实现步骤（按能力启用，不按层级）

### 阶段一：底座 + 诊断能力（4 周）

1. 搭 `agent-service` 骨架（FastAPI + lifespan 断言），对齐 §7 包结构。
2. 落 `CapabilitySpec` / `ToolRegistry` / `ToolPolicyGate` / `AgentRuntime` 四件底座。
3. 实现诊断 ReAct 子图 + 2–3 个核心 toolset（过点 / 工艺 / RAG 图）。
4. 建 `agent_session` / `agent_step_record` / `agent_report` 三表 + checkpointer。
5. 打通"提问式诊断"闭环：工程师提问 → 多步取证 → 5M1E 假设 + 证据链。

### 阶段二：草稿编写能力（3 周）

6. 实现 `DraftService` + `DraftBuilder` 策略（返工单 / 8D / SOP）。
7. 接 `fetch_subgraph_nodes` 与 `search_docs`，复用诊断输出的 `subgraph_ref` 与版本锚点。
8. `Draft.requires_confirmation` 恒真；草稿只落存档表；工程师 UI 展示草稿 + 证据回溯。
9. 对接返工 / 质量 / 工艺上下文"人工确认后落库"的正式应用服务。

### 阶段三：编排能力 —— 先纯代码骨架（3 周）

10. 落 `supervisor_graph` + 代码节点（plan / query_compare / barrier / gate / done）。
11. 落 `ActionCard` + `interrupt` / `Command(resume=…)` + 动作卡双通道推送。
12. **换线全程 PASS 路径跑通，LLM 调用为 0**——先证明"代码能跑的不交给 LLM"。
13. 写工具白名单 + confirmation token + `writes_via` 落库路径。

### 阶段四：编排能力 —— 非确定节点接入（4 周）

14. 编排的 `root_cause` 节点接入诊断能力（结构化 mismatch 入参），产出根因 + 处置卡草稿。
15. `fault_impact` 节点：设备遥测 + FMEA 取证 → 隔离集草稿 → gate。
16. `draft_8d` / `draft_sop` 节点接入草稿编写能力。
17. 各上下文写 ACL（钢网归还/领用、隔离下达、SOP 发布）+ confirmation store。

### 阶段五：评测、加固与推广

18. 建评测集：按**能力维度**（诊断根因命中 / 草稿字段准确与采纳率 / 编排步数与 gate 拒绝率），见 [RAG与Agent评测-设计与实现方案.md](../../RAG与Agent评测/RAG与Agent评测-设计与实现方案.md)。
19. 工具结果缓存、限流（每租户并发会话上限）。
20. 灰度一条产线，按反馈调 prompt 与工具集，不按"层"批量推广。

---

## 11. 约束落地检查清单

- [ ] 三项能力共用一套 `CapabilitySpec` / `ToolRegistry` / `ToolPolicyGate` / `AgentRuntime`，无按能力复制的第二套底座。
- [ ] 无写权限能力（诊断 / 草稿）不得持有写工具，启动断言生效；其 ACL 无写动词。
- [ ] 所有写工具声明 `requires_confirmation=True` + `writes_via`，无 token 的写调用被拒。
- [ ] 任何能力下均无放行 / 拦截类工具（前缀断言）。
- [ ] 工艺查询强制版本锚点，ACL 校验返回 `ACTIVE`。
- [ ] 工具调用前按 `TenantContext` 过滤，拒绝记录可观测。
- [ ] 诊断与草稿不持有任何写 client；草稿只落存档表，不落 MES 业务库。
- [ ] 编排的代码节点（query/compare/barrier/gate）不调 LLM，`node_type=CODE` 落库可验。
- [ ] 编排调诊断 / 草稿时 `parent_session_id` 落库，一条 trace 串起跨能力链路。
- [ ] 报告 / 草稿带置信度与 disclaimer，低置信度转人工。
- [ ] 单工具超时 ≤2s；诊断整会话 ≤60s；编排 ≤3600s（含人确认）。
- [ ] 主动触发只订阅只读事件，产出仍需人确认。
- [ ] 结构化输出经 Pydantic 校验，不符合 schema 判失败重试，重试仍失败转人工。

---

## 12. 面试防守 Q&A

**Q：为什么不再分"诊断 / 草稿 / 编排"三个 Agent（三层）？**
A：因为真实需求横跨它们。客诉 8D 一次要走编排（隔离 gate）+ 诊断（5M1E 排序）+ 草稿（8D 正文），换线 mismatch 要走编排骨架 + 诊断根因 + 草稿处置卡。硬分层只能靠"高层调低层"单向依赖硬接，接不上的地方就写第二份实现。现在的模型是**一个 runtime 承载三种图形态**：诊断是开放式 ReAct 子图、草稿是固定链 + 策略分派、编排是 supervisor 代码图；三者共用一套会话表、一套工具注册表、一套策略闸门、一条 trace。能力的差别体现在"图长什么样、拿哪些工具、有没有写权限"，而不是"起几个服务、建几套底座"。

**Q：不分层会不会失去"风险梯度控制"？**
A：不会，风险梯度改由两个更本质的维度控制：① **能力的写权限声明**（`writes_allowed`：诊断 False、草稿 False、编排 True）——启动断言保证无写权限的能力根本拿不到写工具；② **工具的写声明**（`requires_confirmation` + `writes_via` + 运行时 confirmation token）。这两层叠加，比"层号"更硬：层号靠人记，断言靠启动失败。上线顺序也还在（先诊断、再草稿、最后编排），但那只是**启用节奏**，不是能力等级。

**Q：一个 Agent 会不会工具太多、模型调错工具？**
A：不会，因为工具是**按能力切片下发**的。`ToolDescriptor.capabilities` 声明哪些能力可见，`tools_for(capability, tenant)` 双维过滤——诊断模型看到的是 8–10 个只读工具，编排里的根因节点看到的是钢网/工艺审计相关的少数几个。这和"多个 sub-agent 各持小工具集"效果一致，但共用一个注册表和一套断言，不用维护多份工具定义。

**Q：为什么选 LangGraph 而不是裸 LangChain / 直接调模型 API？**
A：三种图形态都需要显式控制：诊断要对每步做权限拦截与 trace、草稿要固定链、编排要并行分支 + barrier + 可中断 gate。LangGraph 的 `StateGraph` 节点可以是任意 Python 函数——代码节点不消耗 LLM 调用，agent 节点才调 LLM，两类节点同图混合；`recursion_limit` 直接对应"最大步数"红线；`interrupt` / `Command(resume=…)` 原生支持人在回路；`SqlSaver` 让长流程跨进程续跑。`AgentExecutor` 是黑盒循环，做不了细粒度拦截。

**Q：为什么 Agent 用 Python，而 MES 主体是 Java？**
A：Agent 是**旁路服务**，不参与过点主事务，最坏情况是"没诊断出来 / 草稿没用上"，不产生写副作用——这降低了跨语言运维代价；而 Python 在 LangGraph、模型 SDK、评测工具上更成熟。跨语言的物理边界反而强化红线：Agent 进程无法共享 Java 事务与内存，天然不能进过点主事务、不能旁路应用服务写路径。两侧只通过 REST + Kafka 契约解耦，复用既有契约不造新管道。

**Q：三项能力共用一套评测会不会互相污染？**
A：不会，评测按**能力维度**切分而不是按"层号"：诊断看根因命中与证据充分性、草稿看字段准确与采纳率、编排看步数正确性与 gate 拒绝率合理区间。共用的是采集与评分框架（同一条 trace 上取过程指标），不是同一个阈值。反过来，共用框架才让"编排里那次诊断"能被单独评分——分级模型下这条链是断的。

**Q：上线了吗？**
A：这是设计阶段的方案，不是已落地。可讲的架构判断是：① 一个 runtime 三图形态，能力=图形态+工具切片，不是三个 Agent；② 写风险由能力声明 + 工具声明 + 运行时 token 三层共管，靠启动断言兜底；③ 判断某步该不该用 LLM 用三问，编排全程 PASS 时 LLM 调用为 0。

---

## 13. 收尾一句话

"我的 Agent 不是三层，是**一个 Agent 的三个功能**：诊断把证据变成假设，草稿把假设变成可下达的处置，编排把处置塞进跨上下文长流程并在关键步停下来等人。三者共用一套运行时、一张工具注册表、一条 trace、一套写闸门——差别只在图的形态和写权限。判断哪一步该用 LLM 用三问，哪一步该写库用三道闸门，边界靠启动断言而不是评审时的口头承诺。"
