# L3 编排型 Agent —— LangGraph 架构与流程图

> 本文从 [L3编排型Agent-实现方案.md](L3编排型Agent-实现方案.md) 抽取 LangGraph 相关内容，集中呈现 **multi-agent 总体架构图**、**supervisor 编排流程图（含并行 / barrier / gate）**与 **confirmation gate 的 interrupt/resume 时序**，便于聚焦理解 LangGraph 在 L3 中的落地形态。
> 配套说明见实现方案 §2.2、§3、§5.1、§5.3、§7.2、§7.3。

---

## 1. 为什么选 LangGraph supervisor 模式

L3 的核心是**跨上下文编排**：换线 5 步有严格的串行 / 并行 / barrier / 人在回路关系，不是 L1 那种"模型自己决定下一步"的 ReAct 推理。LangGraph 的 `StateGraph` 把这些关系做成显式图：

- **supervisor + subgraph = multi-agent**：每个 sub-agent 是独立 `StateGraph`，有自己的工具集、system prompt、`recursion_limit`，互不污染上下文——这是 multi-agent 的本质。
- **显式图 > 隐式 prompt**：5 步的串行 / 并行 / barrier 在图里是显式边，可对每条边加条件（"③④ 都 PASS 才进 ⑤"）。用纯 prompt 让一个模型自己编排 5 步，易丢步骤、难审计。
- **`interrupt` / `Command(resume=...)` 原生支持人在回路**：confirmation gate 处暂停，state 落 MySQL（checkpointer），进程不阻塞；人确认后 `resume` 续跑——对应"人在回路"。
- **并行分支 + 汇合 barrier**：`conditional_edges` 返回多目标即并行派发，barrier 节点自动等所有分支返回。
- 与 L1 同构，复用 `ToolRegistry` / `tool_call_trace` / OTel 底座。

不选 AutoGen（conversational，适合对话协作，难做严格步骤编排）/ CrewAI（抽象偏高，难做细粒度权限拦截与 gate 状态机）。

---

## 2. 总体架构图

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ agent-service（Python + FastAPI + LangGraph）                            │
│                                                                           │
│  FastAPI ── POST /agent/changeover ──▶ ChangeoverOrchestrator            │
│   POST /agent/changeover/{id}/confirm     │ 构建 supervisor StateGraph     │
│                                            ▼                                 │
│              ┌───────────────────────────────────────────────────────┐   │
│              │  SupervisorGraph（编排，不持任何工具）                    │   │
│              │  plan → dispatch(并行/串行) → barrier → gate(interrupt) │   │
│              └──┬──────────┬──────────┬──────────┬──────────┬─────────┘   │
│                 │ subgraph │ subgraph │ subgraph │ subgraph │ subgraph    │
│      ┌──────────▼┐ ┌───────▼────────┐ ┌▼────────┐ ┌▼────────┐ ┌▼──────┐  │
│      │FirstArticle│ │ProcessSwitch   │ │Inspection│ │Material │ │PassRel│  │
│      │Agent       │ │Agent           │ │Agent     │ │Kitting  │ │Agent  │  │
│      │(首件)      │ │(工艺版本)      │ │(点检/计量)│ │(物料/WIP)│ │(过点) │  │
│      └─────┬──────┘ └───────┬────────┘ └────┬────┘ └────┬────┘ └───┬──┘  │
│            │ model↔tool      │ model↔tool      │           │         │     │
│            ▼                ▼                ▼           ▼         ▼     │
│      ┌──────────────────────────────────────────────────────────────┐    │
│      │  ToolRegistry（按 agent_role 裁剪 + WriteToolGate）           │    │
│      │  各 role 只读 toolset ‖ 写 toolset(requires_confirmation)     │    │
│      └──────┬──────────┬──────────┬──────────┬──────────┬────────────┘    │
│             ▼          ▼          ▼          ▼          ▼                 │
│        ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐            │
│        │ACL 首件│ │ACL 工艺│ │ACL 点检│ │ACL 物料│ │ACL 过点│            │
│        │httpx   │ │httpx   │ │httpx   │ │httpx   │ │httpx   │            │
│        └───┬────┘ └───┬────┘ └───┬────┘ └───┬────┘ └───┬────┘            │
└────────────┼──────────┼──────────┼──────────┼──────────┼──────────────────┘
             ▼          ▼          ▼          ▼          ▼
   生产执行服务(Java)  制造资源服务(Java)  ……  生产执行服务(Java)
   首件/工单/过点REST  工艺/物料/点检REST       过点应用服务REST
   (写:应用服务+发件箱)                          (写:应用服务+发件箱)
                                                          ▲
                              动作卡推送 + 人工确认          │
                      ┌──────────────────────────────────┴──┐
                      │  WebSocket(SSE) + Kafka 动作卡事件     │
                      │  → 操作工/线长 UI  →  /confirm 端点    │
                      └──────────────────────────────────────┘
```

**图例要点**

- **supervisor 不持工具**：只做 plan / dispatch / barrier / gate，想越界也没工具调——能力从架构层切掉。
- **sub-agent = subgraph**：每个 sub-agent 内部仍是 `model_node ↔ tool_node` 的 ReAct 小循环（同 L1），但工具集被 `agent_role` 裁剪到本上下文。
- **写工具走 ACL → 上下文应用服务 REST**：不碰 MES 原始表，过聚合根不变式 + 事务发件箱；过点 sub-agent 只有 `draft_pass_release_card`（意图卡），无放行 / 拦截 API。
- **动作卡通道独立**：gate 的 interrupt 不阻塞进程，卡片经 WebSocket + Kafka 推 UI，人确认走 `/confirm` 端点 resume。

---

## 3. supervisor 编排流程图（串行 / 并行 / barrier / gate）

```text
START
  ↓
[plan_node]  supervisor 决定换线步骤序列（①→②→③‖④→⑤）
  ↓
[FirstArticleAgent subgraph]   ① 首件触发（串行起点）
  ↓
[gate: FIRST_ARTICLE]  interrupt → 推首件触发卡 → 人确认 → resume
  ↓ PASS
[ProcessSwitchAgent subgraph]  ② 工艺版本切换确认
  ↓
[gate: PROCESS_SWITCH]  interrupt → 推工艺激活卡 → 人确认 → resume
  ↓ PASS
  ├─────────────────┬──────────────────┐
  ▼并行             ▼并行               │
[InspectionAgent]  [MaterialKittingAgent]│  ③ 点检/计量 ‖ ④ 物料齐套
  ↓                 ↓                   │
[gate:INSPECTION]  [gate:KITTING]       │  各自 interrupt → 人确认
  ↓ PASS            ↓ PASS              │
  └─────────────────┴──────────────────┘
  ↓
[barrier_node]  校验 gate_inspection==PASS 且 gate_kitting==PASS
  │  任一未过 → status=SUSPENDED，推异常卡，不推放行卡
  ↓ 都 PASS
[PassReleaseAgent subgraph]   ⑤ 首件放行门禁
  ↓
[gate: RELEASE]  interrupt → 推放行意图卡 → 人确认 → resume
  ↓ PASS
[done_node]  changeover_session.status=DONE
  ↓
END
```

**节点与边的约束**

| 项 | 取值 | 落地 |
|----|------|------|
| 并行派发 | `conditional_edges` 返回 `["inspection","kitting"]` | LangGraph 自动并发执行两条分支 |
| barrier 汇合 | `barrier_node` 是两分支边的共同终点 | LangGraph 等两分支都返回才执行；节点内再校验双 PASS |
| gate 中断 | `interrupt(value=action_card)` | state 落 MySQL，进程不阻塞；`/confirm` 端点 `Command(resume=token)` 续跑 |
| supervisor 步数上限 | `recursion_limit=40` | 含 gate 等待，超限抛 `GraphRecursionError` 标 FAILED |
| 整换线超时 | ≤1800s（含人确认等待） | `asyncio.wait_for` 包住图驱动 |
| 中断恢复 | `SqlSaver` checkpointer（`thread_id=session_id`） | 进程重启从断点续跑，gate 等待状态不丢 |
| 故障隔离 | sub-agent 连续失败 2 次 | 标 `SUSPENDED` 推异常卡，不自动重试到死 |
| barrier 防错 | ③ 或 ④ 未 PASS | 禁止推放行卡，直接挂起——编排层防错 |

---

## 4. confirmation gate 时序图（interrupt / resume）

```text
 supervisor          sub-agent          gate_node          UI/线长          /confirm端点      应用服务REST
     │                   │                  │                 │                  │                 │
     │── run subagent ──▶│                  │                 │                  │                 │
     │                   │── draft_* tool ─▶│                 │                  │                 │
     │                   │   (ActionCard)   │                 │                  │                 │
     │                   │                  │── interrupt ────┼──────────────────┼─────────────────┤
     │                   │                  │   (state落MySQL,│                  │                 │
     │                   │                  │    进程不阻塞)  │                  │                 │
     │                   │                  │── push 卡片 ───▶│                  │                 │
     │                   │                  │   (WebSocket+   │                  │                 │
     │                   │                  │    Kafka)       │                  │                 │
     │                   │                  │                 │── 人查看 evidence│                 │
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
- confirmation token 绑定 `action:target`（如 `process_route.activate:RR-100:v4`）+ 过期，防篡改、防重放；token 无效则 `gate_decision=REJECT`。
- 写落库发生在**最右侧应用服务**，不是 agent 进程——过聚合根不变式 + 事务发件箱，与 MES 正常写路径完全一致，只是触发源从"人点按钮"变成"Agent 草拟 + 人确认"。
- gate 决策（谁 / 何时 / 基于哪张卡 / token）落 `changeover_step_record`，全可审计。

---

## 5. 图驱动代码骨架（对应实现方案 §7.2、§7.3、§7.4）

### 5.1 supervisor 构建（并行 + barrier + gate）

```python
# app/orchestration/supervisor_graph.py
class SupervisorGraph:
    def __init__(self, subagents: dict[str, CompiledGraph],
                 gates: GateManager, changeover_repo: ChangeoverRepo) -> None:
        self._subagents = subagents
        self._gates = gates
        self._repo = changeover_repo

    def build(self) -> CompiledGraph:
        g = StateGraph(ChangeoverState)

        g.add_node("plan", self._plan_node)
        g.add_node("first_article", self._run_subagent("first_article"))
        g.add_node("gate_first_article", self._gate("FIRST_ARTICLE"))
        g.add_node("process_switch", self._run_subagent("process_switch"))
        g.add_node("gate_process_switch", self._gate("PROCESS_SWITCH"))
        g.add_node("inspection", self._run_subagent("inspection"))
        g.add_node("gate_inspection", self._gate("INSPECTION"))
        g.add_node("kitting", self._run_subagent("material_kitting"))
        g.add_node("gate_kitting", self._gate("KITTING"))
        g.add_node("barrier", self._barrier_node)
        g.add_node("release", self._run_subagent("pass_release"))
        g.add_node("gate_release", self._gate("RELEASE"))
        g.add_node("done", self._done_node)

        g.add_edge(START, "plan")
        g.add_edge("plan", "first_article")
        g.add_edge("first_article", "gate_first_article")
        g.add_edge("gate_first_article", "process_switch")
        g.add_edge("process_switch", "gate_process_switch")
        # 工艺确认后并行派发点检 + 物料（返回多目标 = 并行分支）
        g.add_conditional_edges(
            "gate_process_switch",
            lambda s: ["inspection", "kitting"],
        )
        g.add_edge("inspection", "gate_inspection")
        g.add_edge("kitting", "gate_kitting")
        g.add_edge("gate_inspection", "barrier")   # 两分支汇合 barrier
        g.add_edge("gate_kitting", "barrier")
        g.add_edge("barrier", "release")           # barrier 等双 PASS 才进 ⑤
        g.add_edge("release", "gate_release")
        g.add_edge("gate_release", "done")
        g.add_edge("done", END)
        return g.compile()

    async def _barrier_node(self, state: ChangeoverState) -> ChangeoverState:
        # 并行分支汇合：两个 gate 都 PASS 才放行，否则挂起不推放行卡
        if not (state["gate_inspection"] == "PASS"
                and state["gate_kitting"] == "PASS"):
            state["status"] = "SUSPENDED"
            await self._gates.push_exception_card(state, "点检或齐套未通过，禁止放行")
        return state

    def _gate(self, step: ChangeoverStep):
        async def fn(state: ChangeoverState) -> ChangeoverState:
            card = build_action_card(state, step)
            await self._repo.save_step(state["session_id"], step, "GATE_WAITING", card)
            # LangGraph interrupt：暂停，等外部 /confirm resume
            decision = await self._gates.await_confirmation(
                state["session_id"], step, card
            )
            state[f"gate_{step.lower()}"] = decision
            await self._repo.record_gate(state["session_id"], step, decision)
            return state
        return fn
```

### 5.2 gate 的 interrupt / resume

```python
# app/orchestration/gates.py
class GateManager:
    def __init__(self, dispatcher: ActionCardDispatcher,
                 confirmation_store: ConfirmationStore) -> None:
        self._dispatcher = dispatcher
        self._store = confirmation_store

    async def await_confirmation(self, session_id: str, step: ChangeoverStep,
                                 card: ActionCard) -> str:
        await self._dispatcher.push(card)                  # WebSocket + Kafka
        # interrupt：阻塞当前图执行，直到 Command(resume=...) 投入
        confirmation = await interrupt(value=card)
        if not confirmation.valid_for(card.writes_via_action()):
            return "REJECT"
        return "PASS" if confirmation.approved else "REJECT"
```

```python
# app/api/changeover_router.py
@router.post("/agent/changeover/{session_id}/confirm")
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

### 5.3 应用服务编排（异步驱动 + 超时兜底）

```python
# app/application/changeover_orchestrator.py
class ChangeoverOrchestrator:
    def __init__(self, supervisor: CompiledGraph,
                 session_manager: SessionManager) -> None:
        self._supervisor = supervisor
        self._sessions = session_manager

    async def start_changeover(self, req: ChangeoverRequest,
                               tenant: TenantContext) -> ChangeoverSession:
        session = await self._sessions.create(req, tenant)
        asyncio.create_task(self._drive(session, tenant))   # 异步驱动，gate 处 interrupt 暂停
        return session

    async def _drive(self, session: ChangeoverSession, tenant: TenantContext) -> None:
        try:
            await asyncio.wait_for(
                self._supervisor.ainvoke(
                    {
                        "session_id": session.id,
                        "tenant": tenant,
                        "work_order_id": session.work_order_id,
                        "target_route_id": session.target_route_id,
                        "target_route_version": session.target_route_version,
                    },
                    config={
                        "recursion_limit": 40,
                        "configurable": {"thread_id": session.id},
                    },
                ),
                timeout=1800.0,   # 含人确认等待的整体上限
            )
        except (GraphRecursionError, asyncio.TimeoutError) as e:
            await self._sessions.mark_failed(session, str(e))
```

- `asyncio.create_task` + `interrupt`：换线是长流程（含人工确认），HTTP 请求只负责"启动"，不阻塞等待全程。
- `recursion_limit=40` 与 `timeout=1800s` 形成"步数 + 时长"双闸门，超限标 FAILED 转人工，不硬走。
- `thread_id=session.id` 让 `SqlSaver` 按换线会话持久化与恢复，gate 等待状态可跨进程续跑。

---

## 6. 一句话定位

L3 用 LangGraph 的 **supervisor + subgraph** 做 multi-agent——supervisor 是只做 plan / dispatch / barrier / gate 的显式 `StateGraph`（不持任何工具），5 个 sub-agent 各自是带 `agent_role` 工具集的 ReAct 小循环；用 `conditional_edges` 并行派发点检 ‖ 物料、`barrier_node` 等双 PASS 才放行、`interrupt` / `Command(resume=...)` 实现 confirmation gate 人在回路，state 经 `SqlSaver` 落 MySQL 可跨进程续跑——编排是图驱动而非 prompt 自由发挥，写的闸门始终在人手里。
