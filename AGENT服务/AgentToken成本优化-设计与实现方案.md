# Agent Token 成本优化 -- 设计与实现方案（Python 技术栈，三项能力共用）

> **定位**：本文是 [整体技术选型与模块划分.md](../整体技术选型与模块划分.md) §3.5.1「成本优化（原自研 `cost/` -> `Offloader` + `ToolOffloadMiddleware` + Model 分层路由 + 自定义 Middleware）」的降本侧展开，与三篇配套：
> - [Agent可观测性-设计与实现方案.md](Agent可观测性-设计与实现方案.md)：**测得清**——`agent_token_total` / `agent_cost_usd_total`、token/会话 P95 漂移告警（§5.4 / §8 / §11.3）。指标名与 `llm_call_log` 字段的**事实源唯一在那篇**。
> - [Agent记忆系统-设计与实现方案.md](Agent记忆系统-设计与实现方案.md)：**存给谁看 / 活多久**——M2 上下文窗口记忆（§4）、自治跨会话缓存（§6）、取数闸门与出口校验（§3.10）。
> - 本篇：**降得下**——在度量与记忆分层之上，把"钱花在哪、怎么省、省了不碰红线不降质量"系统化，闭环是 **测 -> 归因 -> 降 -> 再测**。
>
> **事实源优先级**：[架构决策记录 ADR](../架构设计v2/00-架构决策记录ADR.md) §0 声明"用于取代 [基于AgentScope2.0的架构设计.md](../基于AgentScope2.0的架构设计.md) 的选型结论……两者不一致处以本文为准，冲突点集中列在 §13"。能力流程事实源在 [诊断能力](FactoryAgent/FactoryAgent-诊断能力.md) / [草稿编写能力](FactoryAgent/FactoryAgent-草稿编写能力.md) / [流程编排能力](FactoryAgent/FactoryAgent-流程编排能力.md)；网关分派事实源在 [Agent入口网关-意图路由.md](Agent入口网关-意图路由.md)。本篇不另立口径。
>
> **底座口径**：AgentScope 2.0（证据基线 `v2.0.11`，须 pin 具体小版本 + CI 加 API 漂移检查；2.0 与 1.0 不兼容需改代码）、Python `>=3.12`（框架要求 3.11+，无冲突）。已核实的框架能力：统一 `Agent`（`reply` / `reply_stream`，**内置 ReAct**，无 `ReActAgent` 类）、`Model` 抽象（`DashScopeChatModel` + `Credential`，2.0 直传 `api_key` 作废）、`Toolkit` / `Tool`、`Offloader` + `ToolOffloadMiddleware`、`TracingMiddleware`、`PermissionEngine`、`Content Block` + `Msg` + `Usage`（`input_tokens` / `output_tokens`）、`SessionService` / `AgentState`（2.0.5 改名，旧名不可导入）。⚠️ 这些多位于 `app.*` 下划线私有路径，公开导出仅 `create_app` 与 `SubAgentTemplate`，**须包在适配层后面**（ADR §13 4i），不能当稳定公开 API 直接依赖。框架**不提供** `Handoffs` / `Routing` 移交原语（`ModelRouterMiddleware` 做的是模型路由而非能力移交）、`with_structured_output`、`agentscope.evaluate`；结构化输出口径是 **Content Block（Pydantic BaseModel）+ `Msg`**。
>
> **组件命名口径**：缓存标记 / 早停 / 预算拦截 / 分层路由启动校验，以及 `OffloadCompactPolicy` / `ToolResultCache` / `EvalGate` / `ToolExecHook`，都是**本项目自定义 Middleware 与封装**，不是框架 API；文中 `app/...` 路径一律是**设计目标路径（文件当前不存在）**。`ModelRouter` / `ResultCompactor` / `ObservableChatModel` 是**已被收编或替换的旧名**（底座 §2.10、整体选型 §3.5.1 / §3.5.2），本篇不再把它们当现役组件。
>
> **落地状态（重要）**：当前代码仓 `src/` 下只有 `agents/tool_test.py`、`api/{health,index,tool_test}.py`、`tools/get_current_time.py` 等脚手架，三项能力、观测底座、记忆系统与本篇全部降本组件**均未落地**（[可观测性 §17](Agent可观测性-设计与实现方案.md)、[记忆系统 §10](Agent记忆系统-设计与实现方案.md)）。本文全部条目状态为 📐 设计目标，尚未落地；相关指标**尚未埋点**。
>
> **口径纪律**：本篇讲的是**设计规划**，不是说"已落地降本 X%"。所有量化收益标 🔴 待实测，绝不编业务效果数字。降本的前提是**不碰 MES 红线**（只读 / 不进过点 / 版本一致性 / 证据不可空 / 时变性 / 质量不退 / 编排权唯一）——任何降本手段若牺牲这些，一律不做。
>
> **数字标注口径**：凡涉及业务收益的示例数字按 [ADR §0](../架构设计v2/00-架构决策记录ADR.md) 标 **【目标示例】**，禁止当行业基准引用；设计容量目标与线上实绩分开讲（[面试指南](../面试指南/项目亮点与指标卡片.md) 行 142）。§11.4 的规模分档表是「待回填的测算口径」，不是业务效果数字。

---

## 0. 先界定：降本不是"换便宜模型"

通用做法是把模型换成更便宜的。但本 MES Agent 的成本结构里，**模型单价只是其中一个变量**，而且不是最该先动的。一次诊断会话的钱花在四块：

1. **重复的系统提示 + 工具定义**：ReAct 每一步都把 system prompt + 全部工具描述重发给模型，N 步就发 N 次。这块占比最大、最浪费、最容易省。
2. **滚雪球式的历史上下文**：第 k 步的输入包含前 k-1 步的全部工具结果。工具返回的 JSON 越大，历史涨得越快。
3. **无效的步数**：模型反复查同一上下文、查了不用、该收口不收口——每多一步就是一次"系统提示+历史"的完整重发；schema 校验失败重试还会再叠一层。
4. **模型规格错配**：能用小模型的步骤用了大模型；能用代码做的步骤用了模型。

一句话：**降本的优先级是"少调 > 少发 > 调便宜的"，不是反过来**（整体选型 §5 决策 7）。换便宜模型是**最后一步**，且必须过评测门禁（§10.3）才能换——MES 不允许为省钱牺牲根因准确率。

再补一条当前设计的成本观（[ADR-10](../架构设计v2/00-架构决策记录ADR.md)）：**只看单会话 token 会放行坏 Agent**。成本主轴是 **每成功任务成本**（cost-per-successful-task = 总花费 / 通过 pass^k 的 case 数）。若某个"省 token"的动作让重跑率、拒答率、转人工率上升，token 降了但每成功任务成本反而升——这不算降本。所以本篇所有杠杆的验收口径都同时看 **token 下降** 与 **pass^k / ECE / 每成功任务成本不退**。

---

## 1. 定位与边界

### 1.1 降本的红线（一开口就要讲）

| 红线 | 说明 | 降本时怎么守 |
|------|------|-------------|
| **只读 / 不旁路写** | 诊断 / 草稿 `writes_allowed=False`；编排的写动作过 confirmation gate，**主闸是工具网关的一次性动作令牌**，`Permission` 只是叠加层（ADR-04） | 降本手段不改写路径、不缓存写结果；缓存只作用于只读数据面 |
| **不进过点主事务** | 过点 P99 ≤200ms，判定仍走规则引擎（[领域总览.md](../领域模型/领域总览.md) §4.1 / §5.3） | 不在过点路径挂任何同步 LLM 调用；主动触发一律异步 |
| **版本一致性** | `version_anchor` 是一等公民（ADR-06），查工艺强制 `route_version`；取不到锚点即拒答，回退"当前生效版"判为缺陷 | 缓存 key 必含 `version_anchor`；`data_scope` **只由 `as_of` 派生**，与请求是否带 `version` 无关 |
| **证据不可空** | 每条 hypothesis 至少引用 1 条 `trace_id`（可观测性 §7.2） | `tool_call_trace.output_payload` 落 **FULL view**；压缩只作用"喂模型"一路 |
| **时变性** | MES 现场事实随新数据到达而漂移；默认 `LIVE_ONLY` 偏向查新（记忆系统 §3.10.3） | `CURRENT` 一律跳过答案缓存；`LIVE_ONLY` 且 `CURRENT`/跨 turn 强制实时，缓存命中也作废 |
| **质量不退** | pass^k（ADR-10 口径 pass^8）、ECE <0.1、工具调用冗余、草稿采纳率不因降本下降 | 换模型 / 改提示词 / 加截断 / 改缓存策略必须过 `EvalRunner` 回归 + shadow→canary→放量 |
| **编排权唯一** | 同一请求只允许**一层 ReAct**，`version_resolve → trace_lookup → search_docs` 写成确定性 flow（ADR-13） | 任何降本手段不得引入第二层编排或第二套 ReAct 循环 |

### 1.2 与可观测性 / 记忆系统的关系

- 可观测性是**度量基础设施**：花了多少、谁花的、有没有漂移。
- 记忆系统是**分层视图**：同一份数据"给模型看压缩版、给系统看全文版"（记忆系统 §0「模型看摘要，trace 落全文」）。
- 本篇是**优化动作**：基于度量结果决定"动哪个杠杆、动了之后省了多少、质量退没退"。
- 三者闭环：**测 -> 归因 -> 降 -> 再测**。没有可观测性，降本是盲降；没有记忆分层的双路设计，压缩必然伤证据链；没有降本动作，可观测性只是账单。

### 1.3 不覆盖

- 不重新设计可观测底座（指标 / 表结构事实源唯一在可观测性方案；本篇 §11 只提**待同步的设计扩展**）。
- 不替三项能力各自的业务流程做选型，也不改端点契约（底座 §7「端点契约不变」）。
- 不重新设计记忆分层与取数闸门（事实源在记忆系统 §2 / §3.10），本篇只讲它们在成本侧的落点。
- 不涉及模型供应商商务议价 / 预留额度——那是采购侧，不是架构侧。

---

## 2. 成本模型：钱花在哪

### 2.1 四种运行形态，四种成本结构（本篇最重要的口径修正）

旧版把三项能力统统按"ReAct 多步循环"建模。按当前设计，**只有诊断能力是开放式 ReAct**；草稿是固定链、编排是 `flows/` 确定性代码、检索是单层 flow。把它们的成本套成同一个公式，是归因错乱的源头。

**① 诊断能力 `diagnosis`（开放式 ReAct，成本主战场）**——统一 `Agent` 内置 `model ↔ tool` 循环：

```
总输入 token ≈ Σ_{step=1..N} [ system_prompt + tool_definitions + history_{1..step-1} ]
总输出 token ≈ Σ_{step=1..N} output_step            （tool_calls + 最终报告）
总费用       ≈ Σ (input_step × in_price + output_step × out_price)
```

- `system_prompt + tool_definitions` **每步都重发**，N 步就是 N 次输入成本——最大且最可省的一块。
- `history` 累积，第 k 步带前 k-1 步全部工具结果，**二次放大**；工具返回越大涨得越快。
- N 是乘数：每多一步，不只是多一次输出，还多一次"系统提示+历史"的完整重发。
- 预算：**20 步 / 最多 10 次工具调用**、单工具 ≤2s、整会话 ≤60s（诊断 §2）。工具集按 14 限界上下文切片，诊断 §3 列 11 组、20+ 个工具，`trace_lookup` 首位注册。

**② 草稿能力 `drafting`（固定 4 步链，不是 ReAct）**——`resolve_evidence → retrieve_knowledge → generate → validate_archive` 顺序 `await`，**只有 `generate` 一步调 LLM**（草稿 §1.2）：

```
总输入 ≈ system_prompt + 证据子集 + 检索到的历史文档（可能很大）+ Draft schema
总输出 ≈ 一份强约束 Draft（Pydantic 校验，requires_confirmation=True）
```

成本集中在**单次大输入 + 单次结构化输出**，没有滚雪球。杠杆是"喂给模型的历史文档要裁剪"和"Content Block 结构化输出约束长度"，**不是省步数**；预算 **8 步**。

**③ 编排能力 `orchestration`（`flows/` 应用层代码，不是 ReAct）**——确定性节点（plan / query+compare / barrier / gate / 动作卡 / 写落库）全是代码；只在非确定分支 `await` 诊断 / 草稿能力节点（编排 §0、整体选型 §4）。预算 **40 步 / 300s**（不含等人确认，那部分归 MES 工单侧 SLA）。成本 = Σ(被委托能力节点的成本)，PASS 路径上这一项为 0。

**④ 检索分派（网关 → `agent-service` 单层 flow）**——`RETRIEVAL_REQUEST` → `POST /agent/retrieval`，`version_resolve → trace_lookup → search_docs` 确定性 flow + 单层 ReAct 只决定"是否补查、换哪个种子"（ADR-13）。推理深度 **≤3 次工具调用**、`max_tool_calls=2` 由网关透传但不放大、检索子调用 15s / 整问答 70s（网关 §2 / §5 / §6.1）。网关**不传消息历史**，所以没有"跨轮历史滚雪球"这块成本。

### 2.2 降本杠杆对照表

| 层 | 杠杆 | 砍的是哪块 | 当前设计承载 | 主要适用形态 | 收益预期 🔴 |
|----|------|-----------|-------------|-------------|------------|
| **架构层** | 代码节点不调 LLM | 整次调用归零 | `flows/` 确定性节点（编排 §0、ADR-05 三问） | 编排 | 最大（换线 PASS 时 LLM=0，设计目标） |
| **架构层** | 入口网关规则优先 + 确定性分派 | 整次多步循环归零 | `agent-gateway-service`（网关 §4 / §5） | 入口 | 大（规则命中 0 次 LLM） |
| **架构层** | 单层编排、不叠加第二层 ReAct | 重复的模型往返 | ADR-13 编排权唯一 | 检索 | 大（避免 trace 嵌套 / token 双倍） |
| **模型层** | Model 分层路由（按 capability/phase） | 每 token 单价 | `Model` 抽象 + 启动校验（整体选型 §3.5.1） | 全部 | 中 |
| **提示词层** | **prompt caching** | 重复的系统提示+工具定义 | 自定义缓存标记 Middleware + `CacheControl` | 诊断（多步） | 大（多步 Agent 杀手锏） |
| **提示词层** | 工具描述精简 + 按 capability/tenant 裁剪 | `tool_definitions` | `Toolkit` 装配处可见性收敛 + `PermissionEngine` 执行层校验 | 诊断、编排 | 中 |
| **循环层** | 工具结果压缩 / 超大结果卸载 | `history` 累积 | `Offloader` + `ToolOffloadMiddleware` + `OffloadCompactPolicy` | 诊断 | 大（多步累积场景） |
| **循环层** | 历史 `tool_result` 降级为引用块 | 多轮回灌体积 | `history_rehydration_policy`（记忆系统 §3.10.5） | 多轮会话 | 中 |
| **循环层** | 并行工具调用减少往返 | 步数 N | `Toolkit` 工具执行节点 `asyncio.gather`（记忆系统 §4.5） | 诊断 | 中 |
| **循环层** | 早停 / 收敛检测 + 分能力预算上限 | 步数 N | 自定义早停 Middleware（记忆系统 §4.4） | 诊断 | 中 |
| **缓存层** | 工具结果版本化缓存（按 `data_class` 闸门） | 整次工具调用 + 其结果进历史 | `ToolResultCache` + `DataScopePolicy` + `AnswerValidator` | 诊断、检索 | 中 |
| **缓存层** | 答案缓存（仅 `AS_OF`） | 整次会话归零 | 网关 `QueryCache` + `data_scope` 派生 | 网关 | 中（MES 有风险，严格受限） |
| **输出层** | Content Block 结构化输出 + 按步 `max_tokens` | 输出 token / schema 重试 | Pydantic BaseModel + `Msg`；`model_pricing` | 全部 | 小 |
| **观测闭环** | 成本归因 + 漂移 + 评测门禁 | 持续优化 | `EvalRunner` / hard gate / soft gate / shadow→canary | 持续 | 持续 |

> 收益预期列全部 🔴 待实测，仅表示杠杆量级方向，不是承诺数字；全部条目 📐 尚未落地。

---

## 3. 降本杠杆分层模型

当前设计的五层杠杆（整体选型 §3.5.1）按"**少调 > 少发 > 调便宜的**"排，下层为上层兜底：

| 层 | 名称 | 核心问题 | 框架 / 本项目承载 | 优先级 |
|----|------|---------|------------------|--------|
| **架构层** | 代码节点 + 网关分派 | 这一步到底要不要调 LLM？ | `flows/` 应用层 Python + 网关确定性 HTTP 分派（均非框架原语） | 最高——不调 = 0 成本 |
| **模型层** | Model 分层路由 | 调的话用哪个规格的模型？ | `Model` 抽象 + 本项目启动校验 / `EvalGate` | 高——单价直接打折 |
| **提示词层** | prompt caching / 工具集裁剪 | 每步重发的固定块能不能省？ | 自定义缓存标记 Middleware + `Toolkit` 装配 | 高——多步循环的稳定节省 |
| **推理循环层** | 压缩 / 早停 / 并行 | 步数和每步的上下文能不能压？ | `Offloader` + `ToolOffloadMiddleware`（框架）+ 本项目策略 | 中——按场景收益差异大 |
| **缓存层** | 工具结果 / 答案缓存 | 这次的调用能不能整个跳过？ | 自定义 Middleware + Redis + `DataScopePolicy` 闸门 | 中——命中即归零，但有正确性风险 |
| 闭环 | 观测驱动 | 省了之后怎么验证没退步、怎么持续找下一桶？ | `TracingMiddleware` + `MetricsCollector` + 自建 `EvalRunner` | 持续 |

**两个顺序别混**：

- **杠杆量级优先级**是上表从上到下（架构层归零最狠），也是整体选型 §3.5.1「优先级从严到宽」的口径。
- **落地启用顺序**按风险从低到高：先测（§12 阶段一）→ 架构 / 提示词 / 循环（不动模型规格）→ **最后才动模型规格与答案缓存**。原因不是收益小，而是换模型碰质量红线、答案缓存碰时变性红线，都要评测背书。这就是"换便宜模型是最后一步"的准确含义。
- 与记忆系统的对应：本篇的提示词层 / 循环层 = M2「上下文窗口记忆」，缓存层 =「自治跨会话缓存」（记忆系统 §2.1 / §11）。两套说法指同一批组件，别重复建设。

下面逐层展开。所有代码骨架与 [诊断能力 §7](FactoryAgent/FactoryAgent-诊断能力.md) / [可观测性 §8.4](Agent可观测性-设计与实现方案.md) / [记忆系统 §4](Agent记忆系统-设计与实现方案.md) 的抽象对齐：`Model` 抽象、`Toolkit` / `Tool`、`Offloader` / `ToolOffloadMiddleware`、`TracingMiddleware`、自定义 Middleware 链、本项目的 `ObservabilityPort`、`ObservabilityContext`、`prompt_version`、`TenantContext`、`version_anchor`。**方法签名与 `app/...` 路径都是本项目的设计命名与待建路径，非框架 API。**

---

## 4. 架构层（代码节点 + 入口网关）：能不调 LLM 就不调

这是优先级最高、MES 场景下最该讲的杠杆。**一次调用都不发生，成本就是零**，比任何 prompt 优化都狠。

### 4.1 代码节点不调 LLM（编排能力的设计原则；📐 尚未落地）

[编排能力 §0](FactoryAgent/FactoryAgent-流程编排能力.md) 已明确推翻"5 个换线 sub-agent"与"4 类独立 sub-agent（根因 / 隔离范围 / 追溯 / 生成）"两套旧设计——它们与诊断、草稿形成两套实现。现在只有一条规则：

> **编排 flow 里需要"判根因"就调诊断能力，需要"产出可下达的卡"就调草稿能力，需要"确定性判定"就写普通代码。编排层自己不实现第四种推理。**

判定标准是三问「输入是否开放 / 是否需推理生成 / 分支是否难穷举」，三问皆否走代码节点（**ADR-05 声明：三问是本仓库自有判据，官方文档没有给出量化标准**）。换线 5 步里编排、结构化比对、barrier、gate、写落库全是代码节点。

降本视角的补充：

- 代码节点不只是"防越界"，它同时是**最大的降本手段**——把它单列出来强调，体现"架构降本 > 提示词降本"。
- **设计目标**：换线全程 PASS 时能力节点不触发，`agent_llm_invocation_total = 0`（底座 §4.3、整体选型 §4.3、统一架构 §5）。⚠️ 这是**设计目标，不是既成事实**：相关指标尚未埋点，编排能力完全未实现（编排 §6「诚实边界」）。
- 可观测验证：`agent_llm_invocation_total` 与 `agent_step_total{step_kind=CODE}` 的占比是编排能力的成本健康指标（可观测性 §5.2）。**指标 label 是 `step_kind`**，不是 `node_type`；`node_type` 是业务表 `node_trace` 的列名，别混用。
- **反向约束**：不要为了"降本"把本该 agent 做的开放推理硬塞进代码节点（决策树）。[编排 §10 Q&A](FactoryAgent/FactoryAgent-流程编排能力.md) 与 [能力边界与选型决策卡 §1](FactoryAgent/FactoryAgent-能力边界与选型决策卡.md) 已讲清"决策树永远落后于现场"——降本不能以牺牲根因覆盖率为代价。

### 4.2 入口网关前置分流（按当前网关形态重写）

独立微服务 `agent-gateway-service` 是"一个入口路由到各能力"的落地形态（[网关](Agent入口网关-意图路由.md) 文首；📐 设计阶段的引入规划，不是线上已落地能力）。**降本关键不是"便宜分类器"，而是分派本身是确定性的、不挂 ReAct**：

```text
用户自由文本
  ↓
[IntentRouter] 规则优先（命中即返回，0 次 LLM）
  │              规则命中不了 -> 小模型 + Content Block 强制 Enum 兜底 -> 仍不确定 UNKNOWN 转人工
  ↓  确定性分派（HTTP，不做多步组合、不选工具）
  ├─ RETRIEVAL_REQUEST -> POST /agent/retrieval  (agent-service 单层检索 flow，≤3 次工具调用)
  ├─ DIAGNOSIS_REQUEST -> DelegationClient.invoke_diagnosis(...)  显式委托
  ├─ DRAFT_REQUEST     -> DelegationClient.invoke_draft(...)       显式委托
  └─ UNKNOWN           -> 转人工，不硬答

结构化触发（事件 / 业务端点 / flow 节点）-> 编排能力 flows/  <- 不经过网关意图枚举
```

与旧版"RAG 直答"写法必须区分清楚：

- **不是"RAG 单次检索 + 单次生成直答"**。`rag-service` 已收敛为文档知识服务，只提供 `search_docs` / `fetch_doc_answer`，`AUTO` 多步编排下线（ADR-13）；制造追溯迁出为只读工具（归属待定）。检索请求进的是 **`agent-service` 编排层的单层检索 flow**，编排权唯一在 `agent-service`。
- **不是第二次 ReAct**。同一请求只允许一层 ReAct；网关不推理、不选工具、不传消息历史，只传问题、种子、版本锚点、`session_id` 与 `max_tool_calls`（网关 §5 / §6.1）。
- **编排能力不在网关意图枚举里**：意图只有 3 类 + `UNKNOWN` 兜底，编排由结构化事件 / 端点 / flow 节点触发（整体选型 §4）。
- **规则优先是降本主力**：高频问题走规则，0 次 LLM；只有规则命中不了才用一次小模型（Haiku / DeepSeek / 本地小模型），且 `GatewayIntent` 固化为 Enum 避免编造意图（网关 §4）。
- **答案缓存按策略，不是无条件命中**：`data_scope` 只由 `as_of` 派生，**仅 `AS_OF` 允许答案缓存，`CURRENT` 一律跳过（即使请求带 `version`）**（网关 §5 / §9、记忆系统 §3.10.1）。详见 §8.2。
- **embedding 语义路由属 📐 演进方向**：[Agent路由与多轮上下文](Agent路由与多轮上下文-市面对照与演进方向.md) §7 的档 0（embedding 语义路由 + top-K + 薄锚点缓存）**尚未落地、未拍板**，不能写成现状。
- **框架口径**：AgentScope 2.0 从未提供 `Handoffs` / `Routing` 移交原语；`ModelRouterMiddleware` 做的是**模型路由**，不是能力移交（ADR §13 4e、底座 §8）。入口分诊 = 网关的"规则 + LLM 兜底分类器"（不是 ReAct）；检索内工具选择 = `agent-service` 单 Agent ReAct + `Toolkit`；跨能力委托 = **显式服务调用**，传 `evidence_set_id` / `session_id` / `traceparent`。
- **MES 约束**：网关无写工具、不领用写令牌、不调过点放行 / 拦截 API（网关 §5）。误路由最坏是"简单问题走了诊断多绕几步"或"复杂问题被判检索深度不够"，前者浪费钱、后者降体验，都不碰红线；`UNKNOWN` 与调用失败一律转人工。

### 4.3 离线 / 异步预处理：能预计算的不放实时路径

- 草稿能力的 SOP 升版草拟可订阅 `ProcessRouteActivated` / `process.route.activated` 只读领域事件**异步触发**（[草稿 §2 / §4.5](FactoryAgent/FactoryAgent-草稿编写能力.md)），不在工艺切换的实时关键路径上——既不进过点主事务，也把 LLM 调用挪到非实时窗口，可错峰、可限流、可重试；主动草拟仍需人确认。
- 短后台任务与定时巡检由框架 `BackgroundTaskManager` / `SchedulerManager` 托管（整体选型 §3.5.3）；**不引入 Celery**，也不做 agent 侧挂起 / 断点续跑（ADR-05 / §5 决策 11）。
- ⚠️ "防错即时辅助"这条路线在评测侧 [端到端与过程二分评测设计 §2.3](../RAG与Agent评测/端到端与过程二分评测设计.md) 的 F6 标注为**暂不建**（现有设计未定义该路线指标），本篇只保留"预计算 + 缓存命中即推"的思路，不把它写成已规划能力。
- **成本口径**：把调用挪到异步窗口，减少的是实时路径上的同步 LLM 压力，不是总 token 数；收益要在 `agent_cost_usd_total` 上按时间窗归因，别算成"省了一次调用"。

---

## 5. 模型层：Model 分层路由 / Cascading

### 5.1 思路

现有 AgentScope **`Model` 抽象**已支持 provider 可插拔（[整体选型 §2.3](../整体技术选型与模块划分.md)）：Claude sonnet 级主力推理 / 通义千问多 provider / DeepSeek·Haiku·本地小模型用于分类与降级 / Opus 级 judge / Embedding。降本视角进一步：**不是全会话用一个模型，而是按 capability / phase 分派不同规格**。

| 任务 | 模型规格 | 理由 | 是否计入在线成本 |
|------|---------|------|-----------------|
| 网关兜底意图分类 | 小 / 便宜 | Enum 强约束的单次分类，规则命中时干脆 0 次 | 是（规则命中则 0） |
| 检索 flow 的"是否补查"决策 | 小 / 中 | 单层 ReAct，≤3 次工具调用 | 是 |
| 诊断能力 5M1E 跨源关联推理 | 中-大 | 开放取证，质量红线最紧 | 是 |
| 草稿能力 `generate` 一步 | 大 | 开放生成，草稿采纳率是硬指标 | 是 |
| 编排能力的能力节点 | 跟随被委托能力 | 编排层自己不选模型（编排 §0） | 是 |
| 离线 judge / 评测 | Opus 级 | 判报告质量，**离线跑集** | 否（单独记账） |

### 5.2 实现：`Model` 抽象之上的分层路由 + 评测门禁

整体选型 §3.5.1 把这一格命名为「**Model 分层路由（原 ModelRouter）**」，承载是「`Model` 抽象 + 启动校验（业务自定义）」。下面沿用 `ModelRouter` 作类名仅为对齐既有引用（[网关 §4](Agent入口网关-意图路由.md) 引用本篇 §5.2），它**不是框架路由原语，也不是独立自研子包**。

```python
# app/infrastructure/cost/model_routing.py（设计目标路径；本项目封装，非框架 API）
from dataclasses import dataclass

@dataclass(frozen=True)
class ModelChoice:
    model_name: str
    prompt_version: str      # 模型与提示词绑定版本，评测回归的基准

class ModelRouter:
    """按 capability/phase 查表分派 Model；启动时逐条过 EvalGate，未过评测即拒绝启动。"""

    def __init__(self, routing_table: dict, eval_gate: EvalGate) -> None:
        # routing_table: {(capability, phase): ModelChoice}
        self._table = routing_table
        self._eval_gate = eval_gate

    def for_capability(self, capability: str, phase: str = "default") -> ModelChoice:
        choice = self._table.get((capability, phase)) or self._table[(capability, "default")]
        if not self._eval_gate.passed(capability, choice.model_name, choice.prompt_version):
            raise ModelDowngradeBlocked(
                f"{capability} 降级到 {choice.model_name} 未过评测门禁，禁止启用"
            )
        return choice
```

```python
# app/infrastructure/obs/eval/gate.py（设计目标路径；自研 EvalRunner 之上的 MES 专用层）
class EvalGate:
    """换模型 / 换提示词 / 换工具路由前查回归结果；安全硬断言零容忍，质量软门禁超阈值不放行。"""

    def __init__(self, eval_run_repo: EvalRunRepo, thresholds: dict) -> None:
        self._repo = eval_run_repo
        self._thresholds = thresholds   # {capability: {pass_k_min, ece_max, stale_fact_rate_max, ...}}

    def passed(self, capability: str, model: str, prompt_version: str) -> bool:
        latest = self._repo.latest(capability, model, prompt_version)
        if latest is None:
            return False                  # 没跑过评测 = 不放行
        th = self._thresholds[capability]
        return (latest.pass_k >= th["pass_k_min"]            # ADR-10：pass^8 而非 pass@1
                and latest.ece <= th["ece_max"]              # 可观测性 §10.4：ECE < 0.1
                and latest.stale_fact_rate == 0              # 记忆系统 §3.10.6：hard gate
                and latest.cost_per_successful_task <= th["cost_max"])
```

- **核心纪律**：降级模型 = 改 `routing_table`，但启动时对每条记录查 `EvalGate.passed`，没过评测的抛异常拒绝启动。**省钱不能绕过质量门禁**——这是 MES 场景降本与通用降本的本质区别。
- **门禁的真实形态**（ADR-10、可观测性 §10.2）：评测栈**自建**（自研 `EvalRunner` + 三层判定 + pass^k + pytest CI 门禁，judge 用独立包 `py-openjudge`；`agentscope.evaluate` 在 2.0 已删除未回归）；安全断言（版本锚点 / 写越界 / 越权 / PII / 证据空 / `stale_fact_rate`）为 **hard gate**，质量指标为 **soft gate**；模型 / prompt / 工具路由 / 审批策略**四类变更**一律走 shadow → canary → 放量。`EvalGate` 是这套机制之上的 MES 专用封装，不要写成框架能力。
- `ModelChoice` 绑定 `prompt_version`：换模型往往要同步调提示词，三者（模型 / 提示词 / 评测）一起版本化回归；不改提示词版本号就不许改提示词（可观测性 §8.3）。
- **单 Agent 基线先建**（ADR-10 决策 3）：分不清"架构更优"与"多烧算力"之前，别把并行 / 多 Agent 当降本里程碑。

### 5.3 cascading（级联）：先小后大

```text
[小模型诊断] -> 置信度 >= 阈值? -> 用小模型结果（省）
                          └─ 否 -> [大模型重跑同一会话] -> 用大模型结果（贵但准）
```

- 适合置信度标定成熟后做；但 **ADR-11 明确：置信度由规则算，标定用 Platt / isotonic 在评测集上拟合，不采信 LLM 自报数值**。cascading 的阈值必须来自标定曲线，不能来自模型 `confidence` 字段。
- **风险**：重跑 = 双倍调用；小模型长期不达标反而更贵，且推高 `cost-per-successful-task`。cascading 收益完全取决于小模型"够用"的命中率。🔴 命中率阈值待线上标定，**初期不上 cascading，先做静态分层路由（§5.2）**。

---

## 6. 提示词层：把每步重发的固定块省下来

ReAct 每步重发的 `system_prompt + tool_definitions` 是诊断能力里最大且最稳定的浪费，这层是**多步 Agent 降本的杀手锏**。草稿 / 编排 / 检索不是多步 ReAct，这层对它们的收益有限，别硬套。

### 6.1 Prompt Caching（核心杠杆）

#### 6.1.1 原理

Anthropic / 部分国产模型的 prompt caching：把一段稳定前缀（system prompt + 工具定义 + few-shot）标记为可缓存，首次写入按 1.25×（5 分钟 TTL）或 2×（1 小时 TTL）计费，**后续命中按 ~0.1× 计费**（记忆系统 §4.3）。🔴 provider 支持范围与实时单价待核实，`model_pricing` 需定期同步（可观测性 §8.2）。

对诊断 ReAct：system prompt + 工具定义在整会话内**完全不变**，天然适合缓存。N 步会话里，这块从"发 N 次全价"变成"写 1 次 + 读 N-1 次缓存价"。

#### 6.1.2 实现：`Model` 调用链上的缓存标记 Middleware

[可观测性 §8.4](Agent可观测性-设计与实现方案.md) 的 `ModelObsMiddleware` 是挂在 `Model` 抽象调用链上的统一观测段；[记忆系统 §4.3](Agent记忆系统-设计与实现方案.md) 明确"真实 LLM 接入时把缓存标记插在模型调用的 Middleware 链上，**与观测埋点同一处生效**"。所以不新造一个孤立的缓存 Middleware，而是在同一条链上扩展观测段：

```python
# app/infrastructure/cost/cache_control.py（设计目标路径；与记忆系统 §4.3 同一数据形状）
from dataclasses import dataclass
from typing import Literal

@dataclass
class CacheControl:                  # provider 侧缓存标记的数据形状（非框架 API）
    type: Literal["ephemeral"] = "ephemeral"
    ttl: Literal["5m", "1h"] = "5m"

def mark_system_prompt_cache(prompt: str, ttl: str = "5m") -> dict: ...   # system prompt TTL 5min
def mark_tools_cache(tools: list[dict], ttl: str = "1h") -> list[dict]: ...  # 工具定义 TTL 1h
```

```python
# app/infrastructure/obs/llm_obs.py
# 本篇对 ModelObsMiddleware 的设计扩展（非可观测性 §8.4 现役签名，落地时须同步回事实源）
class ModelObsMiddleware:
    def __init__(self, obs: ObservabilityPort, model_name: str,
                 prompt_version: str, cache: CacheControl | None = None) -> None:
        self._obs, self._model, self._prompt_version, self._cache = obs, model_name, prompt_version, cache

    async def on_model_call(self, next_call, messages, **kw):
        # 1. 只给稳定块打标记；消息块形状由 provider 适配层收敛，非框架 API
        if self._cache is not None:
            messages = [mark_system_prompt_cache(m, self._cache.ttl) if is_system(m) else m
                        for m in messages]
            kw["tools"] = mark_tools_cache(kw.get("tools", []), "1h")
        t0 = time.perf_counter()
        resp = await next_call(messages, **kw)
        usage = resp.usage                        # 框架现役字段：input_tokens / output_tokens
        self._obs.llm_called(
            model=self._model, prompt_version=self._prompt_version,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            finish_reason=resp.finish_reason,
            # ⚠️ 缓存 token 是本篇提议的 provider 扩展字段，不是 Msg.usage 现役字段：
            #    provider 不回传就留空，绝不推算冒充实测（见 §11.1）
            cache_read_tokens=getattr(usage, "cache_read_tokens", None),
            cache_write_tokens=getattr(usage, "cache_creation_tokens", None),
        )
        return resp
```

> `CacheControl` 是**本项目 dataclass**（记忆系统 §4.3），不是带 `apply()` 的框架类；`CacheMarkMiddleware` 是旧版孤名，本篇不再使用。缓存标记 Middleware / 自定义 Middleware 链均 📐 尚未接线。

#### 6.1.3 MES 约束与注意点

- **缓存块的内容必须真的稳定**：system prompt、工具定义、few-shot 可以缓存；**动态上下文（用户问题、工具结果）绝不进缓存块**，否则命中错数据。只标 `system` / `tools` 块。
- **工具定义变了 = 缓存失效**：按阶段动态裁剪工具集（§6.2.3）会破坏命中。**结论（记忆系统 §8.7）：固定工具集 + prompt 缓存通常优于动态工具集 + 缓存未命中**，动态裁剪只在工具集极大（编排覆盖 14 个上下文）且 token 实测显示工具定义占比过高时才考虑。
- **`prompt_version` 变 = 缓存自然失效**：缓存按内容 hash，改提示词后旧缓存不再命中，无需手动清理。
- **provider 适配**：不是所有 provider 都支持 caching。`CacheControl | None = None` 时降级为不缓存，业务无感。
- **TTL 选择**：5min 适合单会话内（诊断整会话 ≤60s）；1h 适合跨会话复用同一 system prompt / 工具定义。🔴 TTL 策略待按会话时长分布与 provider 实际支持定。
- **观测口径纪律**：`Msg.usage` 当前只有 `input_tokens` / `output_tokens`；`cache_read_tokens` / `cache_creation_tokens` **不是框架现役字段**，是本篇提议的 provider 扩展。拿不到就记空值，用"输入 token 下降"间接验证收益，不得编缓存命中率数字。

### 6.2 工具描述精简 + 工具集裁剪

#### 6.2.1 精简 description

`ToolDescriptor.description`（[诊断能力 §3 / §4.2](FactoryAgent/FactoryAgent-诊断能力.md)）是**每次都发给模型**的。纪律：

- description 只写"这个工具查什么、何时该用、入参约束"，不写实现细节、不写废话。
- 工具入参用 Pydantic `args_schema` 生成 schema，**不要在 description 里重复参数说明**。
- 工具粒度 = 实体 × 问题类型，不是 API 数量；分页 / 字段裁剪 / 排序由 ACL 内部定死，不上 schema，返回摘要字段 + `_truncated` / `_omitted_count` 标注（诊断 §3）。

#### 6.2.2 按 capability + tenant 裁剪（当前设计已有）

`Toolkit` 装配处按 `capability + tenant scope` 收敛可见性（记忆系统 §8.6）：工具注册对齐 14 限界上下文，诊断 toolset 是 11 组、20+ 个工具（**不是旧版写的 9 个**），`trace_lookup` 首位注册。⚠️ 可见性收敛**不是安全边界**——真边界是工具网关的一次性动作令牌 + 执行层 `PermissionEngine` 校验（ADR-04），别把"少挂几个工具"当权限隔离。

#### 6.2.3 按推理阶段裁剪（原 `PhaseToolBinder`，默认关闭）

`Toolkit` 动态绑定 + 自定义 Middleware 可做到同一 capability 内按阶段暴露不同工具子集（整体选型 §3.5.1）。**取舍**：阶段裁剪让工具定义每阶段不同 → **破坏 prompt caching 命中**（§6.1.3）。只有工具集大到"工具描述 token > 缓存损失"才值得；诊断不值得，编排覆盖 14 上下文可能值得。🔴 是否启用按 token 实测决定，**默认关闭**。多数情况下先用 §6.1 把 token 量出来再说。

### 6.3 few-shot 按需注入

- 只在模型表现不稳的场景给 few-shot（如 5M1E 分类边界模糊的 case）。
- few-shot 进缓存块（随 system prompt 一起缓存），不要每步重发。
- 示例数量按评测收敛——加到第 N 个准确率不再涨就停；变更即改 `prompt_version`，过评测回归（§10.3）。

---

## 7. 推理循环层：压步数、压每步上下文

### 7.1 工具结果压缩 / 超大结果卸载（模型看摘要，trace 落全文）

#### 7.1.1 问题

诊断 ReAct 第 k 步要带前 k-1 步全部工具结果。`query_pass_records` 返回大批过点记录、`query_equipment_telemetry` 返回长时序时，history 二次放大；模型其实只需要关键字段。

#### 7.1.2 当前设计的承载与命名

| 角色 | 名字 | 归属 |
|------|------|------|
| 超大结果卸载 / 回灌前替换 | `Offloader` + `ToolOffloadMiddleware` | **框架已核实能力**（ADR §13 4b；⚠️ `app.*` 私有路径，须包适配层） |
| 裁哪些字段、截多长 | `OffloadCompactPolicy` + `FIELD_WHITELIST` + `LIST_TRUNCATE = 5` | **本项目压缩策略**（记忆系统 §4.2；设计目标路径 `app/infrastructure/cost/result_compactor.py`） |
| trace 落全文 | `ToolObsMiddleware` → `tool_call_trace.output_payload` 存 **FULL view** | 本项目观测段（可观测性 §14.3） |
| 唯一取数通道 | `ToolExecHook`（权限 + 取数闸门 + 执行 + 落 trace + 回灌） | 本项目封装（记忆系统 §2.3 / §3.10.4） |

> ⚠️ `ResultCompactor` 是**被替换的旧名**（底座 §2.10「原 ResultCompactor → Offloader 接口 + ToolOffloadMiddleware」），只保留为设计目标**文件名**，不再当现役类名；`compact_for_llm()` / `app/infrastructure/ai/offload_policy.py` 属旧版异构写法，一律对齐到 `OffloadCompactPolicy.compact(tool_name, view)`。

```python
# app/infrastructure/cost/result_compactor.py（设计目标路径；本项目策略，非框架 API）
FIELD_WHITELIST: dict[str, list[str]] = {
    "query_pass_records": ["sn", "work_order_id", "station_id", "equipment_id",
                           "route_version", "decision", "blocking_reason"],
    "query_test_results": ["test_id", "station_id", "test_type", "raw_verdict"],
    "trace_lookup": ["serial_no", "evidence_set_id", "version", "version_kind", "version_ref_id"],
}
LIST_TRUNCATE = 5                        # 列表最多保留前 5 项

class OffloadCompactPolicy:             # 由 Offloader / ToolOffloadMiddleware 调用
    def compact(self, tool_name: str, view: Any) -> dict:
        # 1. 字段裁剪（白名单） 2. 列表截断 3. 标注 _truncated / _omitted_count
        ...
```

```python
# app/infrastructure/ai/tool_exec_hook.py（设计目标路径；本项目封装，非框架 API）
class ToolExecHook:
    """唯一取数通道：权限 -> 取数闸门 -> 执行 -> trace 落全文 -> 回灌压缩版。"""

    async def run(self, call, ctx) -> dict:
        tool = self._registry.get(call.name)
        # 1. PermissionEngine + capability/tenant scope 校验（叠加层；主闸在工具网关，ADR-04）
        # 2. DataScopePolicy.resolve(...)：LIVE_ONLY 且 CURRENT/跨 turn -> 强制实时，缓存命中作废
        view = await self._acl.get_or_compute(tool, call.args, ctx)      # FULL view
        trace_id = await self._trace_repo.save_ok(..., view=view, ...)   # ★ M3 落全文，证据链完整
        compacted = self._policy.compact(tool.name, view)                 # ★ 只压缩喂模型这一路
        return {"trace_id": trace_id, "data": compacted}                  # Offloader 卸载超大整包
```

#### 7.1.3 落地分阶段（记忆系统 §2.3 / §4.2）

📐 **策略尚未落地**：先用确定性假模型把全文直接喂模型（此时不烧真实 token），验证流程正确性；接线 `Offloader` / `ToolOffloadMiddleware` 后才走压缩 / 卸载路径。**先正确性、后降本有效性**，两者解耦（记忆系统 §10）。

#### 7.1.4 MES 约束

- **证据链红线**：`tool_call_trace.output_payload` 存压缩前 **FULL view**；工程师 UI 回溯证据读 trace 全文，`evidence` 字段只存 `trace_id` 列表（可观测性 §7.2）。压缩只作用于"喂模型"这一路。
- **裁剪规则要评测**：`FIELD_WHITELIST` 变更过 `EvalRunner` 回归（模型 / prompt / 工具路由 / 审批策略四类变更走 shadow→canary），裁掉的字段不能是根因推理必需的。
- **截断要标注**：`_truncated` / `_omitted_count` 让模型知道数据不全，必要时换更窄查询条件重查，避免基于"看起来只有 5 条"的错误前提推理。
- **多轮回灌降级**：框架消息历史永久留存且含 `tool_result` 块，跨页面续聊会把上一轮"齐套数 = 3"原文喂回模型。`history_rehydration_policy`（记忆系统 §3.10.5）把它降级为引用块 `{hint, trace_id, observed_at, usable_as_fact: false}`——**保留叙事与指代、取消其"可作事实断言"资格**，既省 token 又守时变性红线；出口由 `AnswerValidator` 兜底。

### 7.2 并行工具调用减少往返

- ReAct 每次往返都要重发整个上下文。统一 `Agent` 内置 ReAct 一次产出多个独立 tool call 时，`Toolkit` 工具执行节点用 `asyncio.gather` 并发执行后一次性回灌（记忆系统 §4.5），把"3 次往返"压成"1 次"。诊断里"查同批次锡膏 + 查贴片机参数"本就独立（诊断 §2）。
- 只需在 system prompt 里明确鼓励："对相互独立的查询，一次性发起多个工具调用，不要串行等待"；**纯提示词侧 + 框架已支持**，零基础设施成本。
- ⚠️ **别混两种 `asyncio.gather`**：本节是 `Toolkit` 内并发多个 tool call；编排层的是并发**各自独立的 `Agent.reply()`**（整体选型 §5 决策 1），两者不在一个层级。
- 观测验证：`agent_tool_call_total` 不变，但 `agent_llm_invocation_total`（往返次数）下降——用这个比值量化并行度收益。
- **约束**：有数据依赖的（先查过点拿 `route_version` 再查工艺）必须串行；并行调用仍受 `Permission` 判定、`version_anchor` 校验与 `DataScopePolicy` 取数闸门约束，红线不因并行松动。

### 7.3 早停 / 收敛检测

#### 7.3.1 分能力预算上限（硬兜底）

旧版把"预算上限 = 20"当全局值，当前设计是**按能力装配**（诊断 §2、草稿 §1.2、编排 §6.4、网关 §6.1）：

| 形态 | 预算上限 | 超时 |
|------|---------|------|
| 诊断 `diagnosis` | **20 步** / 最多 10 次工具调用 | 单工具 ≤2s、整会话 ≤60s |
| 草稿 `drafting` | **8 步** | 以能力方案为准 |
| 编排 `orchestration` | **40 步** | 300s（不含等人确认） |
| 检索分派 | `max_tool_calls=2`、≤3 次工具调用 | 检索子调用 15s / 整问答 70s 🔴 待压测标定 |

`agent_step_budget_hit_total` 是命中次数指标；SLO 命中率 <2%，10min 命中率 >5% 报 P2（可观测性 §5.4）。预算是**防失控**兜底，不是"省着用"的优化；它是本项目在能力装配处配置的约束，**不是框架开关名**。

#### 7.3.2 早停：够了就收口

对齐 [记忆系统 §4.4](Agent记忆系统-设计与实现方案.md) 的自定义早停 Middleware：

```python
# app/infrastructure/cost/early_stop.py（设计目标路径；本项目封装，非框架 API）
class EarlyStopDetector:
    def __init__(self, max_tool_calls: int = 8, min_evidence: int = 2): ...

    def should_stop(self, tool_call_count, evidence_count,
                    model_self_assess=None) -> tuple[bool, str]:
        if tool_call_count >= self._max_tool_calls:                       # 工具调用上限
            return True, "budget"
        if model_self_assess is True and evidence_count >= self._min_evidence:   # 自评证据充分
            return True, "enough_evidence"
        if evidence_count >= self._min_evidence * 2:                      # 证据冗余
            return True, "redundant"
        return False, ""
```

- **system prompt 明确收口条件**："已收集到能形成带证据的 5M1E 假设时，停止调用工具，输出报告，不要为追求穷尽反复查询。"
- **冗余检测**：连续查同一上下文 / 查询结果未被任何假设引用 → 提前收口。
- ⚠️ `model_self_assess` 只作**收口提示**，不视为置信度：**置信度由规则算、标定用 Platt / isotonic，不采信 LLM 自报数值**（ADR-11）；最终答案仍要过 `AnswerValidator` 出口校验。
- **约束**：早停不能误伤——证据不足就早停会降根因报告质量。触发后不是直接出报告，而是**转人工**（`needs_human_review`），与可观测兜底（可观测性 §12.1）一致：宁可让人判，不硬答。

---

## 8. 缓存层：命中即归零，但 MES 有正确性风险

### 8.1 工具结果缓存（按 `data_class` 闸门，安全）

#### 8.1.1 思路与当前设计的取数闸门

[记忆系统 §3.10](Agent记忆系统-设计与实现方案.md) 把"能不能用缓存"从 TTL 配置升格为**语义要求**：共享内核 `DataScopePolicy` 给每个工具 / 字段定 `data_class`，默认 `LIVE_ONLY`（fail-safe）；`ToolExecHook` 是**唯一取数通道**（与 `Permission` 同层，模型绕不过），出口由 `AnswerValidator` 确定性校验证据出处。

| 工具 | `data_class` | 缓存策略 | key 维度 |
|------|-------------|---------|---------|
| `get_process_route(route_id, version)` | `IMMUTABLE`（需 `version_anchor`） | 长 TTL（工艺版本不可变，新版本即新 key） | `route_id + route_version + version_anchor + tenant` |
| `query_material_batch(batch_no)` | 🔴 待定（台账变更慢） | 中 TTL 或 `scope="turn"` | `batch_no + tenant` |
| `query_pass_records(serial_no)` | `LIVE_ONLY` | **不跨 turn 缓存**；只允许 `scope="turn"` 请求级 memo | `serial_no + tenant + max_ts` |
| `query_device_params(asset_id, time_range)` 历史窗口 | `AS_OF_ONLY` | 历史窗口可长缓存；当前参数按 `LIVE_ONLY` | `asset_id + time_range + tenant` |
| `evidence_set_id` / `version_anchor` 引用 | `AUTHORITATIVE_RECORD` | 引用本身可存；指向的内容按各自 `data_class` | `session_id + evidence_set_id` |

**铁律**：`data_class == LIVE_ONLY` 且（`CURRENT` 或跨 turn）→ **强制实时，缓存命中也作废**。不是"TTL 设短一点"，是闸门直接不返回旧值。

#### 8.1.2 实现

```python
# app/infrastructure/cost/tool_cache.py（设计目标路径；本项目封装，非框架 API）
class ToolResultCache:
    """版本化工具结果缓存；能否命中由共享内核 DataScopePolicy 裁决，不由 TTL 表单独决定。"""

    async def get_or_compute(self, tool_name, args, version_anchor, ctx, compute_fn):
        decision = self._policy.resolve(ctx.request, tool_name, ctx.turn_id)
        if not decision.allow_cache:
            return await compute_fn()             # LIVE_ONLY + CURRENT/跨 turn：强制实时
        key = (f"tc:{ctx.tenant.tenant_id}:{tool_name}:"
               f"{hash(stable_json(args | {'version_anchor': version_anchor}))}")
        hit = await self._redis.get(key)
        if hit:
            self._obs.tool_cache_hit(tool_name, decision.data_class)
            return json.loads(hit)
        view = await compute_fn()                 # FULL view，trace 落全文不在这里省
        await self._redis.setex(key, decision.ttl_seconds, json.dumps(view.dump()))
        return view
```

> Redis 定位（ADR-07）：消息总线 `RedisMessageBus`（框架必填且只有 Redis 实现）+ 每租户并发限流信号量 + **工具结果短期缓存**；⚠️ Redis **不再兼作会话历史层**，会话与消息用 `AsyncSQLAlchemyStorage` 落 MySQL。另 HITL 确认令牌"存 Redis + TTL 30min"在整体选型 §2.5 标为【待确认设计假设】，本篇不把它当既成事实。

#### 8.1.3 MES 约束（版本一致性 + 时变性）

- **`version_anchor` 必须进 key**：不同版本不串台；`IMMUTABLE` 数据允许 `TOOL_CACHE` 来源，但 `AnswerValidator` 校验它必须带锚点。
- **持续追加的数据慎缓存**：过点记录、不良记录持续追加。`max_ts` 进 key 只是兜底，主约束是 `LIVE_ONLY` 闸门。
- **缓存失效与领域事件**：工艺版本不可变 → 新版本是新 key，无需失效旧 key；物料台账变更若发事件可订阅失效。🔴 失效策略按工具逐个定。
- **评测门禁**：评测集补 `stale_hit`（该查却用了旧值）/ `redundant_live`（该用记忆却重复查）/ `version_leak`（带 `version` 却被整体判成可缓存）三类反例，`stale_fact_rate = 0` 作 `EvalGate` **hard gate**（记忆系统 §3.10.6）。没有这道门禁，"保证"只是设计宣言。
- `ToolCallTrace` 加 `data_scope` / `data_class` / `source` / `turn_id` 四列，让"用了缓存还是实时"可回溯。

### 8.2 答案缓存 / 语义缓存（严格受限）

#### 8.2.1 当前设计形态

网关侧 `QueryCache` 的 key 是「问题 + 租户 + 版本锚点」的**精确匹配**，不是 embedding 相似度语义命中；embedding 语义路由 / 薄锚点缓存属 [路由演进 §7 档 0](Agent路由与多轮上下文-市面对照与演进方向.md) 的 📐 演进方向、未拍板。

是否允许读写缓存，**只看 `data_scope`**（网关 §5 / §9、记忆系统 §3.10.1）：

```python
scope = resolve_data_scope(request)          # 纯函数，只看 as_of，不看 version，0 次 LLM
if scope.allows_answer_cache:                # AS_OF -> True / CURRENT -> False
    cached = await cache.get(request, tenant)
...
```

#### 8.2.2 MES 风险（必须讲清）

MES 追溯的"正确答案"**会随后续数据变化**：工程师今天问"批次 B-77 焊接不良根因"，Agent 基于当时已有的过点 / 不良记录给出假设 A；明天该批次追加 50 条不良记录、锡膏供应商补登批次信息，正确根因可能变成 B。命中昨天的 A 就给了过时结论——而根因直接影响返工 / 隔离决策，过时结论可能误导处置。**尤其危险的是 `version_leak`**：请求带了 `version` 就被整体判成"可缓存"，把"SN-001 按 v3 路线过了哪几站"这类现场查询返回旧过点轨迹（网关 §5）。

#### 8.2.3 受限使用策略

| 场景 | `data_scope` | 答案缓存 | 理由 |
|------|-------------|---------|------|
| "RR-100 工艺路线 v3 的焊接站参数是多少"（历史回放） | `AS_OF` + `IMMUTABLE` | ✅ 可 | 工艺版本不可变，答案稳定 |
| "8D 报告标准模板长什么样" | `AS_OF` + 稳定文档 | ✅ 可 | 模板稳定 |
| "批次 B-77 焊接不良根因" | `CURRENT` + `LIVE_ONLY` | ❌ 禁 | 追溯答案随数据变化 |
| "SN-001 的过点轨迹"（即使带 `version`） | `CURRENT` + `LIVE_ONLY` | ❌ 禁 | 过点记录持续追加 |

- **默认**：`CURRENT` 一律跳过，**初期语义缓存 / 答案缓存默认只对 `AS_OF` 开放**；先只开工具结果缓存（§8.1），等可观测数据证明某些稳定问题高频重复再灰度。
- **指标口径**：`gateway_cache_hit_total` 按 `data_scope` 打 label，另有 `gateway_cache_skip_total`（`CURRENT` 跳过次数，📐 设计目标）；**`CURRENT` 请求不进命中率分母**——否则"按策略跳过"被统计成"缓存没命中"，指标反过来逼人把时变性红线调回去（网关 §11.1）。
- 这条体现 MES 领域判断力：通用降本教程会推荐语义缓存，但 MES 的追溯时变性让它危险；讲清"不是全关，而是按 `data_scope` 闸门策略化开关"，比堆技术得分高。

---

## 9. 输出层：结构化输出 + max_tokens

输出 token 通常占总成本的小头（输入是大头），但这层改动成本最低，顺手做。**当前设计的口径修正：结构化输出不是 `with_structured_output`，是 Content Block（Pydantic BaseModel）+ `Msg`**（底座 §2.2）。

### 9.1 结构化输出约束（已有，强化）

- `DiagnosisReport` / `Draft` 走 Pydantic 校验（诊断 §2 收口条件、草稿 §1.2 `generate`），模型不写自由文本铺垫，直接出字段——输出 token 更少。
- Enum 约束（如 `FiveM1ECategory`、`GatewayIntent`）让类别字段只出枚举值，不啰嗦解释；网关兜底分类用 Content Block 强制 Enum 防编造意图。
- `evidence` 字段只存 `trace_id` 列表（短），不存证据全文（长）——全文在 `tool_call_trace.output_payload`。
- **隐藏收益**：schema 稳 → 少重试。`agent_llm_schema_error_total` 的 SLO <0.5%、5min >2% 报 P2（可观测性 §5.4）；一次 schema 失败重试就是一次"系统提示+历史"的完整重发，比多写几百输出 token 贵得多。

### 9.2 max_tokens 上限

- 每次 `Model` 调用设 `max_tokens` 上限（provider 参数，在 `Model` 抽象处配置，非框架开关名），防模型发散长篇。
- **收口步的报告生成不能卡太死**——被 `finish_reason=length` 截断会丢假设。按步类型分档：tool selection 步小、报告生成步大。🔴 档位待实测。
- 观测：`finish_reason=length` 频繁出现 → 告警（可观测性 §8.1），既是质量信号也是"max_tokens 设太小"的信号。

---

## 10. 观测驱动的降本闭环

降本不是一次性动作，是 **测 -> 归因 -> 降 -> 再测** 的持续闭环。可观测性是闭环的地基。

### 10.1 成本归因（找烧钱大户）

在可观测性 §5.2 的 `agent_token_total{model,direction,capability}` / `agent_cost_usd_total{model,capability}` / `agent_llm_invocation_total` 基础上归因：

| 归因维度 | 找什么 | 优化动作 |
|---------|-------|---------|
| `capability`（diagnosis / drafting / orchestration） | 哪个能力最烧钱 | 针对性优化该能力的提示词 / 工具集 |
| `scenario`（换线 / 客诉 / 首件） | 哪个场景步数最多 | 检查该场景能否更多走代码节点 |
| `step_no` / `prompt_version` | 哪几步最贵、是不是改完提示词变长 | 冗余探索（§7.3）/ 精简提示词（§6.2） |
| `model` | 大模型调用占比 | 评估能否降级（§5，过评测门禁） |
| `bounded_context` / `tool` | 哪个工具结果最膨胀 | 强化 §7.1 字段裁剪 |
| `tenant` | 哪条产线用得多 | 给管理层看 + 限流（§10.4） |
| `data_scope` / `data_class`（新增列，见 §11.1） | 缓存命中是否来自合规来源 | 用 §8.1 闸门与 `AnswerValidator` 兜底 |

⚠️ **命名纪律**（可观测性 §5.1）：label 维度固定 `capability` / `tool` / `status` / `model` / `bounded_context`，**新增维度需评审**——维度爆炸会撑爆 prometheus。旧版表里的 `phase` 不是现役 label，若要做阶段归因，先在可观测性事实源里评审通过再加；`scenario` 在编排的 `agent_session_total{scenario,status}` 里已有。

### 10.2 成本漂移检测（已有，强化解读）

token/会话 P95 超基线 1.5× 持续 30min → P3 成本漂移告警（可观测性 §5.4 / §11.3）。降本视角的排查清单：

- 提示词变长了？（`prompt_version` 变更未配套精简）
- 模型啰嗦了？（completion 占比升 → 加强结构化 / 分档 `max_tokens`）
- 反复重试？（`agent_llm_schema_error_total` 升 → schema 不稳）
- 工具结果变大了？（下游 DTO 字段膨胀 → 强化 §7.1 裁剪）
- 步数失控？（`agent_step_budget_hit_total` 升 → 早停 / 预算）
- 缓存命中率掉了？（缓存块被动态工具集破坏 → §6.1.3，或 `CURRENT` 占比升高 → 这是**正确**的，别误判成退化）

### 10.3 评测门禁（降本的质量护栏）

任何降本动作（换模型 / 改提示词 / 加截断 / 改缓存策略）**必须过回归**：

```
降本变更 -> 自建 EvalRunner 跑评测集（三层判定 + pass^8 + pytest CI）
         -> 对比新旧：hard gate 安全断言全过 + pass^k/ECE 不退 + token 与每成功任务成本下降
  ├─ 通过 -> shadow（100% 复制真实任务但不产生副作用）-> canary -> 放量
  └─ 不通过 -> 不上线（质量红线 > 成本）
```

- 评测指标（可观测性 §10.4 / ADR-10）：根因准确率、证据召回、草稿采纳率、**ECE <0.1**、**工具调用冗余（越接近 1 越好）**、**pass^k（主轴，k=8 口径）**、**每成功任务成本（成本主轴）**。
- 评测栈自建：自研 `EvalRunner` + 三层判定（程序化断言 / 指标阈值 / LLM-as-judge，judge 用独立包 `py-openjudge`）+ pytest CI 门禁；框架只提供 `TracingMiddleware` 的 trace 事件作过程数据（**`agentscope.evaluate` 在 2.0 已删除未回归**）。
- **成本-质量权衡曲线**：离线跑不同模型 / 不同截断强度，画"pass^k / 准确率 vs 每成功任务成本"曲线，找帕累托最优点。🔴 曲线待评测数据积累后出。
- 安全 **hard gate** 零容忍：版本锚点、写越界、租户越权、过点侵入、PII、实体幻觉、证据空、`stale_fact_rate = 0`。

### 10.4 限流（防失控烧钱）

- **每会话**：分能力预算 20 / 8 / 40 步、检索 `max_tool_calls=2`（§7.3.1）——限步数 = 限单会话 token 上限。
- **每租户**：Redis 信号量限并发会话数（ADR-08 四道舱壁之一；整体选型 §2.5）；单工具超时 ≤2s。
- **每日预算**：🔴 可选，按租户设日 token 预算，超限降级（如关闭主动巡检触发、网关强制走检索分派）。这是"硬省钱"开关，防异常情况烧爆；**不得用它压正确性**（不能把 `LIVE_ONLY` 强制改成用缓存）。
- 不引入 Celery，也不做 agent 侧挂起 / 续跑（整体选型 §5 决策 11）。

---

## 11. 数据模型与指标扩展（本篇提议，须同步回事实源）

> ⚠️ 本节全部是**本篇提议的设计扩展**，不是 [可观测性 §8.1 / §5.2](Agent可观测性-设计与实现方案.md) 的现役字段与指标。落地时必须先在可观测性事实源里登记，否则会出现"两处简版各说各话"。

### 11.1 表字段扩展

```sql
-- llm_call_log 现役字段（可观测性 §8.1 / §13.1）：
--   call_id / session_id / step_no / model / prompt_version
--   prompt_token_count / completion_token_count / latency_ms / finish_reason
--   tool_calls_produced / occurred_at
--   prompt 全文不落库，只落摘要 + prompt_version
llm_call_log
  + cache_read_token_count   INT NULL   -- provider 扩展字段：🔴 是否回传待核实，拿不到留 NULL，禁止推算
  + cache_write_token_count  INT NULL   -- 同上
  + compacted_input_tokens   INT NULL   -- OffloadCompactPolicy 压缩后的输入 token（对比未压缩）
  + capability               VARCHAR    -- diagnosis / drafting / orchestration / retrieval
  + phase                    VARCHAR    -- 推理阶段；⚠️ 新增维度需评审（可观测性 §5.1），默认不启用

-- tool_call_trace（记忆系统 §3.10.6 已提出的设计扩展）
  + data_scope               VARCHAR    -- AS_OF / CURRENT（只由 as_of 派生）
  + data_class               VARCHAR    -- LIVE_ONLY / IMMUTABLE / AS_OF_ONLY / AUTHORITATIVE_RECORD
  + source                   VARCHAR    -- LIVE / TOOL_CACHE / AS_OF_EVIDENCE / RECORD
  + turn_id                  VARCHAR    -- 供 AnswerValidator 校验证据新鲜度
```

### 11.2 新增指标（须过可观测性 §5.1 命名规范评审）

| 指标 | 类型 | label | 含义 |
|------|------|-------|------|
| `agent_cache_read_token_total` | Counter | model, capability | prompt cache 命中读取 token（provider 回传才有值） |
| `agent_cache_write_token_total` | Counter | model, capability | prompt cache 写入 token |
| `agent_cache_hit_ratio` | Gauge | model, capability | 缓存命中率（read / (read+write+miss)） |
| `agent_tool_cache_hit_total` | Counter | tool | 工具结果缓存命中次数 |
| `agent_tool_result_saved_token_total` | Counter | tool | 压缩前后 token 差（用 Counter 避免违反 Histogram 后缀规范） |
| `agent_parallel_tool_call_total` | Counter | capability | 单步并行 tool call 数（量化 §7.2） |
| `agent_early_stop_total` | Counter | capability, status | 早停触发次数 |
| `agent_cost_saved_usd_total` | Counter | model, capability | 按 lever 归因的**估算**节省；新增 `lever`/`phase` label 需评审 |
| `gateway_cache_hit_total` | Counter | data_scope | **现役设计指标**（网关 §11.1），答案缓存命中 |
| `gateway_cache_skip_total` | Counter | data_scope | **现役设计指标**，`CURRENT` 按策略跳过次数 |

> 按 lever 归因的估算法：缓存命中省的 ≈ `cache_read_tokens × (in_price - cache_read_price)`；模型路由省的 ≈ `(大模型价 - 小模型价) × tokens`。单价表 `model_pricing` 配置化（🔴 单价来源需定期同步），缓存定价 1.25× / 2× / ~0.1× 属 provider 公开定价但**须按实际账单校准**。这让"哪条杠杆省了多少"可量化，而不是笼统的"降本了"。

### 11.3 SLI / SLO 补充

| SLI | SLO 目标 | 说明 |
|-----|---------|------|
| prompt cache 命中率 | 【目标示例】≥80%（多步会话），🔴 待实测标定 | 低于此说明缓存块被动态工具集破坏或会话过短；provider 不回传 token 时用输入 token 下降间接验证；【目标示例】禁止当行业基准（§11.4） |
| token/会话 P95 | 基线 ±20%，超 1.5× 持续 30min 告警 | 已有 P3 漂移告警（可观测性 §5.4） |
| 工具调用冗余比 | 越接近 1 越好，阈值 🔴 待定 | 已有评测指标（可观测性 §10.4） |
| 降本变更后 pass^k / ECE | 不退 | hard/soft gate 硬约束 |
| 每成功任务成本 | 不升 | ADR-10 成本主轴，防止"token 降了但重跑多了" |
| `stale_fact_rate` | = 0 | 缓存策略的 hard gate（记忆系统 §3.10.6） |

### 11.4 按规模分档的成本测算口径（🔴 待实测，不是业务效果数字）

> **本节不是字段 / 指标扩展**，而是把"按规模"落成 **规模假设 -> 可测指标 -> 待回填占位** 的测算骨架，与 §11.2 的成本归因公式同处一节便于对照。⚠️ 本节**不产出任何"已达成"的业务效果数字**：三项能力、观测底座与全部降本组件均未落地，没有实测数据可支撑"降本 X% / 提升 Y%"。所有示例数字按 [ADR §0](../架构设计v2/00-架构决策记录ADR.md) 标 **【目标示例】**，禁止当行业基准引用；设计容量目标与线上实绩必须分开讲（[面试指南](../面试指南/项目亮点与指标卡片.md) 行 142）。口径与文首「绝不编业务效果数字」一致。

#### 11.4.1 规模分档（锚定已有事实源，不新造规模）

| 档 | 规模口径 | 出处 |
|----|---------|------|
| 试点 | 1 条产线换线试点；入口是"人在问"低 QPS | [整体选型 §6](../整体技术选型与模块划分.md)、[网关 §1.2](Agent入口网关-意图路由.md) |
| 全量 | 6 条产线 / 100+ 物料大类 / 600+ 型号，采集约 500 QPS | [重连补传洪峰处理方案 §0](../实现说明/高频数据/重连补传洪峰处理方案.md)（该文明确"设计容量目标 + 假设，不是线上实绩"） |
| 展望 | 10 条线、采集设计容量约 1 万报文/秒 | [项目亮点与指标卡片行 142](../面试指南/项目亮点与指标卡片.md)；⚠️ 这是**面试话术中的规模假设，非源文档数据、非线上实绩**，诚实口径见 [设备管理服务-面试闪光点行 98](../面试指南/设备管理服务-面试闪光点.md) |

> ⚠️ **关键区分**：500 QPS / 1 万报文秒是**设备采集链路**的容量目标，**不是 Agent 会话量**。Agent 侧是"人在问"低 QPS + 事件异步触发，拿采集 QPS 推会话量就是拿别的链路数据冒充实绩——与 [记忆系统 §8.5](Agent记忆系统-设计与实现方案.md)「不得拿红线数据代替实测」是同一条纪律。

#### 11.4.2 规模输入参数（待回填的输入，不是效果）

| 参数 | 试点 | 全量 | 展望 | 口径 |
|------|------|------|------|------|
| 日 Agent 会话量 | 🔴 待实测 | 🔴 待实测 | 🔴 待实测 | 网关分诊 + 事件触发，不能用采集 QPS 推 |
| 会话类型占比（诊断 / 草稿 / 检索 / 编排） | 🔴 | 🔴 | 🔴 | 按 `capability` 归因（可观测性 §5.2） |
| 平均步数 N | 🔴 | 🔴 | 🔴 | 诊断上限 20 步 / ≤10 次工具调用（§7.3.1） |
| 每步输入 / 输出 token | 🔴 | 🔴 | 🔴 | `Msg.usage` 的 `input_tokens` / `output_tokens`（§6.1.2） |
| 模型单价 | 🔴 | 🔴 | 🔴 | `model_pricing` 配置化、需定期同步（可观测性 §8.2） |
| prompt cache 命中率 | 🔴 | 🔴 | 🔴 | provider 不回传缓存 token 时留空，不推算 |

#### 11.4.3 测算公式（输出留空，待埋点后回填）

```text
日 token   = Σ_capability (日会话量 × 平均步数 × 每步输入 token + 平均输出 token)
日成本     = Σ_model (token × model_pricing 单价)
按 lever 省的 ≈ cache_read_tokens × (in_price - cache_read_price)
              + (大模型价 - 小模型价) × tokens
每成功任务成本 = 总花费 / 通过 pass^8 的 case 数          # ADR-10，成本主轴
```

回填时机：§12 阶段一的 `llm_call_log` / 指标埋点上线并积累观测窗口后，按 `capability` / `model` / `tenant` 回填上表；**回填前不得在任何汇报材料里写成本数字**。验收同时看 token 下降与 pass^8 / ECE / 每成功任务成本不退（§10.3）。

#### 11.4.4 效果侧只给指标定义，不给达成值

| 效果口径 | 定义出处 | 目标写法 |
|---------|---------|---------|
| token/会话 P95 | 可观测性 §5.4 | 不超基线；超 1.5× 持续 30min 报 P3 |
| prompt cache 命中率 | 本篇 §11.3 | 【目标示例】≥80%，🔴 待实测标定，禁止当行业基准 |
| 工具调用冗余比 | 可观测性 §10.4 | 越接近 1 越好，阈值 🔴 待定 |
| 每成功任务成本 | ADR-10 | **不升**（不是"降 X%"） |
| 换线 PASS 时 LLM 调用 | 本篇 §4.1 | 设计目标 0，📐 指标尚未埋点 |
| `stale_fact_rate` | 记忆系统 §3.10.6 | = 0，hard gate |

#### 11.4.5 汇报话术（设计容量目标，不是线上实绩）

> "按 6 条产线的规模假设，Agent 侧是低 QPS 问答加事件异步触发；日会话量、平均步数、token/会话都还没实测，所以成本数字现在填不出来。方案里给的是可观测口径和按 lever 归因的公式，埋点跑一周后再回填。这是设计阶段的容量规划，不是线上实绩。"

---

## 12. 实现步骤（分阶段；先正确性、后降本有效性）

> 顺序遵循 [记忆系统 §10](Agent记忆系统-设计与实现方案.md)「先用最小端到端链路验证流程正确性，再启用降本有效性」与 [底座 §7](../基于AgentScope2.0的架构设计.md) 的迁移视角。**不得用缓存命中率压正确性**（记忆系统 §3.10.6）。

### 阶段零：前置（不属于降本动作，但没它后面全空）

1. 底座切换：引入 AgentScope 2.0（pin 具体小版本）、FastAPI Agent Service（`create_app`）+ `SessionService` + `TracingMiddleware` 跑通空壳；**版本 pin 与 API 漂移检查列入 CI**。
2. 单 Agent 基线先建并测出准确率（ADR-10 决策 3），否则分不清"架构更优"与"多烧算力"。
3. 能力按 [引入路线 §5](AGENT服务引入路线.md) 顺序交付：诊断 → 草稿 → 编排（先纯代码骨架）→ 编排接入能力节点 → 主动触发；**端点契约不变**。

### 阶段一：度量打底 + 架构层（先测后降）

4. 落地可观测性 §8.1 的 `llm_call_log` 与 §5.2 指标；本篇 §11 的扩展字段先登记再接线，**先量出钱花在哪**（不量清楚不动手降）。
5. 埋 `agent_step_total{step_kind=CODE}` 与 `agent_llm_invocation_total`，验证换线 PASS 时 LLM=0 的**设计目标**（当前指标尚未埋点，别说"已观测"）。
6. 网关规则优先 + 确定性分派落地（网关 §4 / §5）：规则命中 0 次 LLM、`RETRIEVAL_REQUEST` 走 `agent-service` 单层检索 flow；按四类变更走 shadow→canary。
7. 正确性三件套先于缓存：`resolve_data_scope`（0 侵入）→ `ToolExecHook` 取数闸门 → 答案 / 工具结果缓存策略化（记忆系统 §3.10.6 落地顺序 ①~③）。

### 阶段二：提示词层（最大稳定收益）

8. `CacheControl` + 缓存标记 Middleware 与 `ModelObsMiddleware` 同链接入（§6.1），先在诊断会话试点，量输入 token 下降；provider 不回传缓存 token 就只记输入 token。
9. 工具描述精简（§6.2.1）：逐个 review `ToolDescriptor.description`，删冗余、去与 `args_schema` 重复的参数说明。
10. 按需评估阶段裁剪（§6.2.3）：仅在 token 实测显示工具定义占比过高时启用，**默认关闭**。

### 阶段三：循环层 + 缓存层

11. `ToolExecHook` / `OffloadCompactPolicy` 接线 `Offloader` + `ToolOffloadMiddleware`（§7.1）：先模型看全文，接线后走压缩 / 卸载；`tool_call_trace.output_payload` 始终落 FULL view。
12. `history_rehydration_policy` + `AnswerValidator` 出口校验（§7.1.4），评测集补 `stale_hit` / `redundant_live` / `version_leak` 三类反例。
13. system prompt 鼓励并行 tool call（§7.2），量 `agent_llm_invocation_total` 下降；`EarlyStopDetector` 接在诊断 Agent 收敛判定处，触发转人工不硬答（§7.3）。
14. `ToolResultCache` 按 `data_class` 策略化（§8.1）：先缓存 `get_process_route`（`IMMUTABLE`，最安全），再灰度台账类。

### 阶段四：模型层 + 闭环

15. Model 分层路由 + `EvalGate`（§5.2）：选型表 + 启动校验，先对网关兜底分类降级小模型（风险最低，且规则命中时本来就是 0 次 LLM）。
16. 全部降本变更过 `EvalRunner` 回归（§10.3）：hard gate 安全断言 + pass^8 / ECE / 每成功任务成本；画成本-质量曲线。
17. 答案缓存默认只对 `AS_OF` 开放（§8.2），`CURRENT` 跳过并单独记 `gateway_cache_skip_total`；embedding 语义路由保持 📐 演进方向，不进本期。
18. 每日预算限流（§10.4，🔴 可选），按租户硬省钱开关；不得用它压取数正确性。

---

## 13. 约束落地检查清单

- [ ] 降本不碰只读 / 不旁路写 / 不进过点 / 版本一致性 / 证据不可空 / 时变性 / 质量不退 / 编排权唯一任一红线。
- [ ] 全篇按 📐 设计目标、尚未落地表述；`app/...` 路径标为设计目标路径；不写"已降本 X%"、不写"已观测"。
- [ ] 只有诊断按多步 ReAct 建模；草稿固定链、编排 `flows/`、检索单层 flow 各自单独归因。
- [ ] 编排口径按 [编排能力 §0](FactoryAgent/FactoryAgent-流程编排能力.md)：不复述"4 类 sub-agent / A/B/C/D"；LLM=0 只作设计目标；指标写 `{step_kind=CODE}`，`node_type` 只作表名。
- [ ] 网关分流写"规则优先 + 确定性分派 `POST /agent/retrieval`"，不写"RAG 直答 / 第二层 ReAct"；embedding 语义路由标 📐 未拍板。
- [ ] 结构化输出写 **Content Block + `Msg`**；不写 `with_structured_output` / `ObservableChatModel` / `ResultCompactor` 当现役组件。
- [ ] 缓存标记与观测埋点同链；`CacheControl` 为本项目 dataclass；`Msg.usage` 现役字段只有 `input_tokens` / `output_tokens`，缓存 token 拿不到留空不推算。
- [ ] `OffloadCompactPolicy.compact()` 与记忆系统 §4.2 对齐；`tool_call_trace.output_payload` 落 FULL view；截断标 `_truncated` / `_omitted_count`。
- [ ] 预算按能力 20 / 8 / 40 + 检索 `max_tool_calls=2`；早停触发转人工，不直接出报告硬答。
- [ ] 缓存 key 含 `version_anchor`；`LIVE_ONLY` + `CURRENT`/跨 turn 强制实时；仅 `AS_OF` 允许答案缓存；`CURRENT` 不进命中率分母。
- [ ] `stale_fact_rate = 0` 作 `EvalGate` hard gate；模型 / prompt / 工具路由 / 审批策略四类变更走 shadow→canary→放量。
- [ ] 模型降级每条过 `EvalGate`；验收同时看 token 下降与 pass^8 / ECE / 每成功任务成本不退。
- [ ] §11 所有新增字段 / 指标标"本篇提议的设计扩展"，落地前同步回可观测性事实源并过命名规范评审。
- [ ] 量化收益标 🔴 待实测，不编业务效果数字。
- [ ] §11.4 规模分档表只作「规模假设 -> 可测指标 -> 待回填占位」；示例数字标【目标示例】、不与行业基准 / 线上实绩混讲；不拿采集链路 QPS 推 Agent 会话量。

---

## 14. 面试防守 Q&A

**Q：Agent 这么烧 token，你怎么管成本？**
A：分清"测得清"和"降得下"。测得清靠可观测性那篇——`agent_token_total` / `agent_cost_usd_total`、按 capability / scenario 归因、token/会话 P95 超基线 1.5× 持续 30min 告警。降得下是本篇，优先级"少调 > 少发 > 调便宜的"。最大杠杆是架构层：编排能力用 `flows/` 代码节点做确定性判定，换线全程 PASS 时能力节点不触发，LLM 调用设计目标为 0；入口网关规则优先 + 确定性分派，规则命中 0 次 LLM，且按 ADR-13 同一请求只允许一层 ReAct，避免双编排把 token 翻倍。其次是 prompt caching 把诊断 ReAct 每步重发的 system prompt + 工具定义降到约 1/10，再是 `Offloader` 压缩工具结果、并行 tool call 减往返、按 `data_class` 闸门的版本化缓存。换便宜模型是最后一步，且必须过 `EvalGate` + shadow→canary。另外成本主轴不只看 token，还看 ADR-10 的每成功任务成本——省了 token 却让重跑变多，不算降本。

**Q：为什么不直接换便宜模型？**
A：模型单价只是成本结构的一个变量。诊断 ReAct 的钱主要花在"每步重发系统提示+工具定义"和"滚雪球的历史上下文"上，换便宜模型只打折不归零；代码节点不调 LLM 是直接归零，prompt caching 把重复块降到约 1/10。而且换模型有质量风险——根因准确率一退，误导返工 / 隔离决策的代价远超省的 token。所以顺序是先砍调用次数、再砍每次发的量、最后才动模型规格；且 `ModelRouter` 启动时逐条查 `EvalGate.passed`，没过评测的降级拒绝启动。

**Q：三项能力的成本结构一样吗？**
A：不一样，这是当前设计里最容易讲错的地方。只有**诊断能力**是开放式 ReAct 多步循环，才有"每步重发 + 历史二次放大"的问题；**草稿能力**是 `resolve_evidence → retrieve_knowledge → generate → validate_archive` 固定 4 步链，只有 `generate` 一步调 LLM，成本是单次大输入 + 单次结构化输出，杠杆是裁剪检索文档而不是省步数；**编排能力**是 `flows/` 确定性代码节点 + 非确定分支才 `await` 诊断 / 草稿，成本等于被委托能力节点之和，PASS 路径为 0；网关分派的检索是 `agent-service` 单层 flow、≤3 次工具调用、网关不传消息历史，也没有跨轮滚雪球。把四项都套成 ReAct 公式，归因就是错的。

**Q：prompt caching 在 MES 场景有什么坑？**
A：三个。一是缓存块必须真稳定——system prompt、工具定义、few-shot 可以缓存，用户问题和工具结果这些动态上下文绝不能进缓存块；二是工具集按阶段动态裁剪会破坏命中，所以记忆系统 §8.7 的结论是"固定工具集 + prompt 缓存通常优于动态工具集 + 缓存未命中"，只有工具集极大时才考虑阶段裁剪，且默认关闭；三是**别把 provider 扩展字段当框架字段**——`Msg.usage` 当前只有 `input_tokens` / `output_tokens`，`cache_read_tokens` / `cache_creation_tokens` 是本篇提议的扩展，provider 不回传就记空值、用输入 token 下降间接验证，绝不推算冒充实测。另外 `prompt_version` 变了缓存按内容 hash 自然失效，不用手动清。

**Q：工具结果那么大，怎么不撑爆上下文？**
A：靠"模型看摘要，trace 落全文"双路。超大结果卸载与回灌前替换由框架 `Offloader` + `ToolOffloadMiddleware` 承载，裁哪些字段、截多长是本项目的 `OffloadCompactPolicy`（字段白名单 + `LIST_TRUNCATE = 5` + `_truncated` / `_omitted_count` 标注）；`ToolExecHook` 是唯一取数通道，`tool_call_trace.output_payload` 永远存 FULL view，工程师 UI 读 trace 不读压缩版。落地还分阶段：先用确定性假模型把全文直接喂模型验证流程正确性，接线 `Offloader` 后才启用压缩。另外多轮回灌走 `history_rehydration_policy`，把历史 `tool_result` 降级为引用块，保留叙事但取消"可作事实断言"资格，出口由 `AnswerValidator` 兜底。

**Q：语义缓存为什么在 MES 要谨慎？现在到底开没开？**
A：因为 MES 追溯的"正确答案"会随后续数据变化——今天基于已有过点 / 不良记录给假设 A，明天追加 50 条不良记录正确根因可能变成 B，命中昨天的 A 会误导返工 / 隔离决策。当前设计不是笼统"默认关闭"，而是**按 `data_scope` 闸门策略化**：`data_scope` 只由 `as_of` 派生，`AS_OF` 才允许答案缓存，`CURRENT` 一律跳过，**即使请求带 `version`**——专门防 `version_leak`（带版本就被误判成可缓存、返回旧过点轨迹）。工具结果侧还有 `DataScopePolicy` 四类 `data_class`，默认 `LIVE_ONLY`，`CURRENT` 或跨 turn 强制实时、缓存命中也作废。评测侧补 `stale_hit` / `redundant_live` / `version_leak` 三类反例，`stale_fact_rate = 0` 作 hard gate。`CURRENT` 跳过还要单独记 `gateway_cache_skip_total`、不进命中率分母，否则指标会反过来逼人把红线调回去。embedding 语义路由还只是 📐 演进方向、未拍板，本期不进。

**Q：降本会不会牺牲质量？怎么保证？**
A：靠评测门禁兜底。评测栈是**自建**的（自研 `EvalRunner` + 三层判定 + pass^k + pytest CI 门禁，judge 用独立包 `py-openjudge`；框架 2.0 已删除 `agentscope.evaluate`）。安全断言（版本锚点 / 写越界 / 越权 / 证据空 / `stale_fact_rate`）是 hard gate 零容忍，质量指标（根因准确率、ECE <0.1、工具调用冗余、pass^8、每成功任务成本）是 soft gate；模型 / prompt / 工具路由 / 审批策略四类变更一律 shadow → canary → 放量。`ModelRouter` 启动时逐条查 `EvalGate.passed`，没过评测的降级直接抛异常拒绝启动。这不是口头保证"不会降质量"，是 CI 硬拦着。

**Q：怎么知道哪条降本杠杆真的省了钱？**
A：靠按 lever 归因的成本指标。`llm_call_log` 记 `capability` / `step_no` / `prompt_version`，本篇再提议补缓存 token、压缩后 token 等扩展字段（须先同步回可观测性事实源）；`agent_cost_saved_usd_total` 按 lever 估算——缓存命中省的 ≈ `cache_read_tokens × (in_price - cache_read_price)`，模型路由省的 ≈ `(大模型价 - 小模型价) × tokens`。再配合成本漂移检测、成本-质量权衡曲线，形成"测 -> 归因 -> 降 -> 再测"闭环。所有新增 label 维度要过可观测性 §5.1 评审，否则维度爆炸撑爆 prometheus。

**Q：编排能力换线说"全程 PASS 时 LLM 调用为 0"，这算降本吗？**
A：算，而且是最大的降本，但要讲清**这是设计目标、指标尚未埋点、编排能力完全未落地**。当前设计里编排不是"4 类 sub-agent"——[编排能力 §0](FactoryAgent/FactoryAgent-流程编排能力.md) 明确推翻了那两套旧设计，现在只有"编排代码层 + 在非确定决策点调诊断 / 草稿能力"。三问（输入是否开放 / 是否需推理生成 / 分支是否难穷举）皆否走代码节点；换线 5 步里编排、结构化比对、barrier、gate、写落库全是代码，agent 只在 mismatch 等异常分支触发。可观测上用 `agent_llm_invocation_total` 与 `agent_step_total{step_kind=CODE}` 占比验证（`node_type` 是 `node_trace` 表列名，别混）。反向也要守：不能为了降本把本该 agent 做的开放推理硬塞决策树，那是牺牲根因覆盖率换省钱，不值。

---

## 15. 一句话定位

"Agent token 成本降本的优先级是**少调 > 少发 > 调便宜的**：只有诊断能力是开放式 ReAct 多步循环，草稿是固定链、编排是 `flows/` 代码节点、检索是 `agent-service` 单层 flow，四者成本结构分开归因；架构层用代码节点把能不调 LLM 的步骤归零（换线 PASS 时 LLM=0 是设计目标）、入口网关规则优先 + 确定性分派让规则命中 0 次 LLM，并按 ADR-13 保持一层 ReAct；提示词层用 prompt caching 把每步重发的系统提示+工具定义降到约 1/10，固定工具集优先于动态裁剪；循环层靠 `Offloader` + `ToolOffloadMiddleware` 压缩工具结果（trace 仍落 FULL view 保证据链）、并行 tool call 减往返、分能力预算与早停转人工；缓存层按 `version_anchor` 版本化，并由 `DataScopePolicy` 的 `data_class` 闸门裁决，`CURRENT` 一律跳过答案缓存、仅 `AS_OF` 可命中；模型层做分层路由但每条降级过 `EvalGate` 与 shadow→canary；最后用按 lever 归因的成本指标闭环'测 -> 归因 -> 降 -> 再测'，验收同时看 token 下降与 pass^8 / ECE / 每成功任务成本不退——全程不碰只读 / 不进过点 / 版本一致性 / 证据不可空 / 时变性 / 质量不退 / 编排权唯一任一红线，省钱不能绕过质量门禁。"
