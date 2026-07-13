# MES Agent 服务 — 基于 AgentScope 2.0 的架构重设计

> 本文是对 `整体技术选型与模块划分.md` 的一次**技术选型重构**，核心目标：
> **尽可能全面地采用 AgentScope 2.0 的技术与组件替换现有方案**（LangGraph / 自研横切能力 / 自研编排层等），
> 在保持"查 → 诊断 → 草拟 → 推动作"业务链与"写动作闸门在人手里"安全底线不变的前提下，
> 借助 AgentScope 2.0 的生产级基础设施显著提升**多智能体协同、分布式支持与可扩展性**。
>
> 适用读者：架构评审、Agent/RAG 研发、平台/SRE。

---

## 0. 为什么用 AgentScope 2.0 重构

原架构以 **LangGraph** 为编排核心，配套大量**自研横切能力**（成本优化 `cost/`、可观测 `obs/`、长程任务 `longtask/`）与**自研 L3 编排层**（supervisor_graph / code_nodes / agents / scenarios）。这套方案可控性强，但存在三个结构性负担：

1. **横切能力自研成本高**：checkpoint 持久化、interrupt/resume、confirmation gate、token 签发、可观测埋点、成本路由等，都是团队从零搭建并长期维护。
2. **多智能体协同是"手搓图"**：L3 的 supervisor + subgraph、并行 barrier、agent 能力互调，全部靠 StateGraph 边和代码节点显式拼装，扩展一个新场景成本高。
3. **分布式与多租户靠外部拼装**：Pod 重启续跑依赖"同 `thread_id` + MySQL checkpoint"，多租户隔离、沙箱执行、后台任务调度都需自行实现。

AgentScope 2.0（阿里通义实验室，2026-05 发布的 breaking release）恰好把上述"硬骨头"做成了**框架内生能力**：

| 原架构痛点 | AgentScope 2.0 对应能力 |
|-----------|------------------------|
| 自研 checkpoint / interrupt / resume | **AgentState 显式状态 + SessionManager + Redis-backed storage**，无状态水平扩展，任意副本恢复任意会话上下文 |
| 自研 confirmation gate + token 签发 | **Permission 权限系统**：Gate tool execution + Human-in-the-loop confirmation + Agent autonomy control（内生） |
| 自研可观测埋点（obs/） | **Middleware 体系**：`TracingMiddleware`（OTel 内生入口）关注点分离 |
| 自研成本压缩（ResultCompactor） | **Offloader 接口 + `ToolOffloadMiddleware`**：上下文压缩与超大工具结果卸载内生 |
| 自研多智能体编排（supervisor_graph） | **统一 `Agent` 类（事件流）+ Pipeline（sequential/fanout/MsgHub）+ Plan 模块** |
| 自研沙箱/受限写隔离 | **Workspace 抽象**（Local/Docker/E2B）+ WorkspaceManager 多租户隔离 |
| 自研前端推送 + human-in-loop | **事件系统**：统一事件总线，原生服务前端与 human-in-the-loop 协作 |
| 自研评测库对接 | **智能体评测 + OpenJudge 评估器**（内生评测栈） |

> **核心原则不变**：AgentScope 2.0 只替换"如何实现"，不改变"能不能写"。L1 全程只读、L2 只产草稿不落库、L3 受限写 + gate 这三条安全红线，从"自研启动断言"升级为"框架 Permission 系统 + 事件确认"承载，安全强度只增不减。

---

## 1. 整体架构总览

### 1.1 系统全景

```
┌─────────────────────────────────────────────────────────────────────┐
│              前端 / 工位 UI / 责任人卡片（AGUI 事件流实时渲染）        │
└──────────┬──────────────────────┬──────────────────────────────────┘
           │ R1 直查图             │ 事件流（reply_stream / AGUIProtocolMiddleware）
           ▼                      ▼
┌────────────────────┐   ┌──────────────────────────────────────────┐
│   rag-service      │   │        agent-service (Python, AgentScope 2.0)│
│  (AgentScope 2.0)  │◄──┤  统一 Agent 类：L1 诊断 / L2 草稿 / L3 编排 │
│  A/B/E 三路线      │   │  Pipeline 协同 + Permission 闸门 + Workspace │
└─────────┬──────────┘   └──────────────┬───────────────────────────┘
          │ Kafka 事件流投影              │ Tool（httpx）只读 REST + 受限写
          │ (GraphProjector)             │ + Kafka 事件订阅 → 事件系统
          ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│            MES 主体服务 (Java/Spring，已有，不替换)                    │
│   生产执行服务 │ 制造资源服务 │ 设备管理服务 (14 个限界上下文)         │
└─────────────────────────────────────────────────────────────────────┘
          ▲                              ▲
          │      共享底座（跨服务）        │
          └── OpenJudge 评测 + TracingMiddleware(OTel) ──┘
```

### 1.2 三大服务边界（职责不变，实现底座切换为 AgentScope 2.0）

| 服务 | 语言/框架 | 职责定位 | 写权限 |
|------|----------|---------|--------|
| **agent-service** | Python + AgentScope 2.0 | 把"查 → 诊断 → 草拟处置 → 推动作"串成自动链；写闸门由 **Permission 系统**承载 | L1 全程只读；L2 仅产草稿不落库；L3 受限写 + Permission gate |
| **rag-service** | Python + AgentScope 2.0（RAG 模块 / Toolkit） | 把"工程师手动查 5 个界面"变成一次问答；三路线分线落地 | 全程只读 |
| **MES 主体服务** | Java/Spring | 14 个限界上下文核心业务与写路径 | 唯一业务写权限，聚合根不变式 + 事务发件箱 |
| **mes-eval** | Python + AgentScope 评测/OpenJudge | RAG 三路线 + Agent 三层级评测、标定、漂移、CI 门禁 | 只读评测 |

### 1.3 跨语言协作模型（Python Agent ↔ Java MES）

- **物理隔离即安全边界**：Agent 仍是只读旁路，最坏"没诊断出来"，跨语言物理边界天然强制不进过点主事务。
- **解耦方式**：仍只通过 **REST 只读接口 + Kafka 只读事件**解耦；Agent 侧出站 REST 封装为 AgentScope **Tool**。
- **链路串联**：由 **`TracingMiddleware`**（AgentScope 2.0 内生 OTel 入口）在工具调用出站时注入 W3C `traceparent`，Java 侧 OTel agent 续接同一 trace。
- **取舍理由**：Python AI 生态 + AgentScope 2.0 生产级基础设施更成熟；MES 主体为 Java/Spring 不替换，两边职责正交。
- **可选增强**：若未来需将部分能力下沉 JVM 侧，AgentScope 提供 **Java 2.0** 版本（同构 API），可平滑扩展跨语言 Agent，无需换框架心智。

---

## 2. 技术选型总览（AgentScope 2.0 版）

### 2.1 编程语言与框架底座

| 类别 | 选型 | 用途 | 替换/说明 |
|------|------|------|----------|
| 语言 | **Python 3.11+** | agent/rag/eval 主体 | 不变 |
| **Agent 框架** | **AgentScope 2.0** | 统一 Agent 类、Pipeline、Permission、Workspace、Middleware、事件系统、Plan、RAG、Tracing、评测 | **替换 LangGraph 全家桶** |
| Java | 已有 | MES 三大主体服务 | 不替换 |

### 2.2 Agent / LLM 编排（核心替换区）

| 能力域 | 原方案 | AgentScope 2.0 新方案 | 替换理由 |
|--------|--------|----------------------|---------|
| 单 Agent 推理循环 | LangGraph StateGraph ReAct | **统一 `Agent` 类**（`reply_stream` 流式 + `reply`），内置 ReAct | 事件流原生支持权限检查 / HITL / 前端集成，不再手搓 StateGraph |
| 多 Agent 协同 | supervisor_graph + subgraph 手搓 | **Pipeline**（`sequential` / `fanout` / **`MsgHub`** 广播）+ **Handoffs/Routing** | 协同变声明式；MsgHub 让多 agent 共享消息上下文，扩展新协同零改图 |
| 任务规划 | code_nodes/plan.py 自研 | **Plan 模块** + `TaskCreate/Get/List/Update` 工具 | 规划/任务分解内生，支持长链路任务拆解与跟踪 |
| 状态/会话 | 自研 + LangGraph checkpoint | **`AgentState` 显式状态 + `SessionManager`** | 显式类型化状态，无状态水平扩展，副本可恢复 |
| 结构化数据 | Pydantic v2（with_structured_output） | **Content Block（Pydantic BaseModel）** + `Msg`（含 `usage`/时间戳） | 消息与内容块原生 Pydantic 化，天然可校验/序列化/溯源（block `id`） |
| 工具 | 自研 ACL client + LangGraph tool | **Toolkit / Tool + MCP + 智能体技能(Skills)** | 工具体系标准化，支持 MCP 生态接入与技能封装 |

> **不再选 LangGraph**：其 checkpoint / interrupt / resume / 手搓图协同的成本，已被 AgentScope 2.0 的 Session/Permission/Pipeline 内生能力覆盖；同时获得沙箱、多租户、事件系统等 LangGraph 不提供的生产能力。
> **仍保留的判断**：不用 AutoGen（conversational 不适合严格步骤编排）、CrewAI（抽象偏高难做细粒度权限）。AgentScope 2.0 在"可控性 + 生产化"上同时优于两者。

### 2.3 大模型（模型可插拔，选型不变）

| 模型 | 用途 |
|------|------|
| Claude（如 claude-sonnet 级） | 主力推理（L1 诊断 / L2 草拟 / L3 能力 A/B/C/D） |
| 通义千问（DashScope） | 多 provider 之一 + AgentScope 原生适配，观测一致性验证 |
| DeepSeek / Haiku / 本地小模型 | Router 分类、cascading 降级、便宜分类器候选 |
| Claude Opus 级 | OpenJudge LLM-as-judge 裁判（与被测不同族） |
| Embedding 模型 | 文档型 RAG 向量化（经 AgentScope **Embedding** 模块接入） |

> 通过 AgentScope **Model** 抽象统一切换 provider（DashScope/Ollama/OpenAI 兼容等）；任何模型降级仍须过评测门禁。

### 2.4 Web / HTTP / 异步 / 服务化

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **AgentScope FastAPI Agent Service** | HTTP 入口 + lifespan-scoped `SessionManager`/`SchedulerManager`/`BackgroundTaskManager` | **替换自研 FastAPI 编排 + 自研长程任务点火**；开发与部署一体（Runtime 合并入主库） |
| **httpx（封装为 Tool）** | 出站只读 REST / 受限写，`traceparent` 由 Middleware 注入 | 保留 httpx，封装形态改为 AgentScope Tool |
| **asyncio** | 协程驱动流式 reply 与后台任务 | 不变，但由 `BackgroundTaskManager` 托管 |
| uvicorn + gunicorn / K8s | 部署为独立微服务 | 不变，天然无状态水平扩展 |

### 2.5 执行环境与隔离（新增能力域）

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **Workspace 抽象** | 业务逻辑与执行环境解耦：`LocalWorkspace` / `DockerWorkspace` / `E2BWorkspace`，共享同一 agent-facing API | **新增**：受限写/代码执行/工具运行在隔离环境，"一次编写到处运行" |
| **WorkspaceManager** | `Local/Docker/E2BWorkspaceManager`，agent-level isolation | **替换自研租户隔离**：面向多租户服务的资源分配 |
| **内置权限工具** | Bash/Edit/Glob/Grep/Read/Write 均带权限控制 | 高危操作内生受控（L3 场景下的文件/脚本类动作） |

### 2.6 存储

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **Redis** | **Redis-backed storage**（`SessionManager` 会话状态 / 多租户资源）；ConfirmationStore 语义并入 Permission；工具结果缓存、限流 | 会话持久化从 MySQL checkpoint 三表迁移到 AgentScope Session store |
| **MySQL** | ① 业务可观测表（`tool_call_trace`/`llm_call_log`/`diagnosis_session`/`l3_session` 等）；② 评测表族（`mes_eval` schema）；③ `AgentState` 归档/审计（可选） | **不再需要 LangGraph SqlSaver 三表**；MySQL 退回"业务证据链 + 评测 + 审计" |
| **Neo4j** | 追溯图投影（属性图 + Cypher），`GraphProjector` 订阅 Kafka 构建 | 不变 |
| **向量库** | 文档型 RAG 向量检索（路线 B），经 AgentScope **RAG/Embedding** 模块接入 | 检索接入层改由 AgentScope RAG 模块承载 |
| **MinIO** | 对象存储（基础设施） | 不变 |
| **SQLAlchemy 2.0 (async) + asyncmy** | MySQL ORM（业务表/评测表） | 不变 |

### 2.7 消息 / 事件

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **AgentScope 事件系统** | 统一事件总线：服务前端 agent 应用 + human-in-the-loop 协作；`reply_stream` 产出 agent events | **替换自研 WebSocket/SSE 推送编排** |
| **`AGUIProtocolMiddleware`** | 流式传输到前端（动作卡实时渲染） | 前端实时推送内生化 |
| **Kafka (`aiokafka`)** | ① 领域事件订阅（`ProcessRouteActivated`/`equipment.fault` 主动触发）→ 桥接进事件系统；② 动作卡持久推送 topic（离线兜底）；③ RAG 侧 `GraphProjector` 订阅 | 保留 Kafka 作为跨服务事件总线；进程内协同改用 AgentScope 事件系统 |

### 2.8 可观测

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **`TracingMiddleware`** | OTel tracing 新入口（从 Agent 类移出，关注点分离） | **替换自研 `obs/Tracing`**；埋点内生、provider 无关 |
| **OTel Collector / W3C traceparent** | 跨语言链路串联（Python→Java） | 不变，注入点改为 Middleware |
| prometheus-client / structlog | 指标埋点 + 结构化日志（`trace_id`/`span_id`） | 保留，作为 Middleware 之外的补充埋点 |
| Tempo/Jaeger、Prometheus、Loki、Grafana | trace/指标/日志存储与看板 + SLI/SLO 告警 | 不变 |
| **AgentScope Studio** | 开发期可视化调试、事件/追踪查看 | **新增**：研发期观测提效 |

### 2.9 评测

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **AgentScope 智能体评测 + OpenJudge 评估器** | 内生评测框架 + LLM-as-judge 评估器 | **替换/收敛自研 JudgeService + Ragas/DeepEval 适配** |
| pytest | 回归脚本、CI 触发 | 保留，包裹 AgentScope 评测 runner |
| Platt scaling / isotonic / PSI / kappa | 置信度标定 / 漂移检测 / judge 一致性 | 保留（作为 OpenJudge 之上的统计层） |

### 2.10 横切能力：从"自研三大子模块"到"框架内生"

| 原横切子模块 | 原实现 | AgentScope 2.0 替换 |
|-------------|--------|---------------------|
| `infrastructure/cost/` 成本优化 | ModelRouter/CacheControl/ResultCompactor/EarlyStop… | **Offloader + `ToolOffloadMiddleware`**（上下文压缩/超大结果卸载）+ Model 抽象分层路由 + 自定义 Middleware（缓存/早停） |
| `infrastructure/obs/` 可观测 | ObservabilityContext/Tracing/MetricsCollector… | **`TracingMiddleware`** + Middleware 链 + Studio |
| `infrastructure/longtask/` 长程任务 | L3Orchestrator/SqlSaver/GateManager/ConfirmationStore… | **SessionManager + AgentState + Permission(gate) + BackgroundTaskManager + SchedulerManager** |

> 关键收益：三大横切能力从"团队自研+长期维护"变成"框架内生+自定义 Middleware 扩展"，团队只写**业务专属 Middleware**（如 MES 脱敏 `Redactor`、成本预算拦截），底座由框架保证。

---

## 3. 模块划分

### 3.1 `agent-service` 模块结构（AgentScope 2.0 重构）

agent-service 仍是单一 Python 微服务，L1/L2/L3 共存；但编排层从"自研 StateGraph + code_nodes"改为"AgentScope Agent + Pipeline + Middleware + Permission"。

```
agent_service/app/
├── service/                      # AgentScope FastAPI Agent Service 入口
│   ├── app.py                    #   lifespan 装配 SessionManager/SchedulerManager/
│   │                             #   BackgroundTaskManager/WorkspaceManager
│   └── routes/                   #   diagnose / draft / l3 / schedule 路由
│
├── agents/                       # 统一 Agent 类实例（reply_stream 事件流）
│   ├── diagnosis_agent.py        # L1: 只读 ReAct 诊断 Agent（Permission=只读）
│   ├── draft_agents.py           # L2: 草稿 Agent（返工单/8D/SOP，NoWrite）
│   └── l3/                       # L3: 编排型 Agent 能力集
│       ├── root_cause_agent.py         # A 根因推理 + 处置卡
│       ├── fault_impact_agent.py       # B 故障隔离范围判定
│       ├── traceability_agent.py       # C 客诉追溯（复用 L1 诊断 Agent）
│       └── draft_agent.py              # D SOP/8D/返工工艺草拟
│
├── pipelines/                    # 多智能体协同编排（声明式，替换 supervisor_graph）
│   ├── changeover.py             # ① 换线：sequential + gate
│   ├── fault_response.py         # ② 设备故障复产：fanout 并行 + MsgHub 汇合
│   ├── complaint_8d.py           # ③ 客诉 8D：sequential（诊断→追溯→草拟）
│   └── process_change.py         # ④ 工艺变更落地
│
├── plans/                        # Plan 模块：长链路任务分解与跟踪
│   └── l3_plan.py                #   TaskCreate/Get/List/Update 驱动
│
├── middleware/                   # 自定义 Middleware（业务横切）
│   ├── redactor_mw.py            #   MES 脱敏（序列号/批次/PII）
│   ├── cost_budget_mw.py         #   预算拦截 + 结果压缩策略
│   ├── early_stop_mw.py          #   证据充分性早停
│   └── metrics_mw.py             #   业务指标埋点（补充 TracingMiddleware）
│
├── permissions/                  # Permission 策略（替换三层启动断言）
│   ├── readonly_policy.py        #   L1：拒绝任何非只读工具
│   ├── nowrite_policy.py         #   L2：不持写 client，requires_confirmation 恒 True
│   └── gated_write_policy.py     #   L3：写工具 gate + HITL confirmation
│
├── tools/                        # Toolkit：工具 + MCP + Skills
│   ├── acl/                      #   对 MES 上下文 + RAG 的只读/受限写工具
│   │   ├── readonly.py           #     过点/工艺/物料/设备/质量只读
│   │   ├── restricted_write.py   #     tooling/telemetry/rework 受限写
│   │   └── rag.py                #     fetch_subgraph_nodes / search_docs
│   └── task_tools.py             #   Plan 任务管理工具封装
│
├── infrastructure/
│   ├── model/                    # AgentScope Model 抽象（provider 可插拔）
│   ├── session/                  # SessionManager + Redis-backed storage 适配
│   ├── workspace/                # WorkspaceManager（Local/Docker/E2B）配置
│   ├── kafka/                    # aiokafka ↔ 事件系统桥接
│   ├── persistence/             # SQLAlchemy：业务可观测表 + 评测/审计
│   └── obs/                      # TracingMiddleware/Studio 接线 + prometheus/structlog
│
├── config.py                     # pydantic-settings
└── main.py                       # 启动装配 + Permission 策略校验
```

**对外接口汇总（端点不变，实现底座切换）：**

| 端点 | 层级 | 说明 |
|------|------|------|
| `POST /agent/diagnose` | L1 | 发起诊断，返回 `DiagnosisReport`（含 `subgraph_ref`）；内部 `reply_stream` 可流式 |
| `POST /agent/draft` | L2 | 草拟处置，返回 `Draft`（`requires_confirmation=True`） |
| `GET /agent/draft/{id}/evidence` | L2 | 回溯草稿证据 |
| `POST /agent/l3/{scenario}/start` | L3 | 启动 Pipeline（scenario ∈ changeover/fault_response/complaint_8d/process_change） |
| `POST /agent/l3/{session_id}/confirm` | L3 | Permission gate 确认，由事件系统触发续跑（替换 `Command(resume=token)`） |
| `GET /agent/l3/{session_id}/events` | L3 | 事件流订阅（AGUI），动作卡实时推送 |

### 3.2 `rag-service` 模块结构（三路线，接入 AgentScope RAG/Toolkit）

| 路线 | 子模块 | 职责 | 技术形态（AgentScope 2.0 版） |
|------|--------|------|------------------------------|
| **A 追溯型** | `RAG服务/追溯型 RAG/` | 全链路追溯，5M1E 根因串联 | GraphRAG + Neo4j Cypher + 事件流预投影，封装为 **Tool** |
| **B 文档型** | `RAG服务/文档型 RAG/` | SOP/手册/标准/8D 检索 | AgentScope **RAG 模块 + Embedding** + 版本过滤 + 事件驱动重索引 |
| **E Agentic RAG** | `RAG服务/Agentic RAG/` | 统一入口路由 A/B | **统一 Agent 类 + Routing/Handoffs + Toolkit 工具选择** |

> 路线 E 由 AgentScope 的 **Routing / Handoffs** workflow 天然承载，替换原自研路由逻辑。引入顺序不变：B -> A -> E。

### 3.3 `mes-eval` 评测库模块结构（对接 OpenJudge）

| 组件 | 职责 | AgentScope 2.0 对接 |
|------|------|---------------------|
| **EvalRunner** | 离线跑集 + 三层判定（断言/指标/judge） | 基于 AgentScope **智能体评测** runner 封装 |
| **JudgeService** | LLM-as-judge，按 MES rubric 判质量 | 基于 **OpenJudge 评估器** |
| **ShadowRunner** | 在线影子采样对比（5% 流量） | 复用事件系统采样 |
| **CalibrationService / DriftDetector** | 置信度标定（ECE/Platt/isotonic）/ 漂移（PSI） | 统计层，OpenJudge 之上 |
| **SafetyChecker / VersionAnchorChecker** | 安全红线硬断言 / 版本锚点比对 | 保留（MES 专用硬门禁） |
| **CIGate** | CI 门禁（hard/soft gate） | 保留 |
| **EvalTarget 适配器** | 6 个被测对象各一适配器 | 适配到 AgentScope Agent/Tool 接口 |

### 3.4 模块间集成点与接口契约

| 集成点 | 接口/契约 | 方向 | 说明 |
|--------|----------|------|------|
| L1 调图 | `query_traceability_graph` Tool（封装 `POST /rag/trace/query`） | Agent → RAG | 注册在 L1 Toolkit **首位**，system prompt 引导"先调图" |
| L1 降级 REST | 各上下文只读 REST Tool | Agent → MES | 图覆盖不足时降级补齐 |
| L2 回查图 | `fetch_subgraph_nodes(subgraph_ref)` Tool | Agent → RAG | 按 L1 透传 `subgraph_ref` 回查，**不重查图** |
| L2 调文档 RAG | `search_docs(query, route_version_filter)` Tool | Agent → RAG | 8D/SOP 草拟检索历史同类 |
| L3 受限写 | ACL 写 Tool → 应用服务 REST（header `X-Confirmation-Token` + `X-Confirmed-By`） | Agent → MES | **由 Permission gate 放行后**才可调用，走聚合根不变式 + 事务发件箱 |
| 图投影 | Kafka 领域事件流 | MES → RAG | `GraphProjector` 订阅构建 Neo4j |
| 主动触发 | `ProcessRouteActivated`/`equipment.fault` → 事件系统 | MES → Agent | 触发 L2 异步草拟 / L3 故障复产 |
| 动作卡推送 | 事件系统 + `AGUIProtocolMiddleware` + Kafka 兜底 | Agent → 前端 | 实时 + 离线双通道 |
| 评测接入 | EvalTarget 适配器 | mes-eval → Agent/RAG | 统一跑集判定 |
| 跨语言追踪 | W3C `traceparent` | Agent → MES | 由 `TracingMiddleware` 注入 |

**版本一致性三段传递链**（核心安全契约，不变）：
```
图 SNAPSHOT_OF_ROUTE{route_version} → L1 evidence.route_version → L2 Draft.route_version → MES 应用服务校验 ACTIVE
```

---

## 4. Agent 分层架构（能力映射到 AgentScope 2.0）

### 4.1 自主度光谱（L0-L4，核心落地 L1/L2/L3）

| 层级 | 名称 | AgentScope 2.0 承载 | Permission 策略 |
|------|------|---------------------|----------------|
| L0 | 收口型问答 | Routing/Handoffs Agent → RAG 工具 | 只读 |
| **L1** | **诊断型** | 单 `Agent`（ReAct 事件流），多步只读推理 | `readonly_policy` |
| **L2** | **草稿型** | `Agent` + 策略化 draft agents，产草稿不落库 | `nowrite_policy`，`requires_confirmation` 恒 True |
| **L3** | **编排型** | **Pipeline（sequential/fanout/MsgHub）+ Plan + 多 Agent 协同** | `gated_write_policy` + HITL |
| L4 | 自治型 | 无人全自动 | 不建议 |

### 4.2 层间数据流

```
L1 诊断 Agent → DiagnosisReport + subgraph_ref
              ↓（AgentState 透传）
L2 草稿 Agent → 按 subgraph_ref 回查图节点 → Draft（requires_confirmation=True）
              ↓ 人确认（MES 正式界面）
          MES 正式应用服务落库（过聚合根不变式 + 事务发件箱）

L3 Pipeline → sequential/fanout 调度确定性步骤 + 多 Agent 能力 A/B/C/D（MsgHub 共享上下文）
              ↓ 非确定分支触发 Agent → ActionCard
              ↓ Permission gate（拦截 → 事件系统 HITL 确认 → 续跑）
          MES 正式应用服务落库
```

### 4.3 L3 "代码 + agent 混合编排" 判定标准（原则不变，落点更清晰）

一个步骤是否需要 Agent，看三问：**输入是否开放？是否需要推理/生成？分支是否难以穷举？**
- 三问皆否 → **Pipeline 确定性节点/普通函数**（不调 LLM）
- 三问有一 → **AgentScope `Agent` 节点**（调 LLM）

> 换线全程 PASS 时 Agent 节点不触发，**LLM 调用为 0**。"代码能做的不交给 LLM"仍是核心原则；Pipeline 让确定性步骤与 Agent 步骤在同一编排里清晰共存。

### 4.4 写动作的三层防线（从"启动断言"升级为"Permission 系统"）

| 层级 | 防线机制 | AgentScope 2.0 实现 |
|------|---------|---------------------|
| L1 | 全程只读 | `readonly_policy`：Permission 拒绝任何非只读工具执行 |
| L2 | 不持写 client | `nowrite_policy`：Permission 禁止写工具注册；`requires_confirmation` 恒 True |
| L3 | 写工具须 gate + HITL | `gated_write_policy`：**Gate tool execution + Human-in-the-loop confirmation**，落库走应用服务过聚合根不变式 + 事务发件箱 |

> Agent 全程不碰 MES 原始表，写路径与人工下达完全一致；触发源从"人点按钮"变为"Agent 草拟 + Permission gate 确认"。相较原自研启动断言，Permission 系统把闸门做进了**每一次工具执行**，粒度更细、更难绕过。

---

## 5. 关键设计决策汇总（重构版）

| # | 决策 | 核心权衡 |
|---|------|---------|
| 1 | **用 AgentScope 2.0 统一 Agent 类替代 LangGraph StateGraph** | 事件流原生支持权限/HITL/前端集成；省去手搓图与 checkpoint 自研 |
| 2 | **多智能体协同用 Pipeline（sequential/fanout/MsgHub）替代 supervisor_graph** | 协同声明式、可组合；MsgHub 共享上下文，扩展新场景零改图 |
| 3 | **写闸门用 Permission 系统替代三层启动断言** | 闸门下沉到每次工具执行，Gate + HITL 内生，粒度更细 |
| 4 | **长程任务用 SessionManager + AgentState + BackgroundTaskManager 替代自研 SqlSaver/Celery/interrupt** | 无状态水平扩展，任意副本恢复任意会话；不再自研 checkpoint 三表 |
| 5 | **可观测用 TracingMiddleware 替代自研 obs/** | OTel 内生入口、关注点分离；配 Studio 提效研发期观测 |
| 6 | **成本优化用 Offloader + ToolOffloadMiddleware + Model 分层 替代自研 cost/** | 超大结果卸载/上下文压缩内生；便宜模型降级仍须过评测门禁 |
| 7 | **执行隔离用 Workspace（Local/Docker/E2B）+ WorkspaceManager** | 受限写/代码执行沙箱化；多租户 agent-level isolation 内生 |
| 8 | **前端推送用事件系统 + AGUIProtocolMiddleware 替代自研 WS/SSE 编排** | 动作卡实时渲染 + HITL 确认同源，Kafka 仍作离线兜底 |
| 9 | **评测用智能体评测 + OpenJudge 替代自研 judge + Ragas/DeepEval** | 评测栈内生；MES 安全硬门禁/版本锚点作为其上的专用层保留 |
| 10 | **RAG 路线 E 用 Routing/Handoffs 承载** | 统一入口路由内生，替换自研路由 |
| 11 | **消息/内容用 Content Block（Pydantic）+ Msg（usage/时间戳/block id）** | 天然可校验/序列化/计费/溯源，替代零散结构化输出 |
| 12 | **跨语言仍 Python Agent + Java MES；预留 AgentScope Java 2.0 同构扩展** | 物理边界安全不变；未来跨语言扩展无需换框架心智 |
| 13 | **安全红线不变、承载升级**（L1 只读/L2 不持写/L3 gated write） | "能不能写"的语义不变，"如何强制"从自研升级为框架内生 |
| 14 | **版本锚定 + 三段传递链完全保留** | MES 追溯的失效工艺红线是业务硬约束，与框架无关 |

---

## 6. 新架构如何提升三大能力

### 6.1 多智能体协同能力

- **统一 Agent 类 + 事件流**：每个 Agent 是 pure producer，协同各方通过事件总线交互，天然支持并发与实时干预。
- **Pipeline 三形态**：`sequential`（诊断→追溯→草拟串行）、`fanout`（故障复产多能力并行）、**`MsgHub`**（多 Agent 广播共享上下文，如 A 根因 + B 隔离范围互相看到彼此结论）。
- **Handoffs/Routing**：L0/E 入口按问题类型移交给专精 Agent，协同从"手搓边"变为"声明式移交"。
- **Plan 模块**：长链路场景（工艺变更落地）用 Plan 拆解任务、`Task*` 工具跟踪进度，多 Agent 认领子任务。

### 6.2 分布式支持

- **无状态水平扩展**：`AgentState` 显式化 + `SessionManager`（Redis-backed）持久会话，**任意副本恢复任意用户完整上下文**——Pod OOM/滚动更新后新副本按 `session_id` 续跑，无需自研 checkpoint。
- **BackgroundTaskManager + SchedulerManager**：长程任务与定时巡检由框架托管，替代自研 asyncio 点火与 Celery。
- **Workspace 分布式形态**：同一份业务代码按需切换 Local→Docker→E2B，执行环境可独立扩缩容。

### 6.3 可扩展性

- **Middleware 链**：新增横切能力（脱敏/预算/审计/限流）= 加一个 Middleware，不侵入 Agent 业务逻辑。
- **Toolkit + MCP + Skills**：新增工具/接入外部 MCP 服务/封装领域技能，均标准化；工具边界仍对齐 14 个限界上下文。
- **Permission 策略即配置**：新增写场景 = 声明一条 gated 策略，闸门自动生效。
- **Model 抽象**：新增/替换 provider 无需改业务；降级过评测门禁即可上线。
- **Pipeline 可组合**：新增 L3 场景 = 组装已有 Agent + 确定性节点，复用度高。

---

## 7. 引入路线（迁移视角）

| 阶段 | 交付内容 | 说明 |
|------|---------|------|
| **第 0 步** | 底座切换 | 引入 AgentScope 2.0，搭 FastAPI Agent Service + SessionManager + TracingMiddleware，跑通空壳 |
| **第 1 步** | L1 诊断迁移 | 将 StateGraph ReAct 重写为统一 Agent + Toolkit + `readonly_policy`；对齐评测基线 |
| **第 2 步** | L2 草稿迁移 | draft agents + `nowrite_policy`；证据回查 Tool 化 |
| **第 3 步** | 横切能力替换 | obs→TracingMiddleware、cost→Offloader/Middleware、longtask→Session/Permission |
| **试点** | L3 换线编排 | Pipeline(sequential)+Permission gate+事件系统 HITL，confirmation 做扎实 |
| **扩展** | L3 其余场景 | fanout/MsgHub 承载故障复产/客诉 8D/工艺变更 |
| **收口** | L0 统一入口 + E Agentic RAG | Routing/Handoffs 收口到 RAG 工具 + L1-L3 能力 |

> 迁移策略：**端点契约不变**（`/agent/diagnose` 等），每层迁移后用 mes-eval（OpenJudge）对齐旧基线，通过评测门禁才切流量，保证平滑替换、可回滚。

---

## 8. 附录：AgentScope 2.0 能力 → 本架构落点速查

| AgentScope 2.0 能力 | 本架构落点 |
|---------------------|-----------|
| 统一 `Agent` 类（reply_stream/reply） | L1/L2/L3 各 Agent |
| Content Block / `Msg`（Pydantic, usage, block id） | 消息与证据溯源、计费 |
| Permission 系统（Gate/HITL/autonomy） | 写动作三层防线 |
| Middleware（Tracing/AGUI/ToolOffload + 自定义） | 可观测/前端流/成本/脱敏/审计 |
| Pipeline（sequential/fanout/MsgHub）+ Routing/Handoffs | L3 编排 + L0/E 入口 |
| Plan + Task 工具 | 长链路任务分解跟踪 |
| SessionManager + AgentState（Redis-backed） | 分布式会话恢复、长程任务 |
| BackgroundTaskManager / SchedulerManager | 后台任务 / 定时巡检 |
| Workspace（Local/Docker/E2B）+ WorkspaceManager | 执行隔离 + 多租户 |
| Toolkit + MCP + Skills | ACL 工具 / 外部服务 / 领域技能 |
| Model + Embedding | provider 可插拔 + RAG 向量化 |
| RAG 模块 | 文档型 RAG（路线 B） |
| 智能体评测 + OpenJudge | mes-eval 评测栈 |
| Tracing + Studio | 生产可观测 + 研发调试 |

---

## 9. 附录：源文档索引

- `整体技术选型与模块划分.md` — 原（LangGraph 版）总览，本文的重构基线
- `AGENT服务/` — L1/L2/L3 分层与三大横切能力设计
- `RAG与Agent协同/`、`RAG与Agent评测/`、`RAG服务/` — 协同、评测、三路线
- `领域模型/`、`实现说明/` — MES 主体（Java）14 个限界上下文与基础设施

> 说明：本文为**技术选型重设计稿**，AgentScope 2.0 组件名以官方文档为准（统一 Agent 类、Content、Permission、Middleware、Pipeline、Plan、Session、Workspace、Toolkit/MCP/Skills、Model/Embedding、RAG、Tracing/Studio、智能体评测/OpenJudge）。落地前建议对齐目标版本的 API 细节。
