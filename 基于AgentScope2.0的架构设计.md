# MES Agent 服务 — 基于 AgentScope 2.0 的架构

> 本文是本仓库 **Agent 侧技术选型的权威口径源**：`整体技术选型与模块划分.md`、`AGENT服务/`、`RAG服务/`、`RAG与Agent评测/` 的编排与框架表述一律以本文为准。
> 核心取向：**尽可能全面地采用 AgentScope 2.0 的技术与组件替换现有方案**（LangGraph / 自研横切能力 / 自研编排层等），
> 在保持"查 -> 诊断 -> 草拟 -> 推动作"业务链与"写动作闸门在人手里"安全底线不变的前提下，
> 借助 AgentScope 2.0 的生产级基础设施显著提升**多智能体协同、分布式支持与可扩展性**。
>
> 适用读者：架构评审、Agent/RAG 研发、平台/SRE。

---

## 0. 为什么用 AgentScope 2.0 重构

原架构以 **LangGraph** 为编排核心，配套大量**自研横切能力**（成本优化 `cost/`、可观测 `obs/`、长程任务 `longtask/`）与**自研 流程编排层**（supervisor_graph / code_nodes / agents / scenarios）。这套方案可控性强，但存在三个结构性负担：

1. **横切能力自研成本高**：checkpoint 持久化、interrupt/resume、confirmation gate、token 签发、可观测埋点、成本路由等，都是团队从零搭建并长期维护。
2. **多智能体协同是"手搓图"**：编排能力的 supervisor + subgraph、并行 barrier、agent 能力互调，全部靠 StateGraph 边和代码节点显式拼装，扩展一个新场景成本高。
3. **分布式与多租户靠外部拼装**：Pod 重启续跑依赖"同 `thread_id` + MySQL checkpoint"，多租户隔离、沙箱执行、后台任务调度都需自行实现。

AgentScope 2.0（阿里通义实验室，2026-05 发布的 breaking release）恰好把上述"硬骨头"做成了**框架内生能力**：

| 原架构痛点 | AgentScope 2.0 对应能力 |
|-----------|------------------------|
| 自研 checkpoint / interrupt / resume | **`AgentState` 显式状态 + `SessionService` + Redis-backed storage**，无状态水平扩展，任意副本恢复任意会话上下文（`SessionService` 是 2.0.5 起的正式名，旧名 `SessionManager` 已失效） |
| 自研 confirmation gate + token 签发 | **Permission 权限系统**：Gate tool execution + Human-in-the-loop confirmation + Agent autonomy control（内生） |
| 自研可观测埋点（obs/） | **Middleware 体系**：`TracingMiddleware`（OTel 内生入口）关注点分离 |
| 自研成本压缩（ResultCompactor） | **Offloader 接口 + `ToolOffloadMiddleware`**：上下文压缩与超大工具结果卸载内生 |
| 自研多智能体编排（supervisor_graph） | **统一 `Agent` 类（事件流）为主干 + 应用层 `asyncio.gather` 并发多个独立 `Agent.reply()` + `TaskCreate/Get/List/Update` 任务工具** ⚠️ 2.0 已删除 `MsgHub` / `sequential_pipeline` / `fanout_pipeline`，仅剩 experimental 的 `GoalPipeline` / `TeamPipeline`，且 `TeamPipeline` 文档明写"成员间不互相说话"——不进生产路径 |
| 自研沙箱/受限写隔离 | **Workspace 抽象**（Local/Docker/E2B）+ WorkspaceManager 多租户隔离 |
| 自研前端推送 + human-in-loop | **事件系统**：统一事件总线，原生服务前端与 human-in-the-loop 协作 |
| 自研评测库对接 | **`py-openjudge`（独立包）作 LLM-as-judge 评估器 + 自研 `EvalRunner` 与 CI 门禁** ⚠️ `agentscope.evaluate` 在 2.0 已删除且未回归，**不存在"内生评测栈"** |

> **核心原则不变**：AgentScope 2.0 只替换"如何实现"，不改变"能不能写"。诊断能力全程只读、草稿能力只产草稿不落库、编排能力受限写 + gate 这三条安全红线，从"自研启动断言"升级为"**工具网关的一次性动作令牌（主闸）+ 框架 Permission 规则与事件确认（叠加层）**"承载，安全强度只增不减。
> **为什么不把 Permission 当唯一主闸**：Permission 判定发生在 Agent 循环内，绕开循环（直接调工具函数/直接发 httpx）即失效；令牌绑定动作与一次性消费在网关强制，与调用方是否走 Agent 无关。见 ADR-04。

---

## 1. 整体架构总览

### 1.1 系统全景

```
┌─────────────────────────────────────────────────────────────────────┐
│              前端 / 工位 UI / 责任人卡片（AGUI 事件流实时渲染）        │
└──────────┬──────────────────────┬──────────────────────────────────┘
           │ R1 统一 RAG 查询      │ 事件流（reply_stream / AGUIProtocolMiddleware）
           ▼                      ▼
┌────────────────────┐   ┌──────────────────────────────────────────┐
│   rag-service      │   │        agent-service (Python, AgentScope 2.0)│
│  (AgentScope 2.0)  │◄──┤  统一 Agent 类：诊断 / 草稿 / 流程编排 │
│ 文档检索+追溯+编排 │   │  asyncio 并发 + Permission 闸门 + Workspace │
└─────────┬──────────┘   └──────────────┬───────────────────────────┘
          │ Kafka 事件同步读模型          │ Tool（httpx）只读 REST + 受限写
          │ + MES 只读 REST              │ + Kafka 事件订阅 -> 事件系统
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
| **agent-service** | Python + AgentScope 2.0 | 把"查 -> 诊断 -> 草拟处置 -> 推动作"串成自动链；写闸门由 **Permission 系统**承载 | 诊断能力全程只读；草稿能力仅产草稿不落库；编排能力受限写 + Permission gate |
| **rag-service** | Python + AgentScope 2.0（RAG 模块 / Toolkit） | 一个统一 RAG，组合文档检索、非图制造追溯与 Agentic 检索编排 | 全程只读 MES；仅写 RAG 自有索引、读模型与审计 |
| **MES 主体服务** | Java/Spring | 14 个限界上下文核心业务与写路径 | 唯一业务写权限，聚合根不变式 + 事务发件箱 |
| **mes-eval** | Python + `py-openjudge` + 自研 `EvalRunner` | 统一 RAG 的检索/追溯/编排/端到端评测 + Agent 三层级评测、标定、漂移、CI 门禁 | 只读评测 |

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
| 语言 | **Python 3.12+** | agent/rag/eval 主体 | 与代码仓 `pyproject.toml` 的 `requires-python = ">=3.12"` 对齐 |
| **Agent 框架** | **AgentScope 2.0**（最新 2.0.11，2026-10-27） | 已核实可用：统一 `Agent` 类、`Toolkit`/Tool/MCP/Skills、Content Block + `Msg`、Permission、Workspace、Middleware、事件系统、`AgentState`/`SessionService`、`BackgroundTaskManager`/`SchedulerManager`、RAG、Tracing。**不可用**：Pipeline 协同原语（2.0 已删）、内生评测（`agentscope.evaluate` 已删）、Studio（已归档） | **替换 LangGraph 全家桶**（图编排能力除外，见 §2.2） |
| Java | 已有 | MES 三大主体服务 | 不替换 |

### 2.2 Agent / LLM 编排（核心替换区）

| 能力域 | 原方案 | AgentScope 2.0 新方案 | 替换理由 |
|--------|--------|----------------------|---------|
| 单 Agent 推理循环 | LangGraph StateGraph ReAct | **统一 `Agent` 类**（`reply_stream` 流式 + `reply`），内置 ReAct | 事件流原生支持权限检查 / HITL / 前端集成，不再手搓 StateGraph |
| 多 Agent 协同 | supervisor_graph + subgraph 手搓 | **单 `Agent` + `Toolkit` 为主干；确需并行时用应用层 `asyncio.gather` 并发多个独立 `Agent.reply()`，结论在应用层显式合并；跨能力共享上下文经 `evidence_set_id` 显式传递，不共享消息流** | ⚠️ 2.0 已删 `MsgHub` / `sequential_pipeline` / `fanout_pipeline`，"多 Agent 广播共享上下文"在 2.0 **无原语支撑**；且 MES 写单线程 + 只读旁路的形态本就不需要广播式协同 |
| 任务规划 | code_nodes/plan.py 自研 | **`TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` 工具 + `Task`/`TaskContext` + `AgentState`** | 任务分解与跟踪以**工具**形态内生；⚠️ 不存在独立"Plan 模块"，长链路规划的持久化与重规划仍属应用层职责 |
| 状态/会话 | 自研 + LangGraph checkpoint | **`AgentState` 显式状态 + `SessionService`** | 显式类型化状态，无状态水平扩展，副本可恢复；⚠️ 2.0.5 起 `SessionManager` 已改名 `SessionService`，旧名不可用 |
| 结构化数据 | Pydantic v2（with_structured_output） | **Content Block（Pydantic BaseModel）** + `Msg`（含 `usage`/时间戳） | 消息与内容块原生 Pydantic 化，天然可校验/序列化/溯源（block `id`） |
| 工具 | 自研 ACL client + LangGraph tool | **Toolkit / Tool + MCP + 智能体技能(Skills)** | 工具体系标准化，支持 MCP 生态接入与技能封装 |

> **不再选 LangGraph**：其 checkpoint / interrupt / resume 的成本，已被 AgentScope 2.0 的 `SessionService` / Permission / Workspace 内生能力覆盖；同时获得沙箱、多租户、事件系统等 LangGraph 不提供的生产能力。
> **诚实边界**：LangGraph 的**图编排本身没有被框架替换**——2.0 删掉了 `pipeline` 三原语，编排形状改由应用层 Python 代码（顺序 `await` + `asyncio.gather` + 显式合并）承担，跨请求/跨天的持久编排归工作流引擎。见 [架构设计v2/00-架构决策记录ADR.md](架构设计v2/00-架构决策记录ADR.md) ADR-05。
> **仍保留的判断**：不用 AutoGen（conversational 不适合严格步骤编排）、CrewAI（抽象偏高难做细粒度权限）。AgentScope 2.0 在"可控性 + 生产化"上同时优于两者。

### 2.3 大模型（模型可插拔，选型不变）

| 模型 | 用途 |
|------|------|
| Claude（如 claude-sonnet 级） | 主力推理（诊断 / 草稿草拟 / 非确定能力节点） |
| 通义千问（DashScope） | 多 provider 之一 + AgentScope 原生适配，观测一致性验证 |
| DeepSeek / Haiku / 本地小模型 | Router 分类、cascading 降级、便宜分类器候选 |
| Claude Opus 级 | OpenJudge LLM-as-judge 裁判（与被测不同族） |
| Embedding 模型 | 统一 RAG 的文档向量化（经 AgentScope **Embedding** 模块接入） |

> 通过 AgentScope **Model** 抽象统一切换 provider（DashScope/Ollama/OpenAI 兼容等）；任何模型降级仍须过评测门禁。

### 2.4 Web / HTTP / 异步 / 服务化

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **AgentScope FastAPI Agent Service**（`create_app`） | HTTP 入口 + lifespan-scoped `SessionService`/`SchedulerManager`/`BackgroundTaskManager` | **替换自研 FastAPI 编排 + 自研长程任务点火**；开发与部署一体（Runtime 合并入主库） |
| **httpx（封装为 Tool）** | 出站只读 REST / 受限写，`traceparent` 由 Middleware 注入 | 保留 httpx，封装形态改为 AgentScope Tool |
| **asyncio** | 协程驱动流式 reply 与后台任务 | 不变，但由 `BackgroundTaskManager` 托管 |
| uvicorn + gunicorn / K8s | 部署为独立微服务 | 不变，天然无状态水平扩展 |

### 2.5 执行环境与隔离（新增能力域）

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **Workspace 抽象** | 业务逻辑与执行环境解耦：`LocalWorkspace` / `DockerWorkspace` / `E2BWorkspace`，共享同一 agent-facing API | **新增**：受限写/代码执行/工具运行在隔离环境，"一次编写到处运行" |
| **WorkspaceManager** | `Local/Docker/E2BWorkspaceManager`，agent-level isolation | **替换自研租户隔离**：面向多租户服务的资源分配 |
| **内置权限工具** | Bash/Edit/Glob/Grep/Read/Write 均带权限控制 | 高危操作内生受控（编排能力场景下的文件/脚本类动作） |

### 2.6 存储

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **Redis** | **Redis-backed storage**（`SessionService` 会话状态 / 多租户资源）；ConfirmationStore 语义并入 Permission；工具结果缓存、限流 | 会话持久化从 MySQL checkpoint 三表迁移到 AgentScope Session store |
| **MySQL** | ① 业务可观测表；② 评测表族（`mes_eval` schema）；③ `AgentState` 归档/审计（可选）；④ RAG 文档元数据、关系型追溯读模型、事件位点与答案审计 | 不再需要 LangGraph SqlSaver 三表；新增统一 RAG 的读模型与审计用途 |
| **ChromaDB** | 统一 RAG 的文档向量检索，经 AgentScope **RAG/Embedding** 模块接入，chunk 不可变并执行版本/权限过滤 | 检索接入层由 AgentScope RAG 模块承载 |
| **SQLAlchemy 2.0 (async) + asyncmy** | MySQL ORM（业务表/评测表/RAG 读模型），追溯采用参数化固定查询计划 | 不使用图数据库或图查询驱动 |

### 2.7 消息 / 事件

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **AgentScope 事件系统** | 统一事件总线：服务前端 agent 应用 + human-in-the-loop 协作；`reply_stream` 产出 agent events | **替换自研 WebSocket/SSE 推送编排** |
| **`AGUIProtocolMiddleware`** | 流式传输到前端（动作卡实时渲染） | 前端实时推送内生化 |
| **Kafka (`aiokafka`)** | ① 领域事件订阅（`ProcessRouteActivated`/`equipment.fault` 主动触发）-> 桥接进事件系统；② 动作卡持久推送 topic（离线兜底）；③ RAG 幂等更新文档索引与关系型追溯读模型 | 保留 Kafka 作为跨服务事件总线；进程内协同改用 AgentScope 事件系统 |

### 2.8 可观测

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **`TracingMiddleware`** | OTel tracing 新入口（从 Agent 类移出，关注点分离） | **替换自研 `obs/Tracing`**；埋点内生、provider 无关 |
| **OTel Collector / W3C traceparent** | 跨语言链路串联（Python->Java） | 不变，注入点改为 Middleware |
| prometheus-client / structlog | 指标埋点 + 结构化日志（`trace_id`/`span_id`） | 保留，作为 Middleware 之外的补充埋点 |
| Tempo/Jaeger、Prometheus、Loki、Grafana | trace/指标/日志存储与看板 + SLI/SLO 告警 | 不变 |
| ~~AgentScope Studio~~ | 开发期可视化调试、事件/追踪查看 | ⚠️ **已归档，不列为选型**：研发期调试改用 OTel + Tempo/Jaeger trace 视图 + `reply_stream` 事件回放自建脚本 |

### 2.9 评测

| 组件 | 用途 | 替换/说明 |
|------|------|----------|
| **`py-openjudge`（独立包）** | LLM-as-judge 评估器 | **替换/收敛自研 JudgeService 的 judge 部分**；⚠️ 它不在 `agentscope` 包内，需单独引依赖与单独版本 pin |
| **自研 `EvalRunner` + CI 门禁** | 跑集、三层判定（断言/指标/judge）、pass^k、cost-per-successful-task、shadow/canary 门禁 | ⚠️ `agentscope.evaluate` 在 2.0 已删除且未回归，**评测栈必须自建**，不能写"框架内生评测" |
| pytest | 回归脚本、CI 触发 | 保留，包裹自研评测 runner |
| Platt scaling / isotonic / PSI / kappa | 置信度标定 / 漂移检测 / judge 一致性 | 保留（作为 OpenJudge 之上的统计层） |

### 2.10 横切能力：从"自研三大子模块"到"框架内生"

| 原横切子模块 | 原实现 | AgentScope 2.0 替换 |
|-------------|--------|---------------------|
| `infrastructure/cost/` 成本优化 | ModelRouter/CacheControl/ResultCompactor/EarlyStop… | **Offloader + `ToolOffloadMiddleware`**（上下文压缩/超大结果卸载）+ Model 抽象分层路由 + 自定义 Middleware（缓存/早停） |
| `infrastructure/obs/` 可观测 | ObservabilityContext/Tracing/MetricsCollector… | **`TracingMiddleware`** + Middleware 链（+ OTel/Tempo 查看；Studio 已归档不用） |
| `infrastructure/longtask/` 长程任务 | SqlSaver/GateManager/ConfirmationStore… | **`SessionService` + `AgentState` + Permission(gate) + `BackgroundTaskManager` + `SchedulerManager`** |

> 关键收益：三大横切能力从"团队自研+长期维护"变成"框架内生+自定义 Middleware 扩展"，团队只写**业务专属 Middleware**（如 MES 脱敏 `Redactor`、成本预算拦截），底座由框架保证。

---

## 3. 模块划分

### 3.1 `agent-service` 模块结构（AgentScope 2.0 重构）

agent-service 仍是单一 Python 微服务，三项能力 共存；但执行内核从"自研 StateGraph + code_nodes"改为"AgentScope `Agent` + `Toolkit` + Middleware + Permission"，**编排形状本身仍是应用层 Python 代码**（框架不再提供 pipeline 原语）。

```
agent_service/app/
├── service/                      # AgentScope FastAPI Agent Service 入口
│   ├── app.py                    #   lifespan 装配 SessionService/SchedulerManager/
│   │                             #   BackgroundTaskManager/WorkspaceManager
│   └── routes/                   #   diagnose / draft / orchestration / schedule 路由
│
├── agents/                       # 统一 Agent 类实例（reply_stream 事件流）
│   ├── diagnosis_agent.py        # 诊断能力: 只读 ReAct 诊断 Agent（Permission=只读）
│   ├── draft_agents.py           # 草稿能力: 草稿 Agent（返工单/8D/SOP，NoWrite）
│   └── orchestration/            # 编排能力: 能力节点 Agent（供 flows/ 调用）
│       ├── root_cause_agent.py         # A 根因推理 + 处置卡
│       ├── fault_impact_agent.py       # B 故障隔离范围判定
│       ├── traceability_agent.py       # C 客诉追溯（复用 诊断能力）
│       └── draft_agent.py              # D SOP/8D/返工工艺草拟
│
├── flows/                        # 编排形状（应用层 Python 代码；2.0 无 pipeline 原语可依赖）
│   ├── changeover.py             # ① 换线：顺序 await + Permission gate
│   ├── fault_response.py         # ② 设备故障复产：asyncio.gather 并发独立 Agent + 显式合并
│   ├── complaint_8d.py           # ③ 客诉 8D：诊断->追溯->草拟 顺序调用
│   └── process_change.py         # ④ 工艺变更落地（跨天部分交工作流引擎）
│
├── task_context/                 # 任务清单：TaskCreate/Get/List/Update + Task/TaskContext
│   └── long_chain_tasks.py       #   长链路场景的任务分解与进度跟踪（非框架 Plan 模块）
│
├── middleware/                   # 自定义 Middleware（业务横切）
│   ├── redactor_mw.py            #   MES 脱敏（序列号/批次/PII）
│   ├── cost_budget_mw.py         #   预算拦截 + 结果压缩策略
│   ├── early_stop_mw.py          #   证据充分性早停
│   └── metrics_mw.py             #   业务指标埋点（补充 TracingMiddleware）
│
├── permissions/                  # 本仓库自定义 PermissionRule 组合（替换三层启动断言）
│   ├── readonly_policy.py        #   诊断能力：拒绝任何非只读工具（规则集名自定，非框架类名）
│   ├── nowrite_policy.py         #   草稿能力：不持写 client，requires_confirmation 恒 True
│   └── gated_write_policy.py     #   编排能力：写工具 gate + HITL confirmation
│
├── tools/                        # Toolkit：工具 + MCP + Skills
│   ├── acl/                      #   对 MES 上下文 + RAG 的只读/受限写工具
│   │   ├── readonly.py           #     过点/工艺/物料/设备/质量只读
│   │   ├── restricted_write.py   #     tooling/telemetry/rework 受限写
│   │   └── rag.py                #     fetch_evidence_set / search_docs
│   └── task_tools.py             #   TaskCreate/Get/List/Update 工具接线
│
├── infrastructure/
│   ├── model/                    # AgentScope Model 抽象（provider 可插拔）
│   ├── session/                  # `SessionService` + Redis-backed storage 适配
│   ├── workspace/                # WorkspaceManager（Local/Docker/E2B）配置
│   ├── kafka/                    # aiokafka ↔ 事件系统桥接
│   ├── persistence/             # SQLAlchemy：业务可观测表 + 评测/审计
│   └── obs/                      # TracingMiddleware 接线 + prometheus/structlog
│
├── config.py                     # pydantic-settings
└── main.py                       # 启动装配 + Permission 策略校验
```

**对外接口汇总（端点不变，实现底座切换）：**

| 端点 | 层级 | 说明 |
|------|------|------|
| `POST /agent/diagnose` | 诊断能力 | 发起诊断，返回 `DiagnosisReport`（含 `evidence_set_id` 与版本锚点）；内部 `reply_stream` 可流式 |
| `POST /agent/draft` | 草稿能力 | 草拟处置，返回 `Draft`（`requires_confirmation=True`） |
| `GET /agent/draft/{id}/evidence` | 草稿能力 | 回溯草稿证据 |
| `POST /agent/orchestration/{scenario}/start` | 编排能力 | 启动编排流程（`flows/` 应用层代码，scenario ∈ changeover/fault_response/complaint_8d/process_change） |
| `POST /agent/orchestration/{session_id}/confirm` | 编排能力 | Permission gate 确认，由事件系统触发续跑（替换 `Command(resume=token)`） |
| `GET /agent/orchestration/{session_id}/events` | 编排能力 | 事件流订阅（AGUI），动作卡实时推送 |

### 3.2 `rag-service` 模块结构（统一 RAG，接入 AgentScope RAG/Toolkit）

rag-service 只建设一个 RAG，统一暴露 `POST /rag/query` 与证据回查接口。内部共享 `RagQuery`、`RagAnswer`、`Evidence`、`VersionAnchor` 和 `TenantContext`，不设置路线级模块或开关。

| 内部能力 | 职责 | 技术形态（AgentScope 2.0 版） |
|----------|------|------------------------------|
| 文档检索 | SOP/手册/标准/8D 检索与精确引用 | AgentScope **RAG 模块 + Embedding** + ChromaDB + 版本/权限过滤 |
| 制造追溯 | 单件、批次、工单及 5M1E 证据串联 | MySQL 关系型读模型 + 固定关联查询 + MES 只读 REST，封装为 **Tool** |
| Agentic | 根据证据缺口追加检索并组合多源证据 | **统一 Agent 类 + Toolkit**，限制工具、步数、时长与记录数 |

简单请求由应用服务直接调用检索 Port；只有需要多源组合或补查时才进入 Agentic。追溯不使用图检索，不引入 Neo4j、Cypher、图投影或 `subgraph_ref`。交付按统一底座与文档检索 -> 关系型追溯 -> Agentic -> 生产治理增量推进。

### 3.3 `mes-eval` 评测库模块结构（对接 OpenJudge）

| 组件 | 职责 | AgentScope 2.0 对接 |
|------|------|---------------------|
| **EvalRunner** | 离线跑集 + 三层判定（断言/指标/judge） | **自研**（⚠️ `agentscope.evaluate` 2.0 已删且未回归，框架不提供 runner） |
| **JudgeService** | LLM-as-judge，按 MES rubric 判质量 | 基于 **OpenJudge 评估器** |
| **ShadowRunner** | 在线影子采样对比（5% 流量） | 复用事件系统采样 |
| **CalibrationService / DriftDetector** | 置信度标定（ECE/Platt/isotonic）/ 漂移（PSI） | 统计层，OpenJudge 之上 |
| **SafetyChecker / VersionAnchorChecker** | 安全红线硬断言 / 版本锚点比对 | 保留（MES 专用硬门禁） |
| **CIGate** | CI 门禁（hard/soft gate） | 保留 |
| **EvalTarget 适配器** | 统一 RAG 一个端到端适配器，按用例评检索、追溯与编排；Agent 三层级各自适配 | 适配到 AgentScope Agent/Tool 接口 |

### 3.4 模块间集成点与接口契约

| 集成点 | 接口/契约 | 方向 | 说明 |
|--------|----------|------|------|
| 诊断能力检索证据 | `query_rag` Tool 封装 `POST /rag/query`，使用 `RETRIEVAL_ONLY` | Agent -> RAG | 一次调用按需获得文档与追溯证据 |
| 诊断能力只读补数 | 各上下文只读 REST Tool | Agent -> MES | 仅用于 RAG 未覆盖且已列入白名单的只读查询 |
| 草稿能力回查证据 | 证据集回查 Tool / `GET /rag/answers/{answer_id}/evidence` | Agent -> RAG | 复用诊断证据与版本锚点，必要时通过统一 RAG 补查 |
| 草稿能力检索文档 | `query_rag(query, version_anchor, RETRIEVAL_ONLY)` Tool | Agent -> RAG | 8D/SOP 草拟检索适用版本的历史文档 |
| 编排能力受限写 | ACL 写 Tool -> 应用服务 REST（header `X-Confirmation-Token` + `X-Confirmed-By`） | Agent -> MES | **由 Permission gate 放行后**才可调用，走聚合根不变式 + 事务发件箱 |
| RAG 读模型同步 | Kafka 领域事件流 | MES -> RAG | 幂等更新文档索引和 MySQL 关系型追溯读模型 |
| 主动触发 | `ProcessRouteActivated`/`equipment.fault` -> 事件系统 | MES -> Agent | 触发 草稿能力异步草拟 / 编排能力故障复产 |
| 动作卡推送 | 事件系统 + `AGUIProtocolMiddleware` + Kafka 兜底 | Agent -> 前端 | 实时 + 离线双通道 |
| 评测接入 | EvalTarget 适配器 | mes-eval -> Agent/RAG | 统一跑集判定 |
| 跨语言追踪 | W3C `traceparent` | Agent -> MES | 由 `TracingMiddleware` 注入 |

**版本一致性传递链**（核心安全契约，不变）：
```
制造记录/文档的 VersionAnchor -> RAG Evidence.version_anchor -> 诊断能力 evidence.version_anchor -> 草稿能力 Draft.version_anchor -> MES 应用服务校验 ACTIVE
```

---

## 4. Agent 能力架构（能力映射到 AgentScope 2.0）

### 4.1 能力矩阵（一个 Agent，三项能力）

| 能力 | 图的形态 | AgentScope 2.0 承载 | Permission 策略 |
|------|---------|---------------------|----------------|
| **诊断能力** | 开放式 ReAct | 单 `Agent`（ReAct 事件流），多步只读推理 | `readonly_policy` |
| **草稿能力** | 固定链 + 策略分派 | `Agent` + 策略化 draft agents，产草稿不落库 | `nowrite_policy`，`requires_confirmation` 恒 True |
| **编排能力** | Pipeline 代码图 | **`flows/` 应用层编排（顺序 `await` + `asyncio.gather` 并发独立 Agent）+ 确定性节点 + 能力节点** | `gated_write_policy` + HITL |

> 统一入口路由（Agentic RAG 收口）归 rag-service 的路由层，不是第四项能力。

### 4.2 能力间数据流

```
诊断能力 -> DiagnosisReport + evidence_set_id
              ↓（invoke，共享 session 与 trace）
草稿编写能力 -> 按 evidence_set_id 回查关系型证据子集 -> Draft（requires_confirmation=True）
              ↓ 人确认（MES 正式界面）
          MES 正式应用服务落库（过聚合根不变式 + 事务发件箱）

编排能力 flows/ -> 顺序 await 与 asyncio.gather 调度确定性节点
              ↓ 非确定分支 invoke("diagnosis") / invoke("drafting")
          能力产物 -> ActionCard
              ↓ Permission gate（拦截 -> 事件系统 HITL 确认 -> 续跑）
          MES 正式应用服务落库
```

### 4.3 编排能力 "代码 + agent 混合编排" 判定标准（原则不变，落点更清晰）

一个步骤是否需要 Agent，看三问：**输入是否开放？是否需要推理/生成？分支是否难以穷举？**
- 三问皆否 -> **确定性节点/普通函数**（不调 LLM，由 `flows/` 直接 `await`）
- 三问有一 -> **AgentScope `Agent` 节点**（调 LLM）

> 换线全程 PASS 时 Agent 节点不触发，**LLM 调用为 0**。"代码能做的不交给 LLM"仍是核心原则；`flows/` 让确定性步骤与 Agent 步骤在同一编排里清晰共存。

### 4.4 写动作的三层防线（主闸在工具网关，Permission 为叠加层）

| 层级 | 防线机制 | AgentScope 2.0 实现 |
|------|---------|---------------------|
| 诊断能力 | 全程只读 | `readonly_policy`：Permission 拒绝任何非只读工具执行 + **只读凭证**（工具网关侧无写通道） |
| 草稿能力 | 不持写 client | `nowrite_policy`：Permission 禁止写工具注册；`requires_confirmation` 恒 True |
| 编排能力 | 写工具须 gate + HITL | `gated_write_policy`：**Gate tool execution + Human-in-the-loop confirmation**，落库走应用服务过聚合根不变式 + 事务发件箱 |

> Agent 全程不碰 MES 原始表，写路径与人工下达完全一致；触发源从"人点按钮"变为"Agent 草拟 + gate 确认"。
> **主闸与叠加层的分工（不可颠倒）**：主闸是**工具网关**——出站写调用必须携带与动作一一绑定的**一次性令牌**，网关校验并消费后即失效，幂等键两把（`action_id` + `dedup_key`），令牌过期或校验失败一律 **fail closed**。Permission 系统作为叠加层把闸门下沉到每一次工具执行，但它发生在 Agent 循环内——绕开循环直接调函数即失效，因此**不能单独作为安全边界**。见 ADR-04。

---

## 5. 关键设计决策汇总（重构版）

| # | 决策 | 核心权衡 |
|---|------|---------|
| 1 | **用 AgentScope 2.0 统一 Agent 类替代 LangGraph StateGraph** | 事件流原生支持权限/HITL/前端集成；省去手搓图与 checkpoint 自研 |
| 2 | **多智能体协同：默认单 Agent + `Toolkit`；确需并行用应用层 `asyncio.gather` 并发独立 `Agent.reply()`，结论显式合并** | ⚠️ 2.0 已删 `pipeline` 三原语与 `MsgHub`，框架不提供声明式协同；协同代码留在本仓库，换来"能读得懂 + 不随框架 API 变动" |
| 3 | **写闸门主闸在工具网关（一次性动作令牌），Permission 作叠加层** | 网关与调用方是否走 Agent 无关，绕不过；Permission 提供循环内的细粒度拦截与 HITL 续跑 |
| 4 | **长程任务用 `SessionService` + `AgentState` + `BackgroundTaskManager` 替代自研 SqlSaver/Celery/interrupt** | 无状态水平扩展，任意副本恢复任意会话；不再自研 checkpoint 三表。⚠️ 跨天/跨进程的持久编排不在此列，归工作流引擎（ADR-05） |
| 5 | **可观测用 TracingMiddleware 替代自研 obs/** | OTel 内生入口、关注点分离；⚠️ Studio 已归档，研发期调试走 Tempo/Jaeger |
| 6 | **成本优化用 Offloader + ToolOffloadMiddleware + Model 分层 替代自研 cost/** | 超大结果卸载/上下文压缩内生；便宜模型降级仍须过评测门禁 |
| 7 | **执行隔离用 Workspace（Local/Docker/E2B）+ WorkspaceManager** | 受限写/代码执行沙箱化；多租户 agent-level isolation 内生 |
| 8 | **前端推送用事件系统 + AGUIProtocolMiddleware 替代自研 WS/SSE 编排** | 动作卡实时渲染 + HITL 确认同源，Kafka 仍作离线兜底 |
| 9 | **评测栈自建：`py-openjudge` 作 judge + 自研 `EvalRunner`/CI 门禁** | ⚠️ `agentscope.evaluate` 2.0 已删且未回归，**不存在内生评测**；Ragas/DeepEval 作为检索指标的可选实现保留 |
| 10 | **RAG 路线 E 的"路由"由单 Agent 的 ReAct + `Toolkit` 工具选择承担** | ⚠️ AgentScope 无 Handoffs/Routing 原语；跨能力委托是显式服务调用 + `evidence_set_id` 传递，不是框架移交机制 |
| 11 | **消息/内容用 Content Block（Pydantic）+ Msg（usage/时间戳/block id）** | 天然可校验/序列化/计费/溯源，替代零散结构化输出 |
| 12 | **跨语言仍 Python Agent + Java MES；预留 AgentScope Java 2.0 同构扩展** | 物理边界安全不变；未来跨语言扩展无需换框架心智 |
| 13 | **安全红线不变、承载升级**（诊断能力只读/草稿能力不持写/编排能力 gated write） | "能不能写"的语义不变；"如何强制"= 网关一次性令牌（主闸）+ Permission/HITL（叠加层），自研断言并入这两层 |
| 14 | **版本锚定 + 三段传递链完全保留** | MES 追溯的失效工艺红线是业务硬约束，与框架无关 |

---

## 6. 新架构如何提升三大能力

### 6.1 多智能体协同能力（诚实边界版）

> 本节早先写作"Pipeline 三形态 + MsgHub + Handoffs/Routing + Plan 模块"，经源码树逐 tag 核对后**全部撤回**：这些是 AgentScope **1.x** 的 API，2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 模块已不存在，`Handoffs/Routing`/独立 `Plan` 模块从未提供。以下是 2.0 真实可依赖的部分。

- **统一 Agent 类 + 事件流**：每个 Agent 是 pure producer，`reply_stream` 事件天然支持并发消费、实时干预与 HITL 挂起续跑——这是本节唯一由框架承载的收益。
- **顺序协同**（诊断 -> 追溯 -> 草拟）：应用层 `await` 串联，上游产物以 Content Block 显式传入下游输入。
- **并行协同**（故障复产多能力）：`asyncio.gather` 并发多个**各自独立**的 `Agent.reply()`，结论在应用层显式合并；每个分支独立预算、独立超时、独立 `Permission` 规则。
- **⚠️ 不提供"广播共享上下文"**：`MsgHub` 式"A 根因 + B 隔离范围互相看到彼此结论"在 2.0 **无原语**。硬做要自建消息中转，等于回到手搓。本项目的替代方案：各分支只读同一份 **`evidence_set_id`**（关系型证据子集），彼此**不**看对方结论——刻意如此，因为扇出式互看会放大错误（无中心校验 17.2× vs 中心化 4.4×，见《Multi-Agent必要性-独立复核与批判》）。
- **跨能力委托**：显式服务调用（`invoke` 语义由我们实现，不是框架关键字），上下文只传 `evidence_set_id` / `session_id` / `traceparent`，不传消息历史。
- **任务分解与跟踪**：`TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` 工具 + `Task`/`TaskContext`（已核实存在）。**但**"多 Agent 认领子任务"需要自建协调循环，框架不给调度器；长链路的持久规划归工作流引擎（ADR-05）。

### 6.2 分布式支持

- **无状态水平扩展**：`AgentState` 显式化 + `SessionService`（Redis-backed）持久会话，**任意副本恢复任意用户完整上下文**——Pod OOM/滚动更新后新副本按 `session_id` 续跑，无需自研 checkpoint。
- **BackgroundTaskManager + SchedulerManager**：长程任务与定时巡检由框架托管，替代自研 asyncio 点火与 Celery。
- **Workspace 分布式形态**：同一份业务代码按需切换 Local->Docker->E2B，执行环境可独立扩缩容。

### 6.3 可扩展性

- **Middleware 链**：新增横切能力（脱敏/预算/审计/限流）= 加一个 Middleware，不侵入 Agent 业务逻辑。
- **Toolkit + MCP + Skills**：新增工具/接入外部 MCP 服务/封装领域技能，均标准化；工具边界仍对齐 14 个限界上下文。
- **Permission 策略即配置**：新增写场景 = 声明一条 gated 规则（`PermissionRule`），闸门自动生效；网关令牌仍是主闸。
- **Model 抽象**：新增/替换 provider 无需改业务；降级过评测门禁即可上线。
- **`flows/` 可组合**：新增 编排能力场景 = 用普通 Python 组装已有 Agent + 确定性节点，复用度高、无框架 DSL 学习成本。

---

## 7. 引入路线（迁移视角）

| 阶段 | 交付内容 | 说明 |
|------|---------|------|
| **第 0 步** | 底座切换 | 引入 AgentScope 2.0（pin 具体小版本），搭 FastAPI Agent Service（`create_app`）+ `SessionService` + `TracingMiddleware`，跑通空壳 |
| **第 1 步** | 诊断迁移 | 将 StateGraph ReAct 重写为统一 Agent + Toolkit + `readonly_policy`；对齐评测基线 |
| **第 2 步** | 草稿迁移 | draft agents + `nowrite_policy`；证据回查 Tool 化（`fetch_evidence_set`） |
| **第 3 步** | 横切能力替换 | obs->TracingMiddleware、cost->Offloader/`ToolOffloadMiddleware`、longtask->`SessionService`/Permission |
| **试点** | 编排能力换线编排 | `flows/changeover.py` 顺序 `await` + 网关令牌（主闸）+ Permission gate + 事件系统 HITL，confirmation 做扎实 |
| **扩展** | 编排能力其余场景 | `asyncio.gather` 并发独立 Agent 承载故障复产；客诉 8D / 工艺变更为顺序调用（跨天部分待工作流引擎） |
| **收口** | 统一入口 + E Agentic RAG | 单 Agent + `Toolkit` 工具选择承载分诊，跨能力委托为显式调用 + `evidence_set_id` 传递 |

> 迁移策略：**端点契约不变**（`/agent/diagnose` 等），每层迁移后用 mes-eval（自研 runner + `py-openjudge`）对齐旧基线，通过评测门禁才切流量，保证平滑替换、可回滚。
> **两条前置**：① 单 Agent 基线先跑通并测出准确率——基线 >45% 后加 Agent 是负回报（《复核》数据），不要用"上了多 Agent"当作里程碑；② 版本 pin 与 API 漂移检查列入 CI，`agentscope` 2.0.x 已出现过模块删除与改名（`pipeline`/`MsgHub` 删、`SessionManager`→`SessionService`）。

---

## 8. 附录：AgentScope 2.0 能力 -> 本架构落点速查

| AgentScope 2.0 能力 | 本架构落点 |
|---------------------|-----------|
| 统一 `Agent` 类（reply_stream/reply） | 三项能力 各 Agent |
| Content Block / `Msg`（Pydantic, usage, block id） | 消息与证据溯源、计费 |
| Permission 系统（`PermissionEngine`/`Rule`/`Mode`/`Decision`） | 写动作三层防线的**叠加层**（主闸是网关一次性令牌） |
| Middleware（`TracingMiddleware`/`AGUIProtocolMiddleware`/`ToolOffloadMiddleware` + 自定义） | 可观测/前端流/成本/脱敏/审计 |
| `Offloader` + `LocalWorkspace` 等 8 种后端 | 超大结果卸载 + 执行隔离 + 多租户 |
| `AgentState` + `SessionService`（Redis-backed） | 分布式会话恢复、长程任务 |
| `BackgroundTaskManager` / `SchedulerManager` | 后台任务 / 定时巡检 |
| `Toolkit` + MCP + Skills | ACL 工具 / 外部服务 / 领域技能 |
| `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate` + `Task`/`TaskContext` | 任务清单与进度跟踪（无独立 Plan 模块） |
| Model + Embedding | provider 可插拔 + RAG 向量化 |
| RAG 模块 | 文档型 RAG（路线 B）——⚠️ 无 ChromaDB 适配器，且该模块曾被整体删除过一次，需版本 pin + 降级预案 |
| `event.RequireUserConfirmEvent` | HITL 确认与续跑 |

**已核实为不存在/已删除，本架构不写进生产路径：**

| 名字 | 真实状态 | 本架构替代 |
|------|---------|-----------|
| `MsgHub` / `sequential_pipeline` / `fanout_pipeline` | 1.x API，2.0.0 起模块不存在 | 顺序 `await` + `asyncio.gather` + 应用层显式合并 |
| `Handoffs` / `Routing` | 从未提供 | 单 Agent 的 ReAct + `Toolkit` 工具选择；跨能力显式调用 |
| `agentscope.evaluate`（"智能体评测"） | 2.0 已删除，未回归 | 自研 `EvalRunner` + CI 门禁；judge 用独立包 `py-openjudge` |
| `AgentScope Studio` | 已归档 | Tempo/Jaeger + `reply_stream` 事件回放 |
| `SessionManager` | 2.0.5 起改名 `SessionService` | 一律写 `SessionService` |
| `GoalPipeline` / `TeamPipeline` | 存在但 experimental，且 `TeamPipeline` 明确"成员间不互相说话" | 观察，不进生产 |

---

## 9. 附录：源文档索引

- `整体技术选型与模块划分.md` — 全仓总览（已对齐本文 AgentScope 2.0 口径）
- `架构设计v2/00-架构决策记录ADR.md` — **本文每条选型背后的判定依据、证据分级与被证伪项**（ADR-01…ADR-12）
- `AGENT服务/` — 三项能力 分层与三大横切能力设计
- `RAG服务/` — 统一 RAG 的文档检索、关系型制造追溯与 Agentic 检索编排
- `RAG与Agent评测/` — 检索/追溯/编排/端到端与 Agent 分层评测
- `领域模型/`、`实现说明/` — MES 主体（Java）14 个限界上下文与基础设施

> 说明：本文为**技术选型重设计稿**。文中组件名已按 **AgentScope 2.0.11 源码树逐 tag 核对**（核对过程与证据见 `架构设计v2/00-架构决策记录ADR.md` ADR-01），不再写"以官方文档为准"式的模糊表述：**已删除/不存在的原语已从生产路径剔除并列在附录速查表下方**。落地前仍需把 `agentscope` pin 到具体小版本，并在 CI 加 API 漂移检查——2.0.x 系列内已发生过模块删除（`pipeline`/`MsgHub`）与改名（`SessionManager`→`SessionService`）。
