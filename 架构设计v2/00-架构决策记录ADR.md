# Agent 侧架构 · 决策记录（ADR）

> **范围**：`agent-service` / `rag-service` / `mes-eval` 三个 Python 服务，以及它们与 Java MES 的交互边界。MES 主体（14 个限界上下文、过点、Outbox、高频采集）不在本文决策范围内，见 `领域模型/` 与 `实现说明/`。
>
> **本文地位**：这是**从零重做**的架构决策记录，用于取代 `基于AgentScope2.0的架构设计.md` 的选型结论。正文（分层图、模块树、数据流、引入路线）待这批决策逐条确认后展开。
>
> **与旧文档的关系**：旧文档口径统一（LangGraph → AgentScope）仍在并行收尾，那份是"现状基线"；本文是"目标设计"。两者不一致处以本文为准，冲突点集中列在 §13。

## 0. 证据分级（沿用仓库既有纪律）

- 【A】同行评议论文 / 原始研究
- 【B】厂商或机构公开技术报告 / **官方文档与代码仓库**（可溯源，非同行评议）
- 【C】二手聚合、社区归纳、技术博客 —— **可信度打折，引用前需回源**
- 【推】基于上述证据的推断
- 【设】未经验证的假设，必须在你的场景实测

凡涉及业务收益的数字一律标 **【目标示例】**，禁止当行业基准引用。框架 API 名标 `[已核实]` / `[待核实]`，核实依据是官方仓库、PyPI 与 docs.agentscope.io，**不是博客**。

---

## ADR-01 底座框架：AgentScope 2.0，但只用已核实的原语

**背景**：需要一个同时提供 ReAct 循环、工具、权限、沙箱、会话、评测外壳的框架，避免自研横切能力。

**决策**：采用 AgentScope 2.0（**2.0.0 发布于 2026-05-25，最新 2.0.11 发布于 2026-10-27**，`requires-python >=3.11`；官方 FAQ 明确 2.0 与 1.0 不兼容需改代码）。本项目**只使用下表已核实原语**；未核实或**已删除**的名字不进入设计与代码。

**核查方式**：对照官方仓库源码树（tag `v2.0.11` 与 `v1.0.21`）、docs.agentscope.io 版本化文档、PyPI 与 release notes。证据路径均为 `github.com/agentscope-ai/agentscope` 下的实际文件。

| 能力 | 裁定与已核实形态 | 分级 |
|------|-----------|------|
| Agent 主体 | ✅ `agentscope.agent.Agent`（`src/agentscope/agent/`，`_agent.py` 内 `reply` / `reply_stream`），全 async ReAct 循环 | 【B】源码 |
| 消息与 Content Block | ✅ `message/_block.py`：Text / Thinking / Data / Hint / ToolCall / ToolResult，**每块带 `id`**；`Msg(BaseModel)` 含 `Usage` 与 `created_at/finished_at` | 【B】源码 |
| 工具 | ✅ `tool/__init__.py` → `Toolkit` / `Tool`，内置 `Bash/Read/Grep/Glob/Write/Edit`；`mcp/` → `MCPClient`/`Stdio`/`HttpMCPConfig`；`skill/` 存在 | 【B】源码 |
| 任务规划工具 | ✅ **精确同名** `TaskCreate/TaskGet/TaskList/TaskUpdate`（`tool/_task/`）。但**没有 `agentscope.plan` 模块**——官方文档的"Plan"就是这 4 个工具 + `is_state_injected` 写 `agent.state.tasks_context` | 【B】源码 + docs `building-blocks/plan.md` |
| 权限 | ✅ `permission/` → `PermissionEngine` / `Rule` / `Mode` / `Behavior` / `Context` / `Decision`；模式 `DEFAULT` / `ACCEPT_EDITS` / `EXPLORE` / `BYPASS` / `DONT_ASK`。旧文档那三行"Gate tool execution / HITL confirmation / Agent autonomy control"**逐字来自官方 changelog**，不是编造 | 【B】源码 + docs |
| 工作区/沙箱 | ✅ `workspace/` → Local / Docker / E2B / K8s / Daytona / OpenSandbox / AppleContainer / Bubblewrap，`Offloader` 亦在此模块；`app/workspace_manager/` → `Local/Docker/E2BWorkspaceManager` + `IsolationPolicy` | 【B】源码 |
| 中间件 | ✅ `middleware/` 存在，`middleware/_tracing/_trace.py` 确实 import `opentelemetry`（Tracing 从 Agent 类移出为中间件，与旧文档一致）；`app/middleware/` → `AGUIProtocolMiddleware`、`ToolOffloadMiddleware`、`StateChangeMiddleware`、`InboxMiddleware`、`TeamMemberLoopMiddleware` | 【B】源码 |
| 状态与会话 | ⚠️ `state/` → `AgentState` / `Task` / `TaskContext` ✅；但会话服务 **2.0.5 起 `SessionManager` 已改名 `SessionService`**（`app/_service/_session.py`）。按旧名写设计不可导入 | 【B】源码 |
| 后台任务/调度 | ✅ `app/_manager/` → `BackgroundTaskManager` / `SchedulerManager`（同列 `CancelDispatcher` / `WakeupDispatcher` / `ChatRunRegistry`） | 【B】源码 |
| 存储 | ✅ Redis 后端真实（`app/storage/_redis_storage.py`、`app/message_bus/_redis_message_bus.py`），FAQ 另列 PostgreSQL / MySQL / SQLite | 【B】源码 + FAQ |
| 服务化 | ✅ 官方名是 **`agentscope.app.create_app`**（FastAPI Agent Service，"Agent as Service"） | 【B】源码 + docs `deploy/agent-service.md` |
| 事件系统 | ✅ `event/` → `RequireUserConfirmEvent` 等；changelog 明确"Deprecate the print interface…pure producer"。HITL 走 `RequireUserConfirmEvent` | 【B】源码 |
| **多 Agent 协同** | ❌ **`MsgHub` / `sequential_pipeline` / `fanout_pipeline` 是 1.x API，2.0.0 起 `pipeline` 该模块内容不存在**。`v2.0.11` 仅剩 `PipelineProtocol` / `GoalPipeline` / `TeamPipeline` / `TeamMember`，且官方标注 **experimental，接口可能变**。**`TeamPipeline` 文档明写 "No member-to-member talk"** | 【B】源码树对比 |
| **Handoffs / Routing** | ❌ **不存在**。仓库无 handoff 原语；`Routing` 仅指模型路由（`classifier` + `ModelRouterMiddleware`）与 IM 渠道消息路由 | 【B】源码 |
| **可观测 UI** | ❌ **`agentscope-studio` 已归档、README 声明不再作为独立工具维护**；2.0 改用内置 `examples/web_ui` | 【B】官方仓库 |
| **内生评测** | ❌ **1.x `agentscope.evaluate` 在 2.0 被删除且未回归**（v2.0.11 顶层无 evaluate/tracing/session/memory/hooks）。OpenJudge 真实但是**独立仓库/独立 PyPI 包 `py-openjudge`**（50+ graders），`agentscope` 包内**无任何引用** | 【B】源码 + 独立仓库 |
| RAG 模块 | ⚠️ 存在（`rag/` → `KnowledgeBase` + Qdrant / MilvusLite / Elasticsearch / MongoDB），但 **2.0.0 曾被整体删除、2.0.5 起才回归**；且**无 ChromaDB 适配器** | 【B】源码 + release notes |
| 跨进程协作 | ✅ **A2A**（`A2AAgent` 经远端 agent card，本质 httpx 客户端）。自研 `rpc` / `distributed` / `RpcAgent` 在 main 分支**已整体删除** | 【B】源码 |
| 长期记忆 | ⚠️ ReMe 集成为外部项目 | 【C】 |
| `agentscope-runtime` | ✅ 旧文档"Runtime 合并入主库"**正确**：官方仓库 README 有 Archive Notice，能力已原生并入 2.0 | 【B】官方仓库 |
| Java 2.0 | ⚠️ 真实（`agentscope-java`，2026-07 GA）但**独立仓库、独立排期、自有抽象**（Harness / Sandbox / Subagent / Plan Mode），FAQ 原文"each with its own repository and release schedule"。**不是 1:1 同构 API** | 【B】FAQ |

**另需注意的稳定性风险**：`app.*` 下的 `middleware` / `_manager` / `workspace_manager` / `_service` 多为下划线私有路径，**公开导出仅 `create_app` 与 `SubAgentTemplate`**。旧文档把这些当稳定公开 API 使用，升级期有断裂风险。


**后果**：
- 正面：横切能力（沙箱、权限档位、会话、服务化）不再自研；`EXPLORE` 模式与诊断能力"全程只读"天然对齐；`DONT_ASK` 对齐定时巡检。
- 负面：**框架版本快速演进且会删卖点**（rpc/distributed 被整体移除）。因此所有框架能力必须封在适配层后面，业务代码不直接依赖具体 middleware/workspace 类名。
- 风险：官方设计哲学是"用模型的推理与工具能力，而不是用严格 prompt 和强编排去约束它"——这与 MES 需要的**确定性防错**存在张力。**解法见 ADR-05：确定性部分根本不由 Agent 承载**。

**不选**：AutoGen（对话式抽象不适合严格步骤编排）、CrewAI（细粒度权限难做）。LangGraph 仍可作为 ADR-05 备选的工作流承载，但**不与 AgentScope 的 pipeline/子图术语混用**——术语混用是旧文档最大的可读性问题。

---

## ADR-02 默认单 Agent；拆多 Agent 需同时过四道闸

**背景**：旧文档曾把 11 个 MES 模块中的 8 个误判为"必拆多 Agent"，复核后推倒（见 `AGENT服务/Multi-Agent必要性-独立复核与批判.md`）。本轮外部调研给出一条该文档没有的**量化决策边界**。

**决策**：所有新功能从单 Agent 起步。拆多 Agent 必须同时满足：

1. **模式闸门**：只允许两类模式 —— ① 广度优先的**只读**取证；② 验证方持有生成方**拿不到的外部证据**的生成-验证。
2. **可拆性闸门（新增）**：任务**必须能分解**，而不是"任务很难"。证据：单 Agent 基线成功率**超过约 45%** 后，继续加 Agent 转为**负回报**（β=−0.236, p=0.004）；强顺序依赖任务上，**所有**多 Agent 架构掉 39%~70%，而可并行的分析类中心化架构 +80.9%。【A/B，arXiv:2512.08296 / 2512.02896】
3. **中心校验闸门（新增）**：扇出**必须**有确定性汇合与校验节点。证据：无交叉验证的 Independent 拓扑错误放大 **17.2×**，Centralized 为 **4.4×**。→ 可以 fan-out，但汇合点必须裁决，否则选了最差拓扑。【A/B，arXiv:2512.08296】
4. **成本闸门**：按"每成功任务成本"算，而非按 p95 延迟。协调开销实测：Single 0% / Independent ≈58% / Decentralized ≈263% / Centralized ≈285% / Hybrid ≈515%；Anthropic 自报多 Agent 研究系统约 **15×** token。【B/C】

> **与《复核》§5.2 的关系（消歧，不是两套结论）**：本 ADR 的四道闸是**可执行的准入清单**（模式 / 可拆性 / 中心校验 / 成本），《复核》§5.2 的五条是**论证顺序**（前置 → 硬判据 → 反向 → 成本 → 运维）。二者同向，本清单是《复核》判据的操作化版本；**若表述冲突，以《复核》为准**，本 ADR 只额外贡献三条外部量化证据：45% 后加 Agent 负回报、Independent 17.2× vs Centralized 4.4×、协调开销阶梯。

**明确概念边界**：**sub-agent 上下文隔离 ≠ 多 Agent 系统**。把脏活丢给子 agent 拿回 1–2k token 摘要是单 Agent 框架内的技巧。

**关键推论**：诊断→草拟→处置这条主链是**顺序依赖型**，落在"所有 MAS 架构都掉"的区间。因此 **MES 主链的多 Agent 主张默认驳回**，只有"影响面五级清点""限定分支后的并行取证"和"带外部规则的审核"三处例外。

---

## ADR-03 服务拓扑与写权限矩阵

**决策**：三个 Python 服务 + 一个 Java 主体，写权限矩阵是唯一硬约束。

| 服务 | 职责 | 写权限 |
|------|------|--------|
| `agent-service` | 诊断（只读推理）→ 草拟（产草稿）→ 编排（受限写 + 人确认） | **仅**草稿表、会话、审计。业务写一律经 MES 应用服务 |
| `rag-service` | **文档知识工具**（召回 + 引用 + 文档版本治理） | 仅自有向量索引、文档治理表、审计。**不读不写 MES 业务表** |
| `mes-eval` | 金标准集、三层判定、标定/漂移、CI 门禁 | 只读评测 |
| MES 主体（Java） | 14 个限界上下文与全部业务写路径 | 唯一业务写权限，聚合根不变式 + Outbox |

**解耦**：只经 **REST 只读** + **Kafka 只读事件**，出站封装为 `Toolkit` Tool `[已核实]`。Agent 不碰 MES 原始表。

**制造追溯已从 RAG 侧迁出**（ADR-13）：形态固定为“只读工具 + 只读数据面”，且坚决不用图检索——MySQL 关系型读模型 + 固定关联查询计划 + MES 只读补数，不引入 Neo4j / Cypher / 图投影；旧文档“按子图回查图节点”的口径统一改为**按 `evidence_set_id` 回查关系型证据子集**。追溯的归属（并入 `agent-service` / 独立只读服务）仍待定，见 `制造追溯-需求与实现效果.md` §6。

---

## ADR-04 安全边界在工具网关层，框架 Permission 只是叠加项

**背景**：这是本轮核查推翻旧文档最重要的一条。框架权限引擎的匹配对象是 **Bash 命令子串与文件路径 glob**；而本项目的危险动作是**自定义 Tool 调 MES 受限写 REST**。把"写闸门做进每一次工具执行、粒度更细更难绕过"这一安全主张押在框架 Permission 上，未经证实。

**决策**：双层，**且顺序不可颠倒**。

1. **外层（真边界，自有代码）**：工具网关 / 服务端鉴权。每个写 Tool 必须
   - 服务端二次鉴权（不信任 Agent 传来的身份）；
   - 参数 schema 强校验 + 白名单（**不指望模型填对参数**：BFCL v4 前沿模型总体仍只有 77.5% 量级 【B】）；
   - **幂等键两把**：调用方重放键（防重复提交）与世界态对账键（防"以为做了"）【B，A2A 讨论】；
   - **动作绑定的一次性确认令牌**：令牌绑定"具体动作 + 具体 target + 具体用户"，不可从 requestId 推导，**用后即废**，过期 **fail closed**【B，Google 有限用途令牌 / OWASP Agentic Top 10 控制项】；
   - 全量审计：谁授权、授权了什么、结果如何。
2. **内层（叠加，框架）**：`Permission` 模式与 allow/deny 规则 `[已核实]`；诊断类 Agent 挂 `EXPLORE` 类只读档位，巡检类无人值守挂 `DONT_ASK`。

**后果**：多写一层，但这是唯一能守住"错给一条已失效工艺 = 批量不良"红线的做法。**内层失效不得导致外层放行**。

**待核实项**（影响设计，不阻塞）：框架 Permission 是否支持对**自定义 Tool** 按 `action:target` 粒度拦截。若支持，外层可退化为校验；若不支持，外层就是唯一边界。

---

## ADR-05 长周期编排归工作流引擎与 MES 审批流，`flows/` 只做单次调用内的协同

**背景**：换线、8D、工艺变更这类链路的真实需求是：跨天等待人工确认、超时升级、严格重试语义、可审计补偿。这是**工作流引擎与审批流的原生职责**（复核 P0-3 已把审批链移出 Agent 判据）。会话持久化不等于 durable workflow。**本轮核查使这条结论从"建议"变成"被迫"**：旧文档把编排能力整体押在 `Pipeline(sequential/fanout/MsgHub)` 上，而这些 API **在 AgentScope 2.0 中已不存在**（1.x API，2.0.0 起模块内容被移除）。押注一个框架已经不提供的原语，实现时无从下手。

**同时撤销一个旧设计：agent 侧的挂起 / 断点续跑 / 跨副本恢复。** 逐项对过 4 个编排场景后判定它不是刚需：① gate 之间几乎全是确定性代码节点（换线快路径 LLM 调用为 0），所谓"挂起期"没有任何需要保住的计算态；② MES 现场事实**时变**，把 T+3s 的快照载回 T+10min 的进程继续跑，等于用过期事实做当下决策（与会话语义缓存默认关闭同一条理由）；③ 每个 gate 产出的都是 MES **已有业务对象**（维修单 / 隔离单 / 首件放行 / 8D / SOP），审批本身就有可靠实体；④ 框架也不提供 `interrupt` / `Command(resume=…)` / checkpoint 原语，自建同语义机制就是把工作流引擎的职责搬进最不适合它的地方。

**决策**：**混合，职责切干净**。

- **审批与待办层**（MES 已有工单实体）：gate 产出的动作卡落成维修单 / 隔离单 / 首件放行 / 8D / SOP，责任人分派、签收、催办、超时升级全部在该实体的生命周期内。
- **工作流引擎层**（Temporal 类 `[选型待定，见 §14 未决问题]`）：跨调用的流程推进、实体间编排、重试与补偿、审计时间线。**所有"等人"与"等物理世界变绿"的 gate 一律在这里，不在 agent 进程内。**
- **AgentScope 层**：只在引擎调用的**单个节点内部**跑 `Agent` 推理与工具执行。**节点内的并发不用框架 pipeline 原语**——核查显示 `fanout_pipeline` / `sequential_pipeline` / `MsgHub` 已从 2.0 移除，`v2.0.11` 仅剩 experimental 的 `GoalPipeline` / `TeamPipeline`。因此节点内扇出用 **`asyncio.gather` 并发多个独立 `Agent.reply()` 调用**实现，汇合即返回，不持有流程状态。
- **共享上下文的旧设计作废**：旧文档"MsgHub 让 A 根因 + B 隔离范围互相看到彼此结论"在 2.0 **无法实现**——`TeamPipeline` 文档明写 **"No member-to-member talk"**。改为**经证据集显式传递**：一个节点的结论落成 `evidence_set_id`，下游按需回查，不做成员间广播。
- **LLM 节点数封顶**：确定性步骤（阈值判断、齐套校验、barrier 分流、放行决策）写成工作流节点里的普通函数，**不进 Agent**。三问判定保留：输入开放？需推理/生成？分支难穷举？——三问皆否即代码。**注意：三问判定是本设计自有判据，官方文档没有给出任何"何时该拆多 Agent"的量化标准**（FAQ 原文仅一句："Use teams when a task benefits from delegation or separate agent contexts. A single agent is sufficient when one model and toolkit can complete the task."）。
- **写操作永远单线程**：并行读与想，串行写。

**代价**：引入一个基础设施组件与它的运维面（集群、版本升级、workflow 代码与业务代码双份心智）。**换来的是"跨天等待 + 补偿 + 审计"不用自研**。若不引入，则需自研持久化状态机——旧文档正是走这条路，复核结论认为这条路把框架当工作流引擎用，成本更高。

**红线**：过点主事务 200ms P99 **不得**被任何编排层触碰。Agent 与编排全部在旁路。

---

## ADR-06 版本锚定是模型外的确定性层，不做成"让模型注意版本"

**背景**：这是 MES 场景与普通 Agent 应用的最大差异，也是旧文档处理最弱的一环。新证据 decisive：嵌入相似度区分"被推翻的事实"与"重复表述"仅 **AUROC 0.59（近似随机）**，过期事实被服务率 **15–40%**【A，arXiv:2606.26511】。**指望向量检索自动排除失效工艺，物理上不成立。**

**决策**：
1. 版本锚点 `route_version` / `bom_version` / `rule_version` 是**一等公民**，贯穿 `诊断 → 草稿 → 写意图 → trace → 评测` 全链路，每环可断言。
2. RAG 每条证据带 `[valid_from, valid_to]` 与 supersession 链（谁替代谁），**由确定性规则过滤**，不由相似度过滤。
3. **取不到锚点即拒答**；任何"默认取当前生效版本"的代码路径判为缺陷，不是偷懒。
4. `ProcessRouteActivated` 事件只用于**新版本建 key**，旧 key 不失效（历史可回放）。
5. 评测集逐条钉锚点，版本不一致直接 hard fail。

---

## ADR-07 状态外置：run 先落库再执行，会话态**不承担跨调用续跑**

**决策**：Agent run **先落库再执行**（与 Agno v3.0 等生产实践同向 【C】），状态用已核实的 `AgentState`（`state/`，含 `Task` / `TaskContext`），会话服务用 **`SessionService`**（`app/_service/_session.py`，**2.0.5 起由 `SessionManager` 改名而来，旧名不可导入**），Redis 后端真实存在（`app/storage/_redis_storage.py`）。⚠️ **用途限定**：`AgentState` + `SessionService` 只承载**会话内上下文与多轮对话记忆**（按 `session_id` 隔离，因此单次调用在哪台副本上跑都行）；**不用它做 gate 挂起 / 断点续跑 / 载回快照**（理由见 ADR-05）。跨 gate 的正确语义是：本段落库后 return，确认后回调 `/advance` **重新发起下一段并重查当下现场事实**；Pod OOM / 滚动更新不需特殊处理，每段幂等可重入。

**与 ADR-05 的分工**：会话状态 ≠ 流程状态。流程推进与等待归审批流/工作流引擎，会话/消息历史归框架 session。两者以 `session_id` + `workflow_id`（或工单 id）双键关联。

**子会话隔离**：`{session_id}:{capability}` 维度隔离；`failure_count >= 2` 则**本段置 `WAITING` 并转人工待办**推异常卡（旧口径的 `SUSPENDED` 不再使用，因它暗含"可恢复"语义）。

---

## ADR-08 过点 200ms 红线的真正风险是资源共享，必须四道舱壁

**背景**：只写"Agent 不同步调 LLM"不足以保住红线。真实干扰来自**共享资源**：连接池、协程池、DB CPU、同消费者组 rebalance【B，Oracle 明确反对 LLM 直连生产库】。

**决策**：四道舱壁。

1. **数据舱壁**：Agent 只查**独立只读副本 / 读模型**，不碰主库。
2. **算力舱壁**：独立服务身份 + 每租户信号量 + 并发上限。
3. **查询预算**：每工具硬上限（时间窗、行数、超时 ≤2s），超预算即返回缺口不放大查询。
4. **背压与降级语义**：Kafka 独立消费组 + pause 背压，避免积压拖垮消费者；**熔断后语义是 fail-open = 不给建议**，绝不阻塞人工界面。

**可观测信号**：Agent 侧查询必须打点独立池占用，能与过点 P99 曲线做相关性验证。

---

## ADR-09 工具层是能力扩展点，也是注入入口

**决策**：
1. 工具边界对齐限界上下文；**检索式工具门控**优先于"拆 Agent"。证据：K≈7.4 个候选即可达到全目录 50 个的准确率（90.3% vs 90.8%）【A/B，arXiv:2605.24660】；Anthropic Tool Search 内部 MCP 评测 49%→74%，工具定义 token ~72K→8.7K【B/C】。**这把"拆 Agent"的门槛抬高了一档**。
2. 若走 MCP：按 **2026-07-28 规范**重估 —— 无状态核心（去掉 `initialize`/`Mcp-Session-Id`，可置于普通 LB 后）、长任务进 Tasks 扩展、授权走 OAuth 2.1 体系、OTel 上下文传递、工具列表缓存。【B】
3. **工具返回值本身是注入面**；工具定义被静默改写是 rug-pull 风险 → 工具定义版本化 + 变更审计 + 出参脱敏。
4. 参数校验程序化（类型/范围/schema），不做成 prompt 里的叮嘱。
5. **不开放任意查询**：追溯计划走白名单，非法计划、缺种子、超时间窗、超记录数一律拒绝。

---

## ADR-10 评测：pass^k 与每成功任务成本，发布走 shadow → canary → 放量

**背景**：旧文档写"单步 95% → 10 步 59.9%"是【推】级算术。现在可换成实测。

**决策**：
1. 指标主轴用 **pass^k / pass^8**（连续 k 次全对），不用 pass@1。实测：τ-bench 系 pass@1≈90% → pass^8≈57%；GPT-4o 零售域 pass@1≈61% → pass^8≈25%；法律基准单条要求达成 94.5% 而整任务完成仅 25.4%。【B/C】
2. 成本主轴用 **cost-per-successful-task** 与副作用失败率。**只看 p95 + 存活率会放行坏 Agent**。【B/C】
3. **单 Agent 基线必须先建**，否则分不清"架构更优"与"多烧算力"（等算力预算下单 Agent 在多跳推理上持平或更优，且部分 MAS 优势来自 API 预算计账漏洞 【A，arXiv:2604.02460】）。
4. 上线顺序：shadow（100% 复制真实任务但**不产生副作用**）→ canary → 放量。模型 / prompt / 工具路由 / 审批策略**四类变更**都必须走影子。【B/C】
5. 金标准集用**自有历史案例**，不追外部榜：公开基准已被污染（Terminal-Bench 2 榜首被审计后 81.8%→~71.7% 【C】）。
6. 门禁分层：安全硬断言（版本锚点、写权限、越权）为 **hard gate**，零容忍阻断；质量指标为 soft gate。
7. **评测栈必须自建，不能声称"框架内生"**：核查显示 1.x 的 `agentscope.evaluate` 在 2.0 **被删除且未回归**，`agentscope-studio` **已归档停止维护**（2.0 改用内置 `examples/web_ui`）。OpenJudge 真实但是**独立仓库/独立包 `py-openjudge`**（50+ graders），`agentscope` 包内**无任何引用**。所以 `mes-eval` 的形态是：**自建 runner + 按需引 `py-openjudge` 作 grader 库 + pytest 做 CI 外壳**，框架只提供 trace 事件（`TracingMiddleware`）作为过程数据来源。旧文档"内生评测栈替换自研 JudgeService"这条理由**不成立**。

---

## ADR-11 归因不靠 LLM 事后判断，靠契约 + 确定性校验 + 拓扑标签 trace

**背景**：新证据推翻了"出问题让强模型诊断一下"的直觉。AgenTracer (ICLR 2026) 测得 SOTA 推理 LLM 判定"哪个 Agent / 哪步出错"的准确率**普遍 <10%**。MAST 也显示失败集中在设计（角色不清）、协调（信息不通）、验证（没人真检查），而非模型能力。【A/B/C】

**决策**：
1. **trace 必须带拓扑标签**（哪个节点、什么角色、哪个 Agent、输入输出契约版本），单 Agent 可独立回放。
2. 校验是**契约级 + 确定性**的：每个子输出有 schema 断言、锚点断言、权限断言，在**执行路径上**拦截，不靠事后分析。
3. 每个 Agent 必须显式写清：角色、工具白名单、**停止条件**、预算上限——MAST 归纳失败最集中在这四项缺失。
4. **LLM 自评置信度不可靠**（复核 P2-5，CRITIC/Huang et al. 支撑）：`confidence` 由规则算，标定用 Platt/isotonic 在评测集上拟合，不采信模型自报数值。
5. `gaps`（缺口）是一等公民：取不到证据必须显式声明缺口并拒答，不许硬凑。

---

## ADR-12 架构不锁死：每季度复测并预留回退

**决策**：
1. 回退开关常驻：编排与取证支持 `single` / `probe_fanout` / `fanout` 三档，默认可一键退回单 Agent。
2. 每季度用评测集复测：**今天需要多 Agent 的场景，下一代模型可能单 Agent 就够了**；框架版本演进会删能力（rpc/distributed 已被删）。
3. 数据就绪度是前置且**未被验证**的假设——旧复核文自标"当前 MES 数据完整度可能不足以支撑自动 RCA"为最大未验证假设。本轮外部调研**没有**替你验证它。**开工前必须先盘点**：过点是否绑设备参数与工艺版本快照、批次绑定是否完整、检验数据能否回溯到单件。数据不全时任何架构都不成立。

---

## ADR-13 能力归属：制造追溯迁出 rag-service，编排权唯一归 agent-service

**背景**：旧设计让 `rag-service` 同时承担文档检索、关系型制造追溯与 Agentic 检索编排。落地推演后发现三个结构性问题：

1. **双编排套娃**：`agent-service` 已有多步编排，`rag-service` 内部又有一层 ReAct 循环，两者叠加导致延迟与 token 双倍、trace 嵌套、预算口径各算一套；
2. **版本口径有分叉风险**：追溯解析出“当时版本”，文档检索又可能在另一处解析，两套逻辑给出两个锚点，最终表现为“用新 SOP 解释旧单件”；
3. **两类负载互相牵制**：向量库与追溯读模型的数据源、故障模式、扩缩容特征完全不同，混在一个服务里，向量库挂掉会连带追溯不可用，追溯同步积压也会拖慢文档检索。

**决策**：

1. `rag-service` 收敛为**文档知识服务**，只提供 `search_docs` 与 `fetch_doc_answer`，以 AgentScope `Tool` over HTTP 接入 `agent-service`；**`AUTO` 多步编排下线**，只保留 `RETRIEVAL_ONLY` 与工具端点。
2. 制造追溯迁出，形态固定为**只读工具 + 只读数据面**（MySQL 读模型 + 固定查询计划 + 三段版本解析 + MES 只读补数）；**归属待定**：候选 A 并入 `agent-service` 进程、候选 B 独立 `trace-query-service`，判据见 `制造追溯-需求与实现效果.md` §6。
3. **编排权唯一**：同一请求只允许一层 ReAct；`version_resolve → trace_lookup → search_docs` 这类链路写成确定性 flow，ReAct 只决定“是否补查、换哪个种子”。
4. **锚点单向流动**：三段 `VersionAnchor` 由追溯侧产出、`agent-service` 透传、文档检索消费；取不到锚点即拒答，回退“当前生效版”判为缺陷（ADR-06 不变）。
5. **证据集单一签发方**：统一 `evidence_set_id` 由 `agent-service` 的 EvidenceRegistry 签发，rag 只回查文档侧引用，避免两套证据集并存。

**后果**：

- 正面：单层编排、单处版本解析、两类负载故障域分离；文档检索可以独立交付与扩缩容；追溯工具可被诊断、草稿、报表、评测复用。
- 负面：多了一个工具契约面（`context_id`、超时、滞后语义、缺口）需要维护；若追溯选 A（并入 `agent-service`），agent 进程会背上 Alembic 迁移与 Kafka 消费组，故障域变大。
- 缓解：追溯若选 A，至少把 consumer 与 api 拆成独立进程角色、使用独立数据库 schema、保留读模型重建工具；工具契约保持与选 B 一致，后续可按 Port 抽出独立服务而不改契约。

**不选**：

- 追溯继续留在 `rag-service`：混负载的代价大于多一个部署单元，且会持续诱导“rag 内部再编排”。
- 追溯使用图数据库：关联深度实测 2-3 跳，关系型 JOIN + 白名单查询计划足够；图库带来建模与运维成本，且查询易退化为全图扇出（ADR-03 红线不变）。

**回滚与触发条件**：若选 A 后出现“agent 部署单元过重 / 读模型重建频繁 / 追溯与推理互相干扰”，按既有 Port 抽出 `trace-query-service`，Tool 契约不变；若出现“无 agent 运时、仅需问答 UI 直连”的场景，文档侧可恢复单步检索 + 生成，但**不得恢复多步循环**。

---

## §13 与现有文档的差异（需要显式推翻的结论）

| # | 现有文档写法 | 本文结论 |
|---|------------|---------|
| 1 | 写闸门"从自研启动断言升级为框架 Permission 承载，安全强度只增不减" | **降级**。Permission 是叠加项，真边界在工具网关 + 动作绑定一次性令牌（ADR-04） |
| 2 | 编排能力 = `Pipeline(sequential/fanout/MsgHub)` + Permission gate 承载整条跨天链路 | **双重推翻**。① 跨天 gate/补偿/审计归工作流引擎（ADR-05）；② 这些 pipeline 原语**在 2.0 已被移除**，`v2.0.11` 仅剩 experimental 的 `GoalPipeline`/`TeamPipeline`，且 `TeamPipeline` 明写 "No member-to-member talk" |
| 3 | "AgentScope 内生分布式支持" | **修正**。自研 `rpc`/`distributed`/`RpcAgent` 在 main 已整体删除，跨进程走 A2A；本设计不依赖跨进程 Agent |
| 4 | `readonly_policy` / `nowrite_policy` / `gated_write_policy` 三条 policy 类 | **名称不成立**。框架真实形态是 `PermissionEngine/Rule/Mode/Behavior/Context/Decision` + 5 个模式；这三条应改述为**我们在 `EXPLORE` / 自定义 deny 规则之上组合出的三套策略**，不是框架类名 |
| 4b | `AGUIProtocolMiddleware`、`ToolOffloadMiddleware`、`Offloader`、`AgentState`、`BackgroundTaskManager`、`SchedulerManager`、`TracingMiddleware`、`Toolkit`、4 个 `Task*` 工具 | ✅ **已核实真实存在**（源码路径见 ADR-01 表）。旧文档这部分没编 |
| 4c | `SessionManager` | ⚠️ 2.0.5 起**改名 `SessionService`**，按旧名写不可导入 |
| 4d | "Plan 模块" | ⚠️ 无 `agentscope.plan`；官方"Plan"= 4 个 Task 工具 + `agent.state.tasks_context` |
| 4e | Handoffs / Routing 承载统一入口路由（早期把它当第四项能力 E） | ❌ **无此原语**。入口路由必须自建，且归属**独立入口网关** `agent-gateway-service`（普通分发函数 / 规则 + LLM 兜底分类器；`ModelRouterMiddleware` 做的是模型路由，不是能力移交），不归 `rag-service` |
| 4f | 智能体评测 + OpenJudge = "框架内生评测栈"，Studio 做研发期观测 | ❌ `agentscope.evaluate` 在 2.0 已删未回归；Studio 已归档；OpenJudge 是独立包 `py-openjudge`，框架内无引用 |
| 4g | ChromaDB "经 AgentScope RAG 模块接入" | ❌ RAG 模块只有 Qdrant / MilvusLite / Elasticsearch / MongoDB，**无 Chroma 适配器**；且该模块 2.0.0 曾被整体删除、2.0.5 才回归 |
| 4h | "AgentScope Java 2.0 同构 API，可平滑跨语言扩展" | ⚠️ Java 2.0 真实但独立仓库/独立排期/自有抽象，FAQ 原文"each with its own repository and release schedule"，**非 1:1 同构** |
| 4i | 把 `app.middleware` / `app._manager` / `app.workspace_manager` / `app._service` 当稳定公开 API | ⚠️ 多为下划线私有路径，公开导出仅 `create_app` 与 `SubAgentTemplate`，须包在适配层后面 |
| 5 | Python 3.12+（本轮刚统一） | 与框架要求 **3.11+** 兼容，可保留 3.12（代码仓 `requires-python` 已是 >=3.12），无冲突 |
| 6 | 受益场景只有两类 | **两类不变，但加两条闸门**：可拆性（基线 >45% 不拆）、扇出必须有中心校验（否则 17.2× 放大） |
| 7 | 版本锚定靠"RAG 返回版本 + 下游校验" | **加强**。embedding 无法区分失效事实（AUROC 0.59），必须模型外确定性过滤 + 取不到锚点即拒答 |
| 8 | 置信度由规则算（旧文档已如此写） | 保留，并补“LLM 事后归因准确率 <10%，归因必须靠契约与 trace 拓扑” |
| 9 | `rag-service` 同时承载文档检索 + 关系型追溯 + Agentic 编排 | **拆分**。rag 收敛为文档知识工具；追溯迁出为独立只读工具（归属待定）；编排权唯一归 `agent-service`（ADR-13） |

---

## §14 未决问题（需要你或实测回答，不要我猜）

1. **工作流引擎选型**：Temporal / 既有 Java 侧流程引擎 / Flowable-BPMN。判据是"跨天 gate 与补偿由谁承载最省"，以及**团队是否愿意运维它**。注意若落到 Java 侧，Agent 与流程分属两种语言栈，需要定义清楚交互契约。
2. **框架 Permission 能否按 `action:target` 拦截自定义 Tool**（决定 ADR-04 外层是主边界还是校验层）。源码里 `PermissionRule` / `PermissionBehavior` / `PermissionContext` 均真实，但**规则匹配是否覆盖非内置工具未证实**——需要读 `_rule` 实现或写探针验证，不能靠文档推断。
3. ~~官方 API 的确切类名~~ **已回填**（见 ADR-01 表，证据基线 v2.0.11）。新冒出三个需要决策的次生问题：
   - **experimental 的 `pipeline` 要不要用**：官方标注接口可能变。本设计目前**不依赖**它（并发用 `asyncio.gather`），若未来 `TeamPipeline` 稳定再评估。
   - **`rag/` 模块要不要依赖**：它在 2.0.0 被整体删除、2.0.5 才回归，且无 Chroma 适配器。倾向**直连向量库（Qdrant / MilvusLite）而非框架 RAG 抽象**，把"曾被删除"的风险挡在适配层外。需你确认是否接受为此多写一层。
   - **前端流式协议**：`AGUIProtocolMiddleware` 与 `event/` 事件流真实，但 AGUI 与既有工位 UI 契约是否匹配需核对；`print` 接口已 deprecated，所有渲染走事件流。
4. **数据就绪度实测结果**（ADR-12）。
5. **"错误工艺版本"历史事故的真实成因分布**【设，未验证】——决定 ADR-06 的优先级是"版本锚定"还是"数据完整性"。
6. **框架版本策略**：2.0.0（2026-05-25）→ 2.0.11（2026-10-27）五个月内 11 个补丁版，期间发生 `SessionManager` 改名、`evaluate` 未回归、`pipeline` 收缩。需要定死 pin 策略与升级评审门槛。

---

## §15 一句话

**默认单 Agent；确定性留在代码；跨天编排归工作流引擎；安全边界建在工具网关而不是框架 Permission；版本锚定做成模型外的确定性过滤；编排权唯一归 agent-service，制造追溯是独立只读工具，rag 只做文档知识；评测用 pass^k 与每成功任务成本、发布走影子；框架只用已核实原语，未核实名字不进代码。**

---

## 附录 A · AgentScope 2.0.11 可确证最小骨架

以下写法逐项对应 ADR-01 表中标 ✅ 的原语（源码路径为 `github.com/agentscope-ai/agentscope` tag `v2.0.11`）。**注意 `pipeline` 一行为 experimental，本设计当前不依赖它。**

```python
from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.model import DashScopeChatModel
from agentscope.tool import Toolkit, Read, Write, Bash, TaskCreate, TaskList
from agentscope.workspace import LocalWorkspace
from agentscope.middleware import TracingMiddleware
from agentscope.permission import PermissionEngine, PermissionRule, PermissionMode
from agentscope.app import create_app                      # FastAPI Agent Service
# 会话服务：SessionService（2.0.5 起，旧名 SessionManager 已失效）
# HITL：agentscope.event.RequireUserConfirmEvent
# 节点内并发：asyncio.gather 多个独立 Agent.reply()，不用 fanout_pipeline（已移除）

async with LocalWorkspace(workdir="./workspace") as ws:
    a = Agent(
        name="diagnosis",
        model=DashScopeChatModel(
            credential=DashScopeCredential(api_key=...),   # 2.0 需 Credential，1.x 直传 api_key 作废
            model=...,
        ),
        toolkit=Toolkit(tools=await ws.list_tools()),
        offloader=ws,                                      # 上下文压缩/超大结果卸载
    )
    async for ev in a.reply_stream(inputs):                # 事件流；print 接口已 deprecated
        ...
```

**引用来源**：官方仓库与版本化文档 —— [PyPI agentscope](https://pypi.org/project/agentscope/)、[Changelog 2.0 vs 1.0](https://docs.agentscope.io/en/versions/2.0.11/others/change-log.md)、[Release Notes](https://docs.agentscope.io/en/versions/2.0.11/release-notes.md)、[FAQ](https://docs.agentscope.io/en/versions/2.0.11/others/faq.md)、[Permission System](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/permission-system/overview.md)、[Pipeline/Team](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/pipeline/team.md)、[Plan](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/plan.md)、[Agent Service](https://docs.agentscope.io/en/versions/2.0.11/deploy/agent-service.md)、[agentscope-studio（已归档）](https://github.com/agentscope-ai/agentscope-studio)、[OpenJudge（独立仓库）](https://github.com/agentscope-ai/OpenJudge)。
