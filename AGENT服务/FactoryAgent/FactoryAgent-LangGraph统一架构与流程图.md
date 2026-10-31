# FactoryAgent —— LangGraph 统一架构与流程图

> 本文集中呈现 FactoryAgent 的 **LangGraph 架构形态**：一个运行时承载**三种图形态**（开放式 ReAct / 固定链 / supervisor 编排），以及各自的流程图与关键约束。
> **核心口径**：诊断、草稿编写、流程编排不是三个 Agent，而是**同一个 `AgentRuntime` 装配出的三张图**；它们共用 `ToolRegistry`、`ToolPolicyGate`、checkpointer、trace 注入点。
> 配套说明见 [总体设计与实现方案](FactoryAgent-总体设计与实现方案.md) §3/§4、[诊断能力](FactoryAgent-诊断能力.md) §2、[草稿编写能力](FactoryAgent-草稿编写能力.md) §1.2、[流程编排能力](FactoryAgent-流程编排能力.md) §2。

---

## 1. 为什么全套用 LangGraph

| 需要的能力 | LangGraph 提供的 | 对本系统的意义 |
|-----------|-----------------|---------------|
| 开放式取证循环 | `StateGraph` + `conditional_edges` | 诊断的 `model ↔ tool` 循环显式化，可对每条边加权限拦截、超时、trace |
| 固定步骤链 | 线性边 + checkpointer | 草稿链"取证据 → 检索 → 生成 → 校验"可中断恢复，与诊断共用底座 |
| 代码/LLM 混排 | 节点可以是任意 Python 函数 | 编排图里代码节点不消耗 LLM 调用，能力节点才调 LLM |
| 并行 + 汇合 | `conditional_edges` 返回多目标 + barrier 节点 | 钢网程序比对 ‖ 齐套比对并行，放行前 barrier 硬校验 |
| 人在回路 | `interrupt` / `Command(resume=…)` | confirmation gate 不阻塞进程，state 落 MySQL 可跨进程续跑 |
| 硬上限 | `recursion_limit` | 直接对应"最大步数"红线，框架级兜底而非口头约束 |
| 子图组合 | 编译后的图可 `ainvoke` | 编排直接调用诊断 / 草稿，不需要另起服务或复制逻辑 |

**不选**：AutoGen（conversational 模式适合多 agent 对话，不适合严格步骤编排 + 代码节点）；CrewAI（抽象偏高，难做细粒度权限拦截与 gate 状态机）；裸 `AgentExecutor`（黑盒循环，难做细粒度拦截）。

---

## 2. 三种图形态对比

| 维度 | ① 开放式 ReAct（诊断） | ② 固定链（草稿） | ③ supervisor（编排） |
|------|----------------------|-----------------|---------------------|
| 图结构 | `model ↔ tool` 循环 | 直线 4 节点 | 代码节点 + 能力节点 + barrier + gate |
| 是否开放规划 | 是（取证路径由中间结果驱动） | 否（步骤固定） | 否（边显式硬编码） |
| LLM 消耗 | 每步一次模型调用 | 一次结构化生成 | 仅能力节点；纯代码路径为 0 |
| `recursion_limit` | 20（≤10 次工具调用） | 8 | 40 |
| 会话超时 | 60s | 90s | 3600s（含人确认等待） |
| 写权限 | 无 | 无 | 有（`requires_confirmation` + `writes_via`） |
| 典型打断点 | 无（一次性出报告） | 无（返回草稿等前端确认） | gate 节点 `interrupt` |

---

## 3. 统一架构图

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│ agent-service（Python + FastAPI + LangGraph）                                     │
│                                                                                  │
│  FastAPI                                                                         │
│   /agent/diagnose  ┐                                                             │
│   /agent/draft     ├──▶ AgentRuntime ──▶ 按 capability 取已装配的图               │
│   /agent/orchestration/... ┘                                                     │
│                                      │                                           │
│        ┌─────────────────────────────┼─────────────────────────────┐             │
│        ▼                             ▼                             ▼             │
│  ① diagnosis_graph            ② draft_chain              ③ supervisor_graph      │
│  model_node ↔ tool_node       4 个直线节点                代码节点 + 能力节点      │
│  （开放式取证）                （固定链 + 策略）           + barrier + gate        │
│        │                             │                             │             │
│        │                             │                  能力节点 invoke ─────┐    │
│        │                             │                             │        │    │
│        │◀────────────────────────────┼─────────────────────────────┘        │    │
│        │◀── 编排按需调用诊断/草稿子图 ────────────────────────────────────┘    │
│        ▼                                                                         │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │ ToolRegistry（按 capability + tenant 双维切片）                             │   │
│  │  read_only 工具 ‖ requires_confirmation + writes_via 写工具               │   │
│  │  放行 / 拦截类工具：不注册到任何能力                                        │   │
│  │ ToolPolicyGate（启动断言）：只读 / 无写 client / 写需确认 / 无放行工具        │   │
│  └──────┬──────────┬──────────┬──────────┬──────────┬──────────────────────┘   │
│         ▼          ▼          ▼          ▼          ▼                          │
│     ACL 过点   ACL 工艺   ACL 物料   ACL 设备   RAG / 图服务                       │
│     （只读）   （版本钉死）（只读）   （只读）   （检索 / 子图回查）                │
│         │          │          │          │          │                          │
│         └──────────┴────┬─────┴──────────┴──────────┘                          │
│                         ▼                                                       │
│      写路径（仅编排能力）：ACL 写 client + confirmation token                      │
│      -> 各上下文正常应用服务（聚合根不变式 + 事务发件箱）                            │
└─────────────────────────┬────────────────────────────────────────────────────────┘
                          ▼
     生产执行服务(Java) / 制造资源服务(Java) / 设备管理服务(Java) / RAG 服务
                          ▲
                          │ aiokafka：equipment.fault / process.route.activated / defect_rate.spike
                          │ -> 路由到对应能力（编排 / 草稿 / 诊断）
```

**图例要点**：

- **同一个 runtime**：三张图在启动时按 `CapabilitySpec` 装配一次，运行期不再分支。
- **跨能力调用走同一 trace**：编排的能力节点 `invoke(..., parent_session_id=session_id)`，诊断的每一步工具调用都挂在编排会话的同一个 `trace_id` 下。
- **写路径只在编排图里**：诊断与草稿的 ACL 只有读动词，写 ACL 只挂在编排的写工具上，且必须带 confirmation token。

---

## 4. 图形态①：诊断 ReAct 流程图

```text
输入：提问（"SN-001 焊接不良根因"）或结构化异常（mismatch / fault / complaint）
  ↓
[model_node] 需要先拿过点轨迹 -> tool_call: query_pass_records
[tool_node]  执行 -> 过点记录（version=v3, asset_id=...）
  ↓
[model_node] 版本 v3 -> tool_call: query_process_route(v3)
[tool_node]  执行 -> 工艺步骤 / 焊接站参数模板
  ↓
[model_node] -> tool_call: query_material_batch + query_device_params
[tool_node]  执行 -> 锡膏批次 B-77 / 贴片机参数偏移
  ↓
[model_node] -> tool_call: query_defect_rate(B-77)
[tool_node]  执行 -> 同批次不良率 12%，高于基线
  ↓
[model_node] 无 tool_call -> 输出 DiagnosisReport（5M1E 排序 + 证据 + 置信度）
  ↓ END
```

| 项 | 取值 | 落地 |
|----|------|------|
| 最大步数 | `recursion_limit=20` | 一次 model+tool 算 2 步，≤10 次工具调用；超限抛 `GraphRecursionError` |
| 单工具超时 | ≤2s | httpx timeout |
| 整会话超时 | ≤60s | `asyncio.wait_for` |
| 收口条件 | 模型无 tool call | `DiagnosisReport` Pydantic 校验 |
| 中断恢复 | `SqlSaver` | state 落 MySQL，进程重启续跑 |
| 兜底 | 超限 / 超时 / 连续 3 次工具失败 | 返回 `partial` 报告"已转人工"，不硬答 |

```python
# app/ai/diagnosis_graph.py
def build_diagnosis_graph(llm, registry, deps) -> CompiledGraph:
    g = StateGraph(AgentState)
    g.add_node("model", ModelNode(llm, registry, capability="diagnosis"))
    g.add_node("tools", ToolNode(registry, deps.step_repo, deps.metrics, "diagnosis"))
    g.add_edge(START, "model")
    g.add_conditional_edges("model", route_tools, ["tools", END])
    g.add_edge("tools", "model")
    return g.compile(checkpointer=deps.checkpointer)
```

---

## 5. 图形态③：编排流程图（以换线为例）

```text
START
  ↓
[plan]                   代码
  ↓
[query_first_article]    代码 -> [gate: FIRST_ARTICLE]      interrupt -> 首件触发卡 -> 人确认
  ↓ PASS
[query_active_route]     代码（版本锚点强制）-> [gate: PROCESS_SWITCH] -> 人确认
  ↓ PASS
  ├──────────────────────┬──────────────────────┐
  ▼并行(代码)            ▼并行(代码)              │
[query_and_compare_tooling]  [query_and_compare_kitting]   代码：query + compare
  ├─ PASS                    ├─ PASS
  └─ FAIL(mismatch)          └─ FAIL(缺料)
  ↓                          ↓
[barrier]  代码：等两分支汇合，确定性分流
  │
  ├─ 都 PASS ──────▶ [draft_release_card] 代码：结构化拼装（非 LLM）
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
  └─ kitting FAIL ─▶ [suspend] 代码：缺料确定性，直接挂起催料
  ↓
[done]
```

**关键**：换线全程 PASS 时，图只走 `plan → first_article → process_switch → tooling_check(PASS) ‖ kitting_check(PASS) → barrier → draft_release → gate_release → done`，**零 LLM 调用**。能力节点只在 mismatch 分支触发。

| 项 | 取值 | 落地 |
|----|------|------|
| 并行派发 | `conditional_edges` 返回 `["tooling_check","kitting_check"]` | 自动并发 |
| barrier 汇合 | 两分支共同终点 | 都返回才执行；节点内确定性分流 |
| gate 中断 | `interrupt(value=action_card)` | state 落 MySQL，进程不阻塞；`/confirm` 后 `Command(resume=token)` |
| 代码节点不调 LLM | query / compare / barrier / gate / draft_release_card | 直接调 ACL，不进 ToolRegistry；`node_type=CODE` |
| 能力节点调 LLM | root_cause / fault_impact / traceability / draft_* | `invoke(capability)`；`node_type=AGENT` |
| 步数 / 时长 | `recursion_limit=40` / `timeout=3600s` | 超限标 `FAILED` 转人工 |
| 故障隔离 | 能力节点连续失败 2 次 / 置信度 low | `SUSPENDED` 推异常卡，不自动重试到死 |

```python
# app/ai/scenarios/changeover_graph.py（节选）
g.add_node("barrier", barrier_node)
g.add_node("root_cause", RootCauseNode(runtime))         # invoke("diagnosis")
g.add_node("disposition_card", DispositionCardNode(runtime))  # invoke("drafting")
g.add_conditional_edges("barrier", lambda s: s["barrier_route"],
                        ["draft_release", "root_cause", "suspend"])
g.add_conditional_edges("gate_disposition",
                        lambda s: ["tooling_check"] if s["retry_tooling"] else ["done"])
```

---

## 6. 图形态②：草稿固定链

```text
DraftRequest(report, draft_kind, tenant)
  ↓
[resolve_evidence]    代码：fetch_subgraph_nodes(subgraph_ref) -> 证据节点 + 版本锚点
  ↓
[retrieve_knowledge]  代码：search_docs(query, version_anchor) -> 历史 8D / 现有 SOP / 模板
  ↓
[generate]            LLM：按 draft_kind 分派 DraftBuilder -> with_structured_output(Draft)
  ↓
[validate_archive]    代码：Pydantic 校验 + requires_confirmation=True + 置信度分级 + 存档
  ↓ END（返回 Draft，等人在正式界面确认）
```

| 项 | 取值 | 落地 |
|----|------|------|
| 步数上限 | `recursion_limit=8` | 固定 4 步，留重试余量 |
| 超时 | ≤90s | 检索 + 生成的合理窗口 |
| 写能力 | 无 | ACL 只有 `fetch/query/search` |
| 兜底 | 证据缺失 / 置信度 < 0.5 | `needs_review=True`，不进确认下达流程 |

---

## 7. confirmation gate 时序（interrupt / resume）

```text
supervisor      能力节点/代码节点      gate_node          UI/责任人        /confirm        应用服务REST
    │                │                   │                 │                │                │
    │── run node ───▶│                   │                 │                │                │
    │                │── card ──────────▶│                 │                │                │
    │                │   (ActionCard +   │                 │                │                │
    │                │    hypothesis)    │                 │                │                │
    │                │                   │── interrupt ────┼────────────────┼───────────────┤
    │                │                   │  (state 落 MySQL, 进程不阻塞)     │                │
    │                │                   │── push 卡片 ───▶│                │                │
    │                │                   │  (WebSocket+Kafka)               │                │
    │                │                   │                 │── 看 evidence  │                │
    │                │                   │                 │   + hypothesis │                │
    │                │                   │                 │── 确认/拒绝 ──▶│                │
    │                │                   │                 │                │── issue token ─┤
    │                │                   │                 │                │ (action:target │
    │                │                   │                 │                │  + 过期)        │
    │                │                   │◀──────────────────── Command(resume=token) ──────┤
    │                │                   │── 校验 token ───┤                │                │
    │                │                   │── 若 PASS ──────┼────────────────┼── 写落库 ─────▶│
    │                │                   │                 │                │  (聚合根不变式 │
    │                │                   │                 │                │  + 事务发件箱) │
    │                │                   │◀─────────────────────────────────── 返回结果 ──────┤
    │◀── step done ──┤                   │  gate_decision=PASS              │                │
    │── 下一步 ──────────────────────────────────────────────────────────────────────────────▶│
```

要点：

- `interrupt` 后进程不阻塞，state 持久化在 MySQL，可承进程重启。
- token 绑定 `action:target`（如 `isolation.issue:tenant_001`、`process_route.activate:RR-100:v4`）+ 过期，防篡改防重放；无效则 `REJECT`。
- 写落库发生在**最右侧应用服务**，不是 Agent 进程——与正常写路径完全一致。
- gate 决策（谁 / 何时 / 基于哪张卡 / 假设 / token）落 `agent_step_record`，全可审计。

---

## 8. 三种形态共用的约束

| 约束 | 落地 |
|------|------|
| 工具按能力 + 租户切片 | `ToolRegistry.tools_for(capability, tenant)` |
| 写工具必须声明确认与落库路径 | `ToolPolicyGate` 断言（`requires_confirmation` + `writes_via`） |
| 无写权限能力不得持写工具 / 写 client | 能力声明 + ACL 方法名断言 |
| 放行 / 拦截类工具不注册 | 前缀断言（`pass_judge` / `force_release` / `release_*` / `block_pass`） |
| 版本一致性 | ACL 强制版本锚点 + `ACTIVE` 校验 |
| 步数与时长双闸门 | `recursion_limit` + `asyncio.wait_for` |
| 中断恢复 | `SqlSaver`（`thread_id = session_id`） |
| 跨能力同 trace | `parent_session_id` + `traceparent` 透传 |

---

## 9. 一句话定位

"FactoryAgent 用一套 LangGraph 编程模型承载三种图形态：诊断是 `model ↔ tool` 的开放式 ReAct 循环（`recursion_limit=20`），草稿是固定 4 节点直线链（差异由策略分派表达），编排是代码节点 + 能力节点 + barrier + gate 的 supervisor 图（换线全 PASS 时零 LLM）。三张图共用同一个 runtime、同一张工具注册表、同一套启动断言、同一条 trace，跨能力调用靠 `parent_session_id` 串起来；`interrupt` / `Command(resume=…)` 让写动作前必须停下来等人确认，`SqlSaver` 让长流程跨进程续跑。"
