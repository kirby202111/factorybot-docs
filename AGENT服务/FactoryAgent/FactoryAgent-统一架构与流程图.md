# FactoryAgent —— 统一架构与流程图

> 本文集中呈现 FactoryAgent 的 **AgentScope 2.0 架构形态**：一个运行时承载**三种能力形态**（开放式 ReAct / 顺序 `await` 固定链 / `flows/` 代码编排），以及各自的流程图与关键约束。
> **核心口径**：诊断、草稿编写、流程编排不是三个 Agent，而是**同一个运行时装配出的三种能力形态**；它们共用 `Toolkit`、`Permission` 策略、`AgentState` + 框架 `Storage`（本项目选 `AsyncSQLAlchemyStorage` 落 MySQL；经 `SessionService` 访问，旧名 `SessionManager` 已失效不可导入）、`TracingMiddleware` 注入点。
> 配套说明见 [总体设计与实现方案](FactoryAgent-总体设计与实现方案.md) §3/§4、[诊断能力](FactoryAgent-诊断能力.md) §2、[草稿编写能力](FactoryAgent-草稿编写能力.md) §1.2、[流程编排能力](FactoryAgent-流程编排能力.md) §2。

---

## 1. 为什么全套用 AgentScope 2.0

| 需要的能力 | 落点（AgentScope 2.0 已核实原语 + 应用层） | 对本系统的意义 |
|-----------|----------------------|---------------|
| 开放式取证循环 | 统一 `Agent` 类（内置 ReAct，`reply` / `reply_stream`） | 诊断的多步只读取证循环由 Agent 内生，可对每次工具执行加权限拦截、超时、trace |
| 固定步骤链 | 应用层顺序 `await` + `AgentState` / `SessionService` | 草稿链"取证据 → 检索 → 生成 → 校验"步骤固定，与诊断共用底座；`AgentState` 只承载**会话内上下文与多轮记忆** |
| 代码/LLM 混排 | `flows/` 里的确定性 flow 步骤 + `Agent` 能力节点 | 编排里确定性步骤不消耗 LLM 调用，能力节点才调 LLM |
| 并行 + 汇合 | `asyncio.gather` 并发**各自独立**的 `Agent.reply()` + 应用层显式合并 + 确定性 barrier | 钢网程序比对 ‖ 齐套比对并行，放行前 barrier 硬校验；分支间不共享消息流、不看对方中间结论 |
| 人在回路 | **主闸**：工具网关的一次性动作令牌（绑定 `action:target` + 过期 fail closed）；**叠加层**：`Permission`；跨调用的审批确认落 **MES 工单/待办实体** | confirmation gate 不阻塞也不挂起：本段产出动作卡与待办就返回，审批完成回调 `/advance` **重新发起下一段并重查当下事实**（MES 数据时变，不恢复快照） |
| 硬上限 | 步数与工具调用预算上限 | 直接对应"最大步数"红线，运行时级兜底而非口头约束 |
| 能力组合 | 能力可被 flow 直接 `await` 调用（`invoke` 是我们自己的应用层语义） | 编排直接调用诊断 / 草稿，不需要另起服务或复制逻辑 |

**⚠️ 本节早先写的 `Pipeline`（`sequential` / `fanout` / `MsgHub`）、`Handoffs` / `Routing`、独立 Plan 模块、`SessionManager`、`AgentScope Studio` 经源码树核对后全部撤回**：前三者是 AgentScope **1.x** API，2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 模块已不存在；`Handoffs` / `Routing` / 独立 Plan 模块从未提供（任务规划是 `TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` 四个工具 + `Task` / `TaskContext` + `AgentState`）；`SessionManager` 在 2.0.5 改名 `SessionService`；`agentscope-studio` 已归档。`GoalPipeline` / `TeamPipeline` 虽存在但官方标注 experimental，且 `TeamPipeline` 明确"成员间不互相说话"——观察，不进生产。协同形状因此由普通 Python 代码承担（见 ADR-01 / ADR-05）。

**不选**：AutoGen（conversational 模式适合多 agent 对话，不适合严格步骤编排 + 确定性 flow 步骤）；CrewAI（抽象偏高，难做细粒度权限拦截与 gate 状态机）；裸模型循环调用（黑盒循环，难做细粒度拦截）。LangGraph 不再作为底座，本文也不沿用其 `StateGraph` / 子图 / checkpoint 术语。

---

## 2. 三种能力形态对比

| 维度 | ① 开放式 ReAct（诊断） | ② 固定链（草稿） | ③ `flows/` 代码编排 |
|------|----------------------|-----------------|---------------------|
| 运行时结构 | `model ↔ tool` 循环 | 直线 4 个 flow 步骤（依次 `await`） | 确定性 flow 步骤 + 能力节点 + barrier + gate |
| 是否开放规划 | 是（取证路径由中间结果驱动） | 否（步骤固定） | 否（顺序显式硬编码） |
| LLM 消耗 | 每步一次模型调用 | 一次结构化生成 | 仅能力节点；纯代码路径为 0 |
| 步数 / 工具调用预算 | 20（≤10 次工具调用） | 8 | 40 |
| 单次调用超时 | 60s | 90s | 300s（**不含等人确认**，等待期在工单侧计时） |
| 写权限 | 无 | 无 | 有（`requires_confirmation` + `writes_via`） |
| 典型分段终点 | 无（一次性出报告） | 无（返回草稿等前端确认） | gate 步骤：产待办后本段结束（**不挂起**） |
| 并行 | 不涉及 | 不涉及 | `asyncio.gather` 并发各自独立 `Agent.reply()`，应用层显式合并；分支只读同一份 `evidence_set_id`，不看对方中间结论（无交叉验证的扇出互看错误放大 17.2×，中心化 4.4×） |

---

## 3. 统一架构图

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│ agent-service（Python + FastAPI + AgentScope 2.0）                                 │
│                                                                                  │
│  FastAPI                                                                         │
│   /agent/diagnose  ┐                                                             │
│   /agent/draft     ├──▶ Agent 运行时 ──▶ 按 capability 取已装配的形态             │
│   /agent/orchestration/... ┘                                                     │
│                                      │                                           │
│        ┌─────────────────────────────┼─────────────────────────────┐             │
│        ▼                             ▼                             ▼             │
│  ① diagnosis Agent            ② draft_chain              ③ 编排 flows/           │
│  model ↔ tool 循环            4 个直线 flow 步骤          确定性步骤 + 能力节点      │
│  （开放式取证）                （顺序 await + 策略）       + barrier + gate         │
│        │                             │                             │             │
│        │                             │                  能力节点 invoke ─────┐    │
│        │                             │                             │        │    │
│        │◀────────────────────────────┼─────────────────────────────┘        │    │
│        │◀── 编排按需调用诊断/草稿能力 ────────────────────────────────────┘    │
│        ▼                                                                         │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │ Toolkit（按 capability + tenant 双维切片）                                 │   │
│  │  read_only 工具 ‖ requires_confirmation + writes_via 写工具               │   │
│  │  放行 / 拦截类工具：不注册到任何能力                                        │   │
│  │ Permission（启动断言）：只读 / 无写 client / 写需确认 / 无放行工具            │   │
│  └──────┬──────────┬──────────┬──────────┬──────────┬──────────────────────┘   │
│         ▼          ▼          ▼          ▼          ▼                          │
│     ACL 过点   ACL 工艺   ACL 物料   ACL 设备   RAG / 追溯读模型                   │
│     （只读）   （版本钉死）（只读）   （只读）   （检索 / 证据集回查）              │
│         │          │          │          │          │                          │
│         └──────────┴────┬─────┴──────────┴──────────┘                          │
│                         ▼                                                       │
│      写路径（仅编排能力）：ACL 写 client + 工具网关签发的一次性动作令牌              │
│      -> 各上下文正常应用服务（聚合根不变式 + 事务发件箱）                            │
└─────────────────────────┬────────────────────────────────────────────────────────┘
                          ▼
     生产执行服务(Java) / 制造资源服务(Java) / 设备管理服务(Java) / 追溯读模型(MySQL) / rag-service(文档工具)
                          ▲
                          │ aiokafka：equipment.fault / process.route.activated / defect_rate.spike
                          │ -> 路由到对应能力（编排 / 草稿 / 诊断）
```

**图例要点**：

- **同一个 runtime**：三种能力在启动时按 `CapabilitySpec` 装配一次，运行期不再分支。
- **跨能力调用走同一 trace**：编排的能力节点 `invoke(..., parent_session_id=session_id)`（`invoke` 是应用层 `await`，不是框架移交原语），诊断的每一步工具调用都挂在编排会话的同一个 `trace_id` 下；跨能力只传 `evidence_set_id` / `session_id` / `traceparent`，不传消息历史。
- **写路径只在编排 flow 里**：诊断与草稿的 ACL 只有读动词，写 ACL 只挂在编排的写工具上，且必须带网关签发的一次性动作令牌。

---

## 4. 运行时形态①：诊断 ReAct 流程图

```text
输入：提问（"SN-001 焊接不良根因"）或结构化异常（mismatch / fault / complaint）
  ↓
[推理步] 需要先拿过点轨迹 -> tool_call: query_pass_records
[工具步]  执行 -> 过点记录（version=v3, asset_id=...）
  ↓
[推理步] 版本 v3 -> tool_call: query_process_route(v3)
[工具步]  执行 -> 工艺步骤 / 焊接站参数模板
  ↓
[推理步] -> tool_call: query_material_batch + query_device_params
[工具步]  执行 -> 锡膏批次 B-77 / 贴片机参数偏移
  ↓
[推理步] -> tool_call: query_defect_rate(B-77)
[工具步]  执行 -> 同批次不良率 12%，高于基线
  ↓
[推理步] 无 tool_call -> 输出 DiagnosisReport（5M1E 排序 + 证据 + 置信度 + evidence_set_id）
  ↓ 收口
```

| 项 | 取值 | 落地 |
|----|------|------|
| 最大步数 | 步数与工具调用预算上限 `=20` | 一次 model+tool 算 2 步，≤10 次工具调用；超预算即终止并兜底 |
| 单工具超时 | ≤2s | httpx timeout |
| 整会话超时 | ≤60s | `asyncio.wait_for` |
| 收口条件 | 模型无 tool call | `DiagnosisReport` Pydantic 校验 |
| 会话内上下文 | `AgentState` + 框架 `Storage`（本项目落 MySQL；`SessionService` 是其上门面，旧名 `SessionManager` 已失效） | 多轮对话记忆与会话隔离（按 `session_id`）；**不承担跨闸门续跑**（框架只有单次调用作用域内的 `RequireUserConfirmEvent` 与 `interrupt` 端点，无 `Command(resume=…)` / checkpoint 续跑原语，且不自建同语义机制） |
| 兜底 | 超预算 / 超时 / 连续 3 次工具失败 | 返回 `partial` 报告"已转人工"，不硬答；`failure_count >= 2` 本段置 `WAITING` 并推异常卡 |

> 设计目标骨架，尚未落地（当前代码仓 `src/` 无相应实现）。下列参数名为本仓库装配层的写法，非逐字核实过的框架关键字；已核实的框架入参只有 `name` / `model` / `toolkit` / `offloader`，事件流走 `reply_stream`（`print` 接口已 deprecated）。

```python
# app/ai/diagnosis_agent.py
def build_diagnosis_agent(model, toolkit, deps) -> Agent:
    """诊断能力 = 统一 Agent 类（内置 ReAct）+ 只读 Toolkit 切片。"""
    return Agent(
        name="diagnosis",
        model=model,                       # AgentScope Model 抽象，可插拔 provider
        toolkit=toolkit.tools_for("diagnosis", deps.tenant),  # 只读工具切片
        sessions=deps.session_service,     # SessionService（2.0.5 起）+ AgentState，storage 落 MySQL
        permission=deps.readonly_policy,   # 叠加层：每次 tool 执行前过 Permission 拦截
        max_steps=20,                      # 步数与工具调用预算上限
        tracing=deps.tracing_middleware,   # TracingMiddleware 注入 trace
    )
```

---

## 5. 运行时形态③：编排流程图（以换线为例）

```text
开始
  ↓
[plan]                   确定性步骤
  ↓
[query_first_article]    确定性步骤 -> [gate: FIRST_ARTICLE]   HITL 确认 -> 首件触发卡 -> 人确认
  ↓ PASS
[query_active_route]     确定性步骤（版本锚点强制）-> [gate: PROCESS_SWITCH] -> 人确认
  ↓ PASS
  ├──────────────────────┬──────────────────────┐
  ▼并行(gather)           ▼并行(gather)            │
[query_and_compare_tooling]  [query_and_compare_kitting]  确定性步骤：query + compare
  ├─ PASS                    ├─ PASS
  └─ FAIL(mismatch)          └─ FAIL(缺料)
  ↓                          ↓
[barrier]  确定性步骤：等两分支汇合，显式合并后分流（普通 if）
  │
  ├─ 都 PASS ──────▶ [draft_release_card] 确定性步骤：结构化拼装（非 LLM）
  │                    ↓ [gate: RELEASE] 人确认
  │                 过点上下文应用服务实际放行（P99≤200ms，规则引擎判定）
  │                    ↓ [done]
  │
  ├─ tooling FAIL ─▶ [root_cause]        ★invoke("diagnosis")   能力节点
  │                    ↓
  │                  [disposition_card]  ★invoke("drafting")    能力节点
  │                    ↓
  │                  [gate: DISPOSITION] 人确认 -> 处置落库 -> 回 tooling_check 重检
  │
  └─ kitting FAIL ─▶ [shortage_ticket] 确定性步骤：缺料确定性，产缺料待办后本段结束
  ↓
[done]
```

**关键**：换线全程 PASS 时，flow 只走 `plan → first_article → process_switch → tooling_check(PASS) ‖ kitting_check(PASS) → barrier → draft_release → gate_release → done`，**零 LLM 调用**。能力节点只在 mismatch 分支触发。

| 项 | 取值 | 落地 |
|----|------|------|
| 并行派发 | `asyncio.gather` 并发 `tooling_check` / `kitting_check` 两个执行体 | 各自独立、独立预算 / 超时 / 权限档位；单场景并行分支数受 `max_fanout ≤ 5` 约束 |
| barrier 汇合 | 两分支都返回后在应用层显式合并 | 汇合点是确定性裁决步骤（缺交叉验证的扇出会把错误放大 17.2×，中心化 4.4×） |
| 分支可见性 | 各分支只读同一份 `evidence_set_id`（关系型证据子集） | **不共享消息流、不看对方中间结论**；框架也不提供广播原语（`MsgHub` 等已删除） |
| gate 分段确认 | gate 停在工具网关一次性令牌（主闸）+ `Permission`（叠加层）；审批确认落 MES 工单/待办实体 | 本段推卡 + 开待办后即返回，**不挂起不占执行槽**；`/advance` 回调重新发起下一段并重查当下事实 |
| 确定性 flow 步骤不调 LLM | query / compare / barrier / gate / draft_release_card | 直接调 ACL，不进 `Toolkit`；`node_type=CODE` |
| 能力节点调 LLM | root_cause / fault_impact / traceability / draft_* | `invoke(capability)`（应用层 `await`）；`node_type=AGENT` |
| 本段步数 / 单次调用时长 | 步数与工具调用预算上限 `=40` / `timeout=300s`（**不含等人确认**） | 超限标 `FAILED` 转人工 |
| 故障隔离 | 能力节点连续失败 2 次 / 置信度 low | 本段置 `WAITING` 推异常卡并转人工待办，不自动重试到死 |

> 设计目标骨架，尚未落地。2.0 没有声明式协同原语（`Pipeline` 的 `sequential` / `fanout` / `MsgHub` 已删除，`GoalPipeline` / `TeamPipeline` 仅 experimental 且明确"成员间不互相说话"），所以编排形状就是普通 Python：顺序 = 依次 `await`，并行 = `asyncio.gather`，分流 = 普通 `if`。步骤名（plan / tooling_check / barrier / gate_release…）是本仓库自命名的 flow 步骤，不是框架概念。确认等待、超时升级、重试与补偿归 MES 工单/审批流与工作流引擎（ADR-05），不在 `flows/` 内，也不在 agent 侧挂起。

```python
# app/ai/flows/changeover.py（节选）
# 编排 = 分段 flow：每段都是确定性 flow 步骤 + 能力节点 + barrier + gate，全部普通 Python
# 一次调用跑到 gate 即返回；审批确认后由 /advance 重新发起下一段（重查当下事实，不恢复快照）

async def seg_first_article(self, ctx) -> FlowResult:
    await self.steps.plan(ctx)                                   # 确定性步骤
    await self.steps.query_first_article(ctx)                    # 确定性步骤
    return await self.gates.open_gate(ctx, "FIRST_ARTICLE")      # 产待办 + 推卡 -> 本段结束

async def seg_tooling_kitting(self, ctx) -> FlowResult:
    ctx.facts = await self.steps.requery_live_facts(ctx)          # ★ 重查当下现场事实（首件/路线/钢网/齐套）
    await self.steps.query_active_route(ctx)                     # 确定性步骤（版本锚点强制）
    # 并行：asyncio.gather 并发各自独立的执行体，汇合即返回，不持有流程状态
    tooling, kitting = await asyncio.gather(
        self.steps.query_and_compare_tooling(ctx),
        self.steps.query_and_compare_kitting(ctx),
    )
    route = self.steps.barrier(tooling, kitting)                 # 确定性分流（普通 if）
    if route == "kitting_fail":
        return await self.tickets.open_shortage_ticket(ctx)      # 确定性：产缺料待办后本段结束
    if route == "tooling_fail":                                  # 仅此处进能力节点
        await self.nodes.root_cause(ctx)                         # 能力节点：invoke("diagnosis")
        await self.nodes.disposition_card(ctx)                   # 能力节点：invoke("drafting")
        return await self.gates.open_gate(ctx, "DISPOSITION")    # 处置待办 + 网关一次性令牌
    await self.steps.draft_release_card(ctx)
    return await self.gates.open_gate(ctx, "RELEASE")            # 全 PASS 也要人放行
```

> 分段拆分本身就是证据：`seg_*` 之间不传递任何内存态，下一段只需要 `session_id` + `scenario` 就能起步——所以没必要挂起。人在屏前的即时确认（同一调用内）仍可直接拿令牌往下走，不开工单。

---

## 6. 运行时形态②：草稿固定链

```text
DraftRequest(report, draft_kind, tenant)   —— 依次 await 四个 flow 步骤
  ↓
[resolve_evidence]    确定性步骤：fetch_evidence_set(evidence_set_id) -> 证据条目 + 版本锚点
  ↓
[retrieve_knowledge]  确定性步骤：search_docs(query, version_anchor) -> 历史 8D / 现有 SOP / 模板
  ↓
[generate]            能力节点（LLM）：按 draft_kind 分派 DraftBuilder -> 强约束 Draft（Pydantic）
  ↓
[validate_archive]    确定性步骤：Pydantic 校验 + requires_confirmation=True + 置信度分级 + 存档
  ↓ 收口（返回 Draft，等人在正式界面确认）
```

| 项 | 取值 | 落地 |
|----|------|------|
| 步数上限 | 步数与工具调用预算上限 `=8` | 固定 4 步，留重试余量 |
| 超时 | ≤90s | 检索 + 生成的合理窗口 |
| 写能力 | 无 | ACL 只有 `fetch/query/search`；挂 `nowrite_policy`（本仓库自定义规则集名）；主闸仍是工具网关一次性动作令牌，草稿侧无任何写通道 |
| 兜底 | 证据缺失 / 置信度 < 0.5（`confidence` 由规则算，不采信 LLM 自评） | `needs_review=True`，不进确认下达流程；`gaps` 显式声明 |

---

## 7. confirmation gate 时序（分段确认 / 回调起下一段，**无挂起与续跑**）

```text
flow 驱动        能力节点/确定性步骤    gate 步骤          UI/责任人        /advance        应用服务REST
    │                │                   │                 │                │                │
    │── 执行步骤 ───▶│                   │                 │                │                │
    │                │── card ──────────▶│                 │                │                │
    │                │   (ActionCard +   │                 │                │                │
    │                │    hypothesis)    │                 │                │                │
    │                │                   │── HITL 确认 ────┼────────────────┼───────────────┤
    │                │                   │  (落成 MES 工单/待办实体，本段 return、进程可退出)
    │                │                   │── push 卡片 ───▶│                │                │
    │                │                   │  (事件系统+Kafka)                │                │
    │                │                   │                 │── 看 evidence  │                │
    │                │                   │                 │   + hypothesis │                │
    │                │                   │                 │── 确认/拒绝 ──▶│                │
    │                │                   │                 │                │── issue token ─┤
    │                │                   │                 │                │ (action:target │
    │                │                   │                 │                │  + 过期)        │
    │                │                   │◀─────────── 审批完成回调（新一次调用，重查当下事实）─────┤
    │                │                   │── 校验 token ───┤                │                │
    │                │                   │── 若 PASS ──────┼────────────────┼── 写落库 ─────▶│
    │                │                   │                 │                │  (聚合根不变式 │
    │                │                   │                 │                │  + 事务发件箱) │
    │                │                   │◀─────────────────────────────────── 返回结果 ──────┤
    │◀── step done ──┤                   │  gate_decision=PASS              │                │
    │── 下一步 ──────────────────────────────────────────────────────────────────────────────▶│
```

要点：

- **本段与下一段是两次独立调用**，中间不在 agent 侧保存任何可恢复的流程快照：本段推卡并把待办落成 MES 已有工单实体后即 `return`，审批完成回调 `/advance` 后重新发起下一段并按 `evidence_set_id` **重查当下现场事实**（MES 数据时变，不恢复快照）。`AgentState` 经框架 `Storage`（本项目落 MySQL）只承载**会话内上下文与多轮记忆**；责任人分派、催办、超时升级、跨天全在工单实体生命周期内（ADR-05）；`BackgroundTaskManager` 只托管单次调用的短后台任务，**不做 gate 计时与催办**。
- token 由**工具网关**签发并校验（主闸），绑定 `action:target`（如 `isolation.issue:tenant_001`、`process_route.activate:RR-100:v4`）+ 过期，用后即废、过期 fail closed，防篡改防重放；无效则 `REJECT`。`Permission` 是叠加层，内层失效不得导致外层放行。
- 写落库发生在**最右侧应用服务**，不是 Agent 进程——与正常写路径完全一致。
- gate 决策（谁 / 何时 / 基于哪张卡 / 假设 / token）落 `agent_step_record`，全可审计。

---

## 8. 三种形态共用的约束

| 约束 | 落地 |
|------|------|
| 工具按能力 + 租户切片 | `Toolkit.tools_for(capability, tenant)` |
| 写动作的主闸 | **工具网关的一次性动作令牌**（绑定 `action:target` + 用户 + 过期，用后即废，失败 fail closed）+ 服务端二次鉴权 + 参数 schema 白名单 + 幂等键两把 |
| 写工具必须声明确认与落库路径 | `Permission`（叠加层）断言（`requires_confirmation` + `writes_via`）；内层失效不得导致外层放行 |
| 三套本仓库自定义规则集名 | `readonly_policy`（诊断，挂 `EXPLORE` 类只读档位）/ `nowrite_policy`（草稿）/ `gated_write_policy`（编排）——我们在 `PermissionEngine` / `PermissionRule` / `PermissionMode` 之上**组合出的策略集名称，不是框架类名** |
| 无写权限能力不得持写工具 / 写 client | 能力声明 + ACL 方法名断言 |
| 放行 / 拦截类工具不注册 | 前缀断言（`pass_judge` / `force_release` / `release_*` / `block_pass`） |
| 版本一致性 | ACL 强制版本锚点 + `ACTIVE` 校验；锚点取不到即拒答 |
| 步数与时长双闸门 | 步数与工具调用预算上限 + `asyncio.wait_for` |
| 分段与确认回调 | `session_id` 作为会话键 + MES 工单/待办实体承载确认；`/advance` 起下一段（**不挂起、不续跑、不恢复快照**） |
| 跨能力同 trace | `parent_session_id` + `traceparent` 透传；排障用 OTel + Tempo/Jaeger（`TracingMiddleware`）+ `reply_stream` 事件回放——`AgentScope Studio` 已归档，不作为工具列 |

---

## 9. 一句话定位

"FactoryAgent 用一套 AgentScope 2.0 编程模型承载三种运行时形态：诊断是 `model ↔ tool` 的开放式 ReAct 循环（内置于统一 `Agent` 类，步数与工具调用预算上限 = 20），草稿是固定 4 步的顺序 `await` 直线链（差异由策略分派表达），编排是确定性 flow 步骤 + 能力节点 + barrier + gate 的 `flows/` 普通 Python（并行用 `asyncio.gather` 并发各自独立 `Agent.reply()`、结论在应用层显式合并，换线全 PASS 时零 LLM）。三种形态共用同一个 runtime、同一张 `Toolkit`、同一套 `Permission` 启动断言、同一条 trace，跨能力调用靠 `parent_session_id` 串起来、上下文只传 `evidence_set_id`；工具网关的一次性动作令牌作为主闸、`Permission` 作叠加层，写动作前必须停下来等人确认——**Agent 无状态分段执行**：本段产出动作卡并落成 MES 工单/待办实体后就返回，审批回来重新发起下一段并重查当下事实（`AgentState` + `SessionService` 只承载会话内上下文与多轮记忆，不做跨进程续跑）。"
