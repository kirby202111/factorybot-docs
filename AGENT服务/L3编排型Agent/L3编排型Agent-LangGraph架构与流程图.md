# L3 编排型 Agent —— LangGraph 架构与流程图

> 本文从 [L3编排型Agent-实现方案.md](L3编排型Agent-实现方案.md) 抽取 LangGraph 相关内容，集中呈现 **multi-agent 总体架构图**、**supervisor 编排流程图（代码节点 + agent 调用节点 + 并行 / barrier / gate）**与 **confirmation gate 的 interrupt/resume 时序**，便于聚焦理解 LangGraph 在 L3 中的落地形态。
> **核心口径**：L3 = **编排代码层（确定性）+ 4 类非确定 agent 能力（A/B/C/D）**。supervisor StateGraph 里**代码节点（不调 LLM）和 agent 调用节点（调 LLM）混合编排**——这是本文图示的重点。
> 配套说明见实现方案 §2.2、§3、§5.1、§5.3、§7.2、§7.3。

---

## 1. 为什么选 LangGraph supervisor 模式

L3 的核心是**跨上下文编排**，但编排的大部分是确定性步骤（顺序固定、判定规则固定、输入结构化），只有少数非确定决策点需要 agent。LangGraph 的 `StateGraph` 适合这种"代码 + agent 混合编排"：

- **代码节点与 agent 节点同图**：LangGraph 的节点可以是任意 Python 函数——`query+compare`、`barrier`、`gate` 是纯代码节点（不调 LLM）；`root_cause`、`fault_impact`、`traceability`、`draft_*` 是 agent 调用节点（调 LLM + 工具）。两类节点在同一张图里混合，**代码节点不消耗 LLM 调用**。
- **supervisor + subgraph = multi-agent**：4 类 agent 能力各自是独立 `StateGraph`，有自己的工具集、system prompt、`recursion_limit`，互不污染上下文。
- **显式图 > 隐式 prompt**：编排步骤的串行 / 并行 / barrier / gate 在图里是显式边，可对每条边加条件（"都 PASS 才进放行"、"mismatch 才进 A"）。用纯 prompt 让一个模型自己编排，易丢步骤、难审计。
- **`interrupt` / `Command(resume=...)` 原生支持人在回路**：confirmation gate 处暂停，state 落 MySQL（checkpointer），进程不阻塞；人确认后 `resume` 续跑。
- **并行分支 + 汇合 barrier**：`conditional_edges` 返回多目标即并行派发，barrier 节点自动等所有分支返回。
- 与 L1 同构，复用 `ToolRegistry` / `tool_call_trace` / OTel 底座；能力 C 直接嵌入 L1 诊断图作为子图。

不选 AutoGen（conversational，适合对话协作，难做严格步骤编排 + 代码节点）/ CrewAI（抽象偏高，难做细粒度权限拦截与 gate 状态机）。

---

## 2. 总体架构图

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ agent-service（Python + FastAPI + LangGraph）                            │
│                                                                           │
│  FastAPI ── POST /agent/l3/{scenario}/start ──▶ L3Orchestrator           │
│   POST /agent/l3/{session_id}/confirm           │ 构建 supervisor StateGraph│
│                                                  ▼                         │
│              ┌─────────────────────────────────────────────────────────┐ │
│              │  SupervisorGraph（编排代码层，不持任何工具，不调 LLM）       │ │
│              │  代码节点: plan / query+compare / barrier / gate / write  │ │
│              │  agent 调用节点: 仅在非确定决策点触发 A/B/C/D              │ │
│              └──┬───────────────┬───────────────┬───────────────┬──────┘ │
│                 │ subgraph A    │ subgraph B    │ subgraph C    │ subgraph D│
│      ┌──────────▼┐ ┌───────────▼────────┐ ┌────▼────────┐ ┌────▼──────┐  │
│      │RootCause   │ │FaultImpact         │ │Traceability │ │DraftAgents│  │
│      │Agent       │ │Agent               │ │Agent        │ │SOP/8D/    │  │
│      │(A 根因)    │ │(B 隔离范围)        │ │(C 追溯,     │ │ReworkCraft│  │
│      │            │ │                    │ │ 嵌入 L1)    │ │(D 生成)   │  │
│      └─────┬──────┘ └───────────┬────────┘ └────┬────────┘ └────┬──────┘  │
│            │ model↔tool          │ model↔tool      │ model↔tool   │         │
│            ▼                    ▼                ▼             ▼         │
│      ┌──────────────────────────────────────────────────────────────┐    │
│      │  ToolRegistry（按 capability 裁剪 + WriteToolGate）            │    │
│      │  各能力只读 toolset ‖ 写 toolset(requires_confirmation)        │    │
│      │  注：代码节点的 query 不进 Registry，直接调 ACL（确定性，非 LLM）│    │
│      │  注：放行/拦截类工具不注册到任何 capability                      │    │
│      └──────┬──────────┬──────────┬──────────┬──────────┬────────────┘    │
│             ▼          ▼          ▼          ▼          ▼                 │
│        ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐            │
│        │ACL 钢网│ │ACL 设备│ │ACL 过点│ │ACL 工艺│ │ACL 质量│            │
│        │/工艺审计│ │遥测/FMEA│ │/WIP    │ │/SOP    │ │/8D    │            │
│        └───┬────┘ └───┬────┘ └───┬────┘ └───┬────┘ └───┬────┘            │
└────────────┼──────────┼──────────┼──────────┼──────────┼──────────────────┘
             ▼          ▼          ▼          ▼          ▼
   生产执行服务(Java)  制造资源服务(Java)  ……  各上下文应用服务 REST
   (写:应用服务+发件箱，过聚合根不变式)
                                                          ▲
                              动作卡推送 + 人工确认          │
                      ┌──────────────────────────────────┴──┐
                      │  WebSocket(SSE) + Kafka 动作卡事件     │
                      │  → 责任人 UI  →  /confirm 端点         │
                      └──────────────────────────────────────┘
```

**图例要点**

- **supervisor 是纯代码编排器**：不持工具、不调 LLM，只做 plan / dispatch / barrier / gate——想越界也没工具调。
- **代码节点 vs agent 节点**：代码节点（query/compare/barrier/gate/write）直接调 ACL，不进 ToolRegistry，不调 LLM；只有 4 类 agent 能力（A/B/C/D）经 ToolNode 调 LLM + 工具。**换线全程 PASS 时，A/B/C/D 都不触发，LLM 调用为 0**。
- **写工具走 ACL → 上下文应用服务 REST**：不碰 MES 原始表，过聚合根不变式 + 事务发件箱；放行 / 拦截类工具**不注册到任何 capability**——过点放行能力从架构层不存在于 agent 工具集。
- **动作卡通道独立**：gate 的 interrupt 不阻塞进程，卡片经 WebSocket + Kafka 推 UI，人确认走 `/confirm` 端点 resume。

---

## 3. supervisor 编排流程图（以换线场景为例：代码节点 + agent 调用节点 + 并行 / barrier / gate）

```text
START
  ↓
[plan_node]  代码：决定步骤序列
  ↓
[query_first_article]  代码：查首件状态
  ↓
[gate: FIRST_ARTICLE]  代码节点 interrupt → 推首件触发卡 → 人确认 → resume
  ↓ PASS
[query_active_route]  代码：查工艺版本（ACL 强制 route_version）
  ↓
[gate: PROCESS_SWITCH]  代码节点 interrupt → 推工艺激活卡 → 人确认 → resume
  ↓ PASS
  ├──────────────────────┬──────────────────────┐
  ▼并行(代码)            ▼并行(代码)              │
[query_and_compare_tooling]   [query_and_compare_kitting]   代码：query+compare
  │ expected=ST-B              │ kit_rate==100%?            产出结构化结果
  │ actual=扫码                 │                            (PASS/FAIL+code)
  ├─ PASS                       ├─ PASS                      │
  └─ FAIL(mismatch)             └─ FAIL(缺料)                │
  ↓                            ↓                            │
[barrier_node]  代码：等两分支汇合，按结构化结果分流（确定性，非 agent 判定）
  │
  ├─ 都 PASS ────────────▶ [draft_release_card]  代码：结构化拼装放行卡（非 LLM）
  │                          ↓
  │                       [gate: RELEASE]  代码节点 interrupt → 推放行意图卡 → 人确认
  │                          ↓ PASS
  │                       过点上下文应用服务实际放行（过点主事务 + 规则引擎，P99≤200ms）
  │                          ↓
  │                       [done_node]
  │
  ├─ tooling FAIL ──────▶ [RootCauseAgent (A)]  ★agent 调用节点：自适应取证 + 根因假设 + 草拟处置卡
  │                          ↓
  │                       [gate: DISPOSITION]  代码节点 interrupt → 推处置卡（含根因假设+证据）→ 人确认
  │                          ↓ PASS
  │                       处置落库（如归还ST-A/领用ST-B，走钢网上下文应用服务）
  │                          ↓
  │                       回 [query_and_compare_tooling] 重检（代码节点，agent 不参与判定）
  │
  └─ kitting FAIL ──────▶ [SUSPENDED]  代码：缺料是确定的，不嵌 agent，直接挂起推线长催料
  ↓
[done_node]  代码：session.status=DONE
  ↓
END

★ = agent 调用节点（调 LLM），其余均为代码节点（不调 LLM）
```

**关键**：换线全程 PASS 时，图只走左侧 `plan → first_article → process_switch → tooling_check(PASS) ‖ kitting_check(PASS) → barrier → draft_release → gate_release → done`，**全程零 LLM 调用**。agent A 只在 `tooling_check` 产出 `FAIL(mismatch)` 时触发。

**节点与边的约束**

| 项 | 取值 | 落地 |
|----|------|------|
| 并行派发 | `conditional_edges` 返回 `["tooling_check","kitting_check"]` | LangGraph 自动并发执行两条分支 |
| barrier 汇合 | `barrier_node` 是两分支边的共同终点 | LangGraph 等两分支都返回才执行；节点内按结构化结果分流 |
| barrier 分流 | `conditional_edges` 按 `barrier_route` 路由 | 都 PASS → 放行；tooling FAIL → A；kitting FAIL → 挂起 |
| gate 中断 | `interrupt(value=action_card)` | state 落 MySQL，进程不阻塞；`/confirm` 端点 `Command(resume=token)` 续跑 |
| 代码节点不调 LLM | `query/compare/barrier/gate/draft_release` 是纯 Python 函数 | 直接调 ACL，不进 ToolRegistry；`l3_step_record.node_type=CODE` |
| agent 节点才调 LLM | `RootCauseAgent` 等是 subgraph | 经 ToolNode 调 LLM + 工具；`node_type=AGENT` |
| supervisor 步数上限 | `recursion_limit=40` | 含 gate 等待，超限抛 `GraphRecursionError` 标 FAILED |
| 整流程超时 | ≤3600s（含人确认等待） | `asyncio.wait_for` 包住图驱动 |
| 中断恢复 | `SqlSaver` checkpointer（`thread_id=session_id`） | 进程重启从断点续跑，gate 等待状态不丢 |
| 故障隔离 | agent 连续失败 2 次 / 置信度 low | 标 `SUSPENDED` 推异常卡，不自动重试到死 |
| barrier 防错 | tooling 或 kitting 未 PASS | 禁止推放行卡，分流到 agent A 或挂起——编排层防错（确定性硬校验，非 agent 判定） |

---

## 4. 其他场景的编排形态（代码骨架 + agent 能力组合）

各场景复用同一套代码节点 + agent 能力，只是组合不同：

### 4.1 设备故障复产（场景②）：代码骨架 + B

```text
[设备故障事件触发]
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行                  ▼并行                    │
[draft_repair_order]    [FaultImpactAgent (B)]   ★agent：故障模式推理 + 隔离集草拟
  代码：草拟维修单         │                        │
  ↓                       ↓                        │
[gate: REPAIR]         [gate: ISOLATION]          代码节点 interrupt → 人确认
  ↓ PASS                 ↓ PASS                    │
  └──────────────────────┴──────────────────────┘
  ↓
[计量复校 gate]  代码：复校结果确认（确定性，不嵌 agent）
  ↓ PASS
[复产首件 gate]  代码：barrier 等复校+点检 PASS → 推复产放行卡 → 人确认
  ↓
[done]
```

- 隔离范围判定嵌 B（非确定），其余（维修单草拟、复校 gate、复产 gate）是代码节点。
- 复校 / 复产 gate 是代码节点，agent 不碰这两道红线。

### 4.2 客诉 8D（场景③）：代码骨架 + C + D

```text
[客诉触发]
  ↓
[TraceabilityAgent (C)]  ★agent：嵌入 L1，5M1E 假设排序 + 证据链
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行                  ▼并行                    │
[供应商批次追溯]        [隔离范围判定]            │
  代码（版本钉死后查）    代码或复用 B             │
  ↓                       ↓                        │
  └──────────────────────┴──────────────────────┘
  ↓
[gate: ISOLATION]  代码节点 interrupt → 推隔离卡 → 人确认 → 返工上下文下达
  ↓ PASS
[DraftAgents.draft_8d (D)]  ★agent：草拟 8D 报告（拉追溯链 + 历史 8D）
  ↓
[gate: 8D_PUBLISH]  代码节点 interrupt → 推 8D 发布卡 → 人确认
  ↓
[done]
```

- 版本钉死由 ACL 代码做（`routeVersion` 强制过滤），C 在此之上做 5M1E 假设排序。
- 8D 草拟嵌 D（开放生成），隔离下达是代码 gate + 应用服务。

### 4.3 工艺变更落地（场景④）：代码骨架 + D

```text
[订阅 ProcessRouteActivated 事件]
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行                  ▼并行                    │
[DraftAgents.draft_sop (D)]  [资质核对]           ★agent 仅左侧：SOP 草拟（开放生成）
  ↓                            代码：操作工资质 ∈ 工艺要求资质集？（确定性，不嵌 agent）
  ↓ PASS                       ↓ PASS
  └──────────────────────┴──────────────────────┘
  ↓
[barrier]  代码：等双 PASS
  ↓
[新工艺首件验证 gate]  代码：推首件放行卡 → 人确认
  ↓
[done]
```

- SOP 草拟嵌 D（开放生成），资质核对是确定性查询（代码节点，不嵌 agent）——**这是"该用代码的没用 AI"的典型体现**。

---

## 5. confirmation gate 时序图（interrupt / resume）

```text
 supervisor          agent节点/代码节点    gate_node          UI/责任人        /confirm端点      应用服务REST
     │                   │                  │                 │                  │                 │
     │── run node ──────▶│                  │                 │                  │                 │
     │                   │── draft_* ──────▶│                 │                  │                 │
     │                   │   (ActionCard +  │                 │                  │                 │
     │                   │    hypothesis)   │                 │                  │                 │
     │                   │                  │── interrupt ────┼──────────────────┼─────────────────┤
     │                   │                  │   (state落MySQL,│                  │                 │
     │                   │                  │    进程不阻塞)  │                  │                 │
     │                   │                  │── push 卡片 ───▶│                  │                 │
     │                   │                  │   (WebSocket+   │                  │                 │
     │                   │                  │    Kafka)       │                  │                 │
     │                   │                  │                 │── 人查看 evidence│                 │
     │                   │                  │                 │   + hypothesis   │                 │
     │                   │                  │                 │   (trace_id)     │                 │
     │                   │                  │                 │── 确认/拒绝 ─────────────────────▶│
     │                   │                  │                 │                  │── issue token ──┤
     │                   │                  │                 │                  │  (绑定action:    │
     │                   │                  │                 │                  │   target+过期)   │
     │                   │                  │◀──────────────────────── Command(resume=token) ──────┤
     │                   │                  │── 校验 token ───┤                  │                 │
     │                   │                  │   valid_for?    │                  │                 │
     │                   │                  │── 若 PASS ─────▶│                  │                 │
     │                   │                  │   写落库 ──────────────────────────────────────────▶│
     │                   │                  │                 │                  │   应用服务      │
     │                   │                  │                 │                  │   过聚合根不变式│
     │                   │                  │                 │                  │   +事务发件箱   │
     │                   │                  │◀───────────────────────────────────── 返回结果 ─────┤
     │                   │                  │── gate_decision=PASS                            │
     │◀── step done ─────┤                  │                 │                  │                 │
     │── 下一步 ─────────────────────────────────────────────────────────────────────────────────▶│
```

**时序要点**

- `interrupt` 后 supervisor 进程不阻塞，state 持久化在 MySQL（`SqlSaver`），可承进程重启。
- confirmation token 绑定 `action:target`（如 `isolation.issue:tenant_001`、`process_route.activate:RR-100:v4`）+ 过期，防篡改、防重放；token 无效则 `gate_decision=REJECT`。
- 写落库发生在**最右侧应用服务**，不是 agent 进程——过聚合根不变式 + 事务发件箱，与 MES 正常写路径完全一致，只是触发源从"人点按钮"变成"Agent 草拟 + 人确认"。
- gate 决策（谁 / 何时 / 基于哪张卡 / agent 假设 / token）落 `l3_step_record`，全可审计。
- 动作卡带 `evidence` + `agent_hypothesis` + `confidence`，确认人基于证据与假设确认而非盲批。

---

## 6. 图驱动代码骨架（对应实现方案 §7.1、§7.2、§7.3、§7.4）

### 6.1 supervisor 构建（代码节点 + agent 调用节点 + 并行 + barrier + gate）

```python
# app/orchestration/scenarios/changeover_graph.py
class ChangeoverGraph:
    """换线场景：代码骨架 + 仅在 mismatch 分支嵌入 RootCauseAgent (A)。"""

    def __init__(self, code_nodes: CodeNodes, agents: AgentRegistry,
                 gates: GateManager, repo: L3Repo) -> None:
        self._c = code_nodes
        self._agents = agents
        self._gates = gates
        self._repo = repo

    def build(self) -> CompiledGraph:
        g = StateGraph(L3State)

        # 代码节点（不调 LLM）
        g.add_node("plan", self._c.plan)
        g.add_node("first_article", self._c.query_first_article)
        g.add_node("gate_first_article", self._gate("FIRST_ARTICLE"))
        g.add_node("process_switch", self._c.query_active_route)
        g.add_node("gate_process_switch", self._gate("PROCESS_SWITCH"))
        g.add_node("tooling_check", self._c.query_and_compare_tooling)   # 代码：query+compare
        g.add_node("kitting_check", self._c.query_and_compare_kitting)   # 代码：query+compare
        g.add_node("barrier", self._barrier_node)
        g.add_node("draft_release", self._c.draft_release_card)          # 代码：结构化拼装
        g.add_node("gate_release", self._gate("RELEASE"))
        g.add_node("done", self._c.done)

        # agent 节点（仅 mismatch 分支触发，调 LLM）
        g.add_node("root_cause", self._run_agent("root_cause"))          # A
        g.add_node("gate_disposition", self._gate("DISPOSITION"))

        g.add_edge(START, "plan")
        g.add_edge("plan", "first_article")
        g.add_edge("first_article", "gate_first_article")
        g.add_edge("gate_first_article", "process_switch")
        g.add_edge("process_switch", "gate_process_switch")
        # 工艺确认后并行派发钢网程序比对 + 齐套比对（返回多目标 = 并行分支）
        g.add_conditional_edges("gate_process_switch", lambda s: ["tooling_check", "kitting_check"])
        g.add_edge("tooling_check", "barrier")
        g.add_edge("kitting_check", "barrier")
        # barrier 按结构化结果分流（确定性，非 agent 判定）
        g.add_conditional_edges("barrier", self._barrier_route,
                                ["draft_release", "root_cause", "suspend"])
        g.add_edge("draft_release", "gate_release")
        g.add_edge("gate_release", "done")
        g.add_edge("root_cause", "gate_disposition")
        # 处置确认后回 tooling_check 重检（代码节点，agent 不参与判定）
        g.add_conditional_edges("gate_disposition",
                                lambda s: ["tooling_check"] if s["retry_tooling"] else ["done"])
        g.add_edge("done", END)
        return g.compile()

    async def _barrier_node(self, state: L3State) -> L3State:
        # 并行分支汇合：按结构化结果分流（确定性硬校验，非 agent 判定）
        t, k = state["tooling_result"], state["kitting_result"]
        if t["status"] == "PASS" and k["status"] == "PASS":
            state["barrier_route"] = "draft_release"
        elif t["status"] == "FAIL":           # 钢网/程序 mismatch → 交 agent A
            state["barrier_route"] = "root_cause"
            state["expected"] = t["expected"]
            state["actual"] = t["actual"]
            state["mismatch_code"] = t["code"]
        else:                                  # 缺料 → 确定性，不嵌 agent，挂起催料
            state["barrier_route"] = "suspend"
            state["status"] = "SUSPENDED"
            await self._gates.push_exception_card(state, "物料齐套未达标，请催料")
        return state

    def _barrier_route(self, state: L3State) -> str:
        return state["barrier_route"]

    def _run_agent(self, capability: str):
        async def fn(state: L3State) -> L3State:
            sub = self._agents.get(capability)
            result = await sub.ainvoke(
                state, config={"configurable": {"thread_id": state["session_id"]}})
            state["agent_hypothesis"] = result["hypothesis"]
            state["agent_confidence"] = result["confidence"]
            state["action_card"] = result["disposition_card"]
            await self._repo.save_agent_step(state, capability, result)
            return state
        return fn

    def _gate(self, step: str):
        async def fn(state: L3State) -> L3State:
            card = build_action_card(state, step)
            await self._repo.save_step(state["session_id"], step, "GATE_WAITING", card)
            decision = await self._gates.await_confirmation(state["session_id"], step, card)
            state[f"gate_{step.lower()}"] = decision
            await self._repo.record_gate(state["session_id"], step, decision)
            return state
        return fn
```

### 6.2 gate 的 interrupt / resume

```python
# app/orchestration/code_nodes/gate.py
class GateManager:
    def __init__(self, dispatcher: ActionCardDispatcher,
                 confirmation_store: ConfirmationStore) -> None:
        self._dispatcher = dispatcher
        self._store = confirmation_store

    async def await_confirmation(self, session_id: str, step: str,
                                 card: ActionCard) -> str:
        await self._dispatcher.push(card)                  # WebSocket + Kafka
        # interrupt：阻塞当前图执行，直到 Command(resume=...) 投入
        confirmation = await interrupt(value=card)
        if not confirmation.valid_for(card.writes_via_action()):
            return "REJECT"
        return "PASS" if confirmation.approved else "REJECT"
```

```python
# app/api/l3_router.py
@router.post("/agent/l3/{session_id}/confirm")
async def confirm_gate(
    session_id: str,
    req: ConfirmRequest,
    store: ConfirmationStore = Depends(get_store),
    graph: CompiledGraph = Depends(get_supervisor_graph),
) -> ConfirmResponse:
    token = store.issue(session_id, req.step, req.approved, req.user_id)
    await graph.ainvoke(
        Command(resume=token),
        config={"configurable": {"thread_id": session_id}},
    )
    return ConfirmResponse(step=req.step, decision="PASS" if req.approved else "REJECT")
```

### 6.3 应用服务编排（异步驱动 + 超时兜底）

```python
# app/application/l3_orchestrator.py
class L3Orchestrator:
    def __init__(self, supervisor: CompiledGraph,
                 session_manager: SessionManager) -> None:
        self._supervisor = supervisor
        self._sessions = session_manager

    async def start(self, req: L3Request, tenant: TenantContext) -> L3Session:
        session = await self._sessions.create(req, tenant)
        asyncio.create_task(self._drive(session, tenant))   # 异步驱动，gate 处 interrupt 暂停
        return session

    async def _drive(self, session: L3Session, tenant: TenantContext) -> None:
        try:
            await asyncio.wait_for(
                self._supervisor.ainvoke(
                    {"session_id": session.id, "tenant": tenant, **session.context},
                    config={
                        "recursion_limit": 40,
                        "configurable": {"thread_id": session.id},
                    },
                ),
                timeout=3600.0,   # 含人确认等待的整体上限
            )
        except (GraphRecursionError, asyncio.TimeoutError) as e:
            await self._sessions.mark_failed(session, str(e))
```

- `asyncio.create_task` + `interrupt`：L3 流程是长流程（含人工确认），HTTP 请求只负责"启动"，不阻塞等待全程。
- `recursion_limit=40` 与 `timeout=3600s` 形成"步数 + 时长"双闸门，超限标 FAILED 转人工，不硬走。
- `thread_id=session.id` 让 `SqlSaver` 按会话持久化与恢复，gate 等待状态可跨进程续跑。

---

## 7. 一句话定位

L3 用 LangGraph 的 **supervisor + subgraph** 做"编排代码层 + 4 类非确定 agent 能力"的混合编排——supervisor 是只做 plan / dispatch / barrier / gate 的纯代码 `StateGraph`（不持工具、不调 LLM），图里**代码节点**（query+compare / barrier / gate / write，不调 LLM）和 **agent 调用节点**（A 根因 / B 故障隔离 / C 客诉追溯 / D 生成类，调 LLM）混合，agent 只在非确定决策点触发（换线全程 PASS 时 LLM 调用为 0）；用 `conditional_edges` 并行派发无依赖步骤、`barrier_node` 按结构化结果确定性分流（mismatch 才进 A）、`interrupt` / `Command(resume=...)` 实现 confirmation gate 人在回路，state 经 `SqlSaver` 落 MySQL 可跨进程续跑——编排是图驱动而非 prompt 自由发挥，agent 只在 4 类代码做不了或做起来极复杂的非确定段赚回成本，硬防错与过点判定仍是代码 / 规则引擎的活，写的闸门始终在人手里。
