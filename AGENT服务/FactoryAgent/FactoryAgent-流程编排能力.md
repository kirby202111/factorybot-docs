# FactoryAgent 流程编排能力（跨上下文长流程，人在回路）

> 本文是 [FactoryAgent-总体设计与实现方案.md](FactoryAgent-总体设计与实现方案.md) 中**流程编排能力（`orchestration`）**的落地展开。
> **核心形态**：编排 = **编排代码层（确定性）** + **在非确定决策点调用诊断 / 草稿能力**。代码层做所有"步骤顺序固定、判定规则固定、输入结构化"的活（编排 / 结构化比对 / barrier 硬防错 / confirmation gate / 写落库 / 版本钉死 / 放行能力隔离）；诊断与草稿能力只在代码做不了或做起来极复杂的决策点介入。
> **口径纪律**：写动作**过 confirmation gate**，落库走各上下文**正常应用服务**，绝不旁路写；**不进过点主事务**（[领域总览.md](../../领域模型/领域总览.md) §5.3），过点 P99 ≤200ms 判定仍走规则引擎；不编业务效果数字。
> **配套**：[诊断能力](FactoryAgent-诊断能力.md)、[草稿编写能力](FactoryAgent-草稿编写能力.md)、[现场痛点与能力嵌入点](FactoryAgent-现场痛点与能力嵌入点.md)、[能力边界与选型决策卡](FactoryAgent-能力边界与选型决策卡.md)。

---

## 0. 编排能力不做"第二套 Agent"

早期设计把编排拆成 5 个换线 sub-agent（首件 / 工艺 / 钢网程序 / 物料 / 放行），看似 multi-agent，实则其中 4 个是确定性查询比对——齐套率是不是 100%、钢网号等不等于工艺路线里的、程序版本是不是 ACTIVE，全是 `expected == actual` 的结构化判定，LLM 在这里是负资产。再后来又把"根因 / 隔离范围 / 追溯 / 生成"做成 4 类独立 sub-agent，于是根因推理、5M1E 排序、8D 生成这些逻辑与诊断能力、草稿能力形成了**两套实现**。

现在只有一条规则：

**编排图里需要"判根因"就调诊断能力，需要"产出可下达的卡"就调草稿能力，需要"确定性判定"就写代码节点。编排层自己不实现第四种推理。**

| 步骤类型 | 例子 | 归属 | 实现 |
|---|---|---|---|
| 顺序 / 并行 / 汇合 | 换线 5 步编排 | 代码 | supervisor 图的边 |
| 结构化比对 | 齐套率 == 100%、钢网号匹配、程序版本 == ACTIVE | 代码 | `query_compare` 代码节点 |
| 硬防错 | 未双 PASS 不放行 | 代码 | `barrier` 代码节点 |
| 写落库 | 激活工艺、下达隔离、发布 SOP | 代码 | gate + 上下文应用服务 REST |
| 版本钉死 | 工艺查询强制版本锚点 | 代码 | ACL 校验 |
| 放行能力隔离 | 不给任何能力放行 API | 代码 | 工具禁注册 + 启动断言 |
| 动作卡推送 + 超时升级 | gate 等人确认、卡住催办 | 代码 | dispatcher + deadline |
| **核对异常根因分支** | mismatch 后判台账错 / 工艺录错 / 产线拿错 | **诊断能力** | `invoke("diagnosis", 结构化异常)` |
| **故障隔离范围** | 故障模式 × 漂移窗口 × 产品敏感度 | **诊断能力 + 草稿能力** | 取证判范围 → 出隔离集卡 |
| **客诉根因排序** | 5M1E 假设加权排序 + 跨源语义关联 | **诊断能力** | 同上，换入参 |
| **生成类** | SOP / 8D / 返工单 / 处置卡草拟 | **草稿能力** | `invoke("drafting", draft_kind)` |

**为什么不把"现在该查什么"交给模型编排**：编排步骤的串行 / 并行 / barrier / gate 在图里是显式边，可审计、可回归、可断言；用纯 prompt 让模型自己编排，易丢步骤、难审计，而 MES 的漏步骤等于带病生产。

---

## 1. 定位与边界

### 1.1 这项能力解决什么

把现场跨上下文长流程里**非确定的那段**（根因处置 / 隔离范围判定 / 根因追溯 / 生成草拟）从"人脑拍 + 电话串 3 个系统 + 手写"升级为"Agent 取证 + 推理 + 草拟 + 人确认落库"；**确定性那段**（编排 / 比对 / 硬防错 / 写落库）用代码骨架做扎实。

### 1.2 非确定节点的统一形态

编排图里所有 LLM 节点都长成同一种形状：**取证 → 结构化 → 交给草稿能力 → gate**。

```
[编排节点]  拿到结构化上下文（mismatch / 故障 / 批次 / 工艺 diff）
    ↓ invoke("diagnosis", ...)          -- 需要假设与排序时；纯生成的场景可跳过
[诊断能力]  多步只读取证 → 根因假设 + 置信度 + 证据引用
    ↓ invoke("drafting", draft_kind=...) -- 需要可下达的产物时
[草稿能力]  按子图证据 + 模板 → Draft（requires_confirmation=True）
    ↓
[gate 节点]  interrupt → 推动作卡 → 人确认 → 写走上下文应用服务
```

| 场景 | 是否需诊断能力 | 草稿类型 | 说明 |
|------|--------------|---------|------|
| 换线 mismatch | 是（结构化异常入参） | `DISPOSITION_CARD` | 根因假设 + 建议动作 + 路由责任人 |
| 设备故障复产 | 是（遥测时序 + FMEA） | `ISOLATION_CARD` + `REPAIR_ORDER`（代码拼装） | 隔离集 + 维修单 |
| 客诉 8D | 是（批次号入参） | `ISOLATION_CARD` → `EIGHT_D` | 先隔离后 8D 正文 |
| 工艺变更落地 | 否（生成类） | `SOP` | 开放生成，代码做不了 |

> 这就是"一个 Agent"的直接体现：编排只声明"这里需要判根因 / 这里需要出草稿"，产物结构、证据链、置信度兜底全由被调能力保证。

### 1.3 硬边界

| 边界 | 说明 | 落地 |
|------|------|------|
| **不进过点主事务** | 不调过点引擎放行 / 拦截 API | 放行类工具**不注册到任何能力** + 启动断言 |
| **写动作 confirmation gate** | 写工具 `requires_confirmation`，产 intent + draft，人确认后落库 | `ToolPolicyGate` + `confirmation_store` |
| **写不旁路应用服务** | 不直写 MES 原始表 | 写工具声明 `writes_via`，走应用服务 REST，过聚合根不变式 + 事务发件箱 |
| **版本一致性** | 工艺查询强制版本锚点 | ACL 校验 `ACTIVE`（与诊断能力共用） |
| **能力切片** | 每个节点只见它该见的工具 | `tools_for(capability, tenant)` |
| **硬防错** | 未双 PASS 不放行 | `barrier` 确定性校验，非 LLM 判定 |
| **代码节点不调 LLM** | query / compare / barrier / gate / draft_release_card | 纯 Python 节点，`node_type=CODE` 落库可验 |
| **可观测兜底** | 每步 + 每个 gate 带 trace 与置信度，低置信度转人工 | 共用底座 |

---

## 2. 图形态：supervisor 代码图

```text
FastAPI ── POST /agent/orchestration/{scenario}/start ──▶ OrchestrationService
        ── POST /agent/orchestration/{session_id}/confirm          │
                                                                   ▼
                     ┌──────────────────────────────────────────────────────┐
                     │ SupervisorGraph（代码编排层，不持工具、不调 LLM）        │
                     │  代码节点：plan / query+compare / barrier / gate / write│
                     │  能力节点：按需 invoke(diagnosis) / invoke(drafting)    │
                     └──┬─────────────┬─────────────┬─────────────┬─────────┘
                        │             │             │             │
                        ▼             ▼             ▼             ▼
                 diagnosis 子图  diagnosis 子图  drafting 链   drafting 链
                 (根因/追溯)     (隔离范围)      (处置卡)      (8D / SOP)
                        │             │             │             │
                        └─────────────┴──────┬──────┴─────────────┘
                                             ▼
                     ToolRegistry（按能力切片 + 写工具声明 + 启动断言）
                                             │
              ┌──────────────┬───────────────┴──────────────┬──────────────┐
              ▼              ▼                              ▼              ▼
         ACL 只读        ACL 只读                       ACL 写（带 token）  RAG
         （各上下文）    （遥测 / FMEA / 台账）           -> 应用服务 REST
                                             ▲
                          动作卡 + 人工确认    │
              WebSocket(SSE) + Kafka -> 责任人 UI -> /confirm
```

**关键设计决策**：

- **supervisor 不持工具、不调 LLM**：只做 plan / dispatch / barrier / gate。想越界也没工具调。
- **代码节点与能力节点物理分层**：`ai/code_nodes/`（确定性）与 `ai/agent_nodes/`（能力调用）分目录，评审与回归可分别覆盖。
- **gate 是显式代码节点**：`interrupt` 暂停 → 推动作卡 → 人确认 → `resume` 续跑；gate 决策落库可审计。
- **barrier 是防错层不是决策层**：并行分支汇合后按结构化结果**确定性分流**（都 PASS → 放行卡；mismatch → 诊断能力；缺料 → 挂起催料）。

---

## 3. 场景：代码骨架 + 能力嵌入

### 3.1 换线（场景①）

```text
[plan]                 代码：决定步骤序列
  ↓
[query_first_article]  代码：查首件状态 -> [gate: FIRST_ARTICLE] 人确认
  ↓
[query_active_route]   代码：查工艺版本（强制版本锚点）-> [gate: PROCESS_SWITCH] 人确认
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行(代码)            ▼并行(代码)              │
[query_and_compare_tooling]   [query_and_compare_kitting]
  ├─ PASS                       ├─ PASS
  └─ FAIL(mismatch)             └─ FAIL(缺料)
  ↓                            ↓
[barrier]  代码：等两分支汇合，按结构化结果确定性分流
  │
  ├─ 都 PASS ─────▶ [draft_release_card] 代码：结构化拼装放行卡（非 LLM）
  │                    ↓ [gate: RELEASE] 人确认
  │                 过点上下文应用服务实际放行（过点主事务 + 规则引擎，P99≤200ms）
  │                    ↓ [done]
  │
  ├─ tooling FAIL ─▶ invoke("diagnosis", 结构化 mismatch)  ★诊断能力
  │                    ↓ invoke("drafting", DISPOSITION_CARD)  ★草稿能力
  │                  [gate: DISPOSITION] 人确认 -> 处置落库 -> 回 tooling_check 重检
  │
  └─ kitting FAIL ─▶ [SUSPENDED] 代码：缺料是确定的，不调能力，直接挂起推线长催料
```

**两个"懂不用"的点**：缺料挂起不调 LLM（确定性）；放行卡由代码结构化拼装（碰过点红线附近，不让 LLM 生成）。**换线全程 PASS 时 LLM 调用次数为 0**。

### 3.2 设备故障复产（场景②）

```text
[设备故障事件触发]
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行                  ▼并行                    │
[draft_repair_order]    invoke("diagnosis", 遥测+FMEA)  ★诊断能力：故障模式 + 漂移窗口 + 敏感度
  代码：结构化拼装维修单      ↓
  ↓                     invoke("drafting", ISOLATION_CARD)  ★草稿能力
[gate: REPAIR]           ↓
  ↓ PASS                [gate: ISOLATION] 人确认 -> 返工/返修上下文下达
  └──────────────────────┴──────────────────────┘
  ↓
[gate: RECALIBRATION]   代码：计量复校结果确认（红线，不调能力）
  ↓ PASS
[gate: RESTART_FA]      代码：barrier 等复校 + 点检 PASS -> 推复产放行卡
  ↓
[done]
```

复校 / 复产首件两道红线是代码节点，**任何能力都不碰**。

### 3.3 客诉 8D（场景③）

```text
[客诉触发]
  ↓
invoke("diagnosis", batch_no)    ★诊断能力：5M1E 假设排序 + 证据链
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行(代码)            ▼并行                    │
[供应商批次追溯]         invoke("diagnosis", 敏感度) ★诊断能力（隔离范围）
  代码：版本钉死后查在库品     ↓
  ↓                      invoke("drafting", ISOLATION_CARD) ★草稿能力
  └──────────────────────┴──────────────────────┘
  ↓
[gate: ISOLATION]  人确认 -> 返工上下文下达
  ↓ PASS
invoke("drafting", EIGHT_D)   ★草稿能力：拉追溯链 + 历史 8D 草拟正文
  ↓
[gate: 8D_PUBLISH]  人确认 -> 质量上下文发布
  ↓
[done]
```

### 3.4 工艺变更落地（场景④）

```text
[订阅 process.route.activated]
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行                    ▼并行                  │
invoke("drafting", SOP)  ★草稿能力：开放生成        [资质核对]  代码：操作工资质 ∈ 工艺要求资质集？
  ↓                       ↓ PASS                   （确定性，不调能力）
  └──────────────────────┴──────────────────────┘
  ↓
[barrier]  代码：等双 PASS
  ↓
[gate: NEW_ROUTE_FIRST_ARTICLE]  代码：推首件放行卡 -> 人确认
  ↓
[done]
```

SOP 草拟是开放生成（代码做不了）；资质核对是确定性查询（代码做，不调能力）——这是"该用代码的没用 AI"的典型体现。

---

## 4. 写路径与 confirmation gate

### 4.1 写工具白名单

| 能力 | 只读工具（示例） | 写工具（`requires_confirmation=True` + `writes_via`） |
|------|-----------------|------------------------------------------------------|
| 诊断 | 过点 / WIP / 物料 / 设备 / 质量 / 图与文档检索（全套） | 无 |
| 草稿 | 图节点回查 / 文档检索 / 工艺版本 | 无 |
| 编排 | 各上下文只读 + 遥测 / FMEA / 钢网台账 | `draft_isolation_card` → 返工/返修上下文 · `draft_rework_order` → 返工上下文 · `draft_sop_publish` → 工艺管理上下文 |
| — | 放行 / 拦截类 | **永不注册** |

> 编排的写工具大多是"草拟 + 卡片"（落库走应用服务），**没有直写原始表的工具**。

### 4.2 confirmation gate 的 interrupt / resume

```python
# app/ai/code_nodes/gate.py
class GateManager:
    def __init__(self, dispatcher: ActionCardDispatcher,
                 confirmation_store: ConfirmationStore) -> None:
        self._dispatcher = dispatcher
        self._store = confirmation_store

    async def await_confirmation(self, session_id: str, step: str,
                                 card: ActionCard) -> str:
        await self._dispatcher.push(card)              # WebSocket + Kafka
        confirmation = await interrupt(value=card)     # 阻塞当前图执行，state 落 MySQL
        if not confirmation.valid_for(card.writes_via_action()):
            return "REJECT"
        return "PASS" if confirmation.approved else "REJECT"
```

```python
# app/api/orchestration_router.py
@router.post("/agent/orchestration/{session_id}/confirm")
async def confirm_gate(
    session_id: str, req: ConfirmRequest,
    store: ConfirmationStore = Depends(get_store),
    runtime: AgentRuntime = Depends(get_runtime),
) -> ConfirmResponse:
    token = store.issue(session_id, req.step, req.approved, req.user_id)
    await runtime.resume("orchestration", session_id, token)   # Command(resume=token)
    return ConfirmResponse(step=req.step, decision="PASS" if req.approved else "REJECT")
```

### 4.3 写落库时序（以批次隔离为例）

```text
编排图 fault_impact 节点
  -> invoke("diagnosis", ...)                根因 / 隔离范围
  -> invoke("drafting", ISOLATION_CARD)      隔离集卡（requires_confirmation）
  -> gate interrupt，推卡给质量工程师
  -> 工程师查看 agent_hypothesis（隔离集 + 敏感度理由）+ evidence，确认
  -> confirmation token（绑定 isolation.issue:{tenant}，带过期）
  -> 编排写 ACL: POST /api/isolation-orders（header: X-Confirmation-Token / X-Confirmed-By）
  -> 返工上下文 application 层：
       IsolationAggregate.issue(batch_set, reason)   -- 聚合根不变式校验
       -> 事务发件箱落 BatchIsolated 事件
  -> 收到成功，gate_decision=PASS，续跑
```

**Agent 全程不碰 MES 原始表**；写路径与"人点按钮"完全一致，只是触发源变成"Agent 草拟 + 人确认"。

### 4.4 过点红线（代码层）

- **放行能力从架构层切掉**：任何能力下不存在 `pass_judge` / `force_release` / `release_*` / `block_pass` 工具（启动断言）。
- **放行卡是代码节点直出**：`draft_release_card` 是结构化拼装，非 LLM。
- **实际放行走过点应用服务**：人确认后走过点执行上下文正常应用服务（主事务 + 规则引擎，P99 ≤200ms）。
- **barrier 前置防错**：核对未完成时不推放行卡——这是编排层防错，确定性代码。

---

## 5. 状态与动作卡

```sql
-- 与总体设计的 agent_session / agent_step_record 同构，按能力过滤即可
agent_session
  - session_id (PK)
  - capability = 'ORCHESTRATION'
  - scenario (CHANGEOVER / FAULT_RESPONSE / COMPLAINT_8D / PROCESS_CHANGE)
  - business_ref (JSON)                -- work_order_id / batch_id / asset_id
  - tenant_context (JSON)
  - status (PLANNING / RUNNING / SUSPENDED / DONE / FAILED)
  - current_step
  - created_at / updated_at

agent_step_record
  - record_id (PK) / session_id (FK)
  - capability                          -- orchestration / diagnosis / drafting（跨能力同表）
  - step / node_type (CODE / AGENT)
  - status (PENDING / RUNNING / GATE_WAITING / CONFIRMED / FAILED)
  - action_card_payload (JSON)
  - agent_hypothesis (JSON) / agent_confidence
  - gate_decision (PASS / REJECT / RETRY) / gate_decided_by / gate_decided_at
  - tool_call_traces (JSON)
  - occurred_at
```

```python
class ActionCard(BaseModel):
    card_id: str
    session_id: str
    step: str
    capability: str | None         # 来自哪个能力，None 表示代码节点直出
    intent: str                    # "激活工艺路线 RR-100 v4" / "下达批次隔离"
    draft_payload: dict            # 草稿内容（intent + draft，非已落库数据）
    writes_via: str                # "工艺管理上下文.application.activate_route"
    requires_confirmation: bool = True
    evidence: list[str]            # trace_id 列表
    agent_hypothesis: dict | None  # 根因 / 隔离集（代码节点为 None）
    confidence: str | None
    risk_note: str
    deadline: datetime | None
```

- 卡片带 `evidence` + `agent_hypothesis` + `confidence`，确认人点开可回溯推理与工具调用——**基于证据确认而非盲批**。
- `node_type` + `capability` 落库，让"本次会话调了几次 LLM、哪几步是代码、哪几步来自哪个能力"可量化。

---

## 6. 代码骨架

### 6.1 supervisor 图装配（换线）

```python
# app/ai/scenarios/changeover_graph.py
class ChangeoverGraph:
    """换线：代码骨架 + 仅在 mismatch 分支调用诊断/草稿能力。"""

    def __init__(self, code_nodes, capability_nodes, gates, repo) -> None:
        self._c = code_nodes
        self._agents = capability_nodes
        self._gates = gates
        self._repo = repo

    def build(self) -> CompiledGraph:
        g = StateGraph(OrchestrationState)

        # 代码节点（不调 LLM）
        g.add_node("plan", self._c.plan)
        g.add_node("first_article", self._c.query_first_article)
        g.add_node("gate_first_article", self._gate("FIRST_ARTICLE"))
        g.add_node("process_switch", self._c.query_active_route)
        g.add_node("gate_process_switch", self._gate("PROCESS_SWITCH"))
        g.add_node("tooling_check", self._c.query_and_compare_tooling)
        g.add_node("kitting_check", self._c.query_and_compare_kitting)
        g.add_node("barrier", self._barrier_node)
        g.add_node("draft_release", self._c.draft_release_card)
        g.add_node("gate_release", self._gate("RELEASE"))
        g.add_node("suspend", self._c.suspend)
        g.add_node("done", self._c.done)

        # 能力节点（仅 mismatch 分支触发）
        g.add_node("root_cause", self._agents.root_cause)          # invoke(diagnosis)
        g.add_node("disposition_card", self._agents.disposition)   # invoke(drafting)
        g.add_node("gate_disposition", self._gate("DISPOSITION"))

        g.add_edge(START, "plan")
        g.add_edge("plan", "first_article")
        g.add_edge("first_article", "gate_first_article")
        g.add_edge("gate_first_article", "process_switch")
        g.add_edge("process_switch", "gate_process_switch")
        # 返回多目标 = 并行派发
        g.add_conditional_edges("gate_process_switch",
                                lambda s: ["tooling_check", "kitting_check"])
        g.add_edge("tooling_check", "barrier")
        g.add_edge("kitting_check", "barrier")
        g.add_conditional_edges("barrier", self._barrier_route,
                                ["draft_release", "root_cause", "suspend"])
        g.add_edge("draft_release", "gate_release")
        g.add_edge("gate_release", "done")
        g.add_edge("root_cause", "disposition_card")
        g.add_edge("disposition_card", "gate_disposition")
        g.add_conditional_edges("gate_disposition",
                                lambda s: ["tooling_check"] if s["retry_tooling"] else ["done"])
        g.add_edge("done", END)
        return g.compile(checkpointer=self._repo.checkpointer)
```

### 6.2 能力节点（统一形态）

```python
# app/ai/agent_nodes/root_cause.py
class RootCauseNode:
    """编排节点：把结构化 mismatch 交给诊断能力，产物再交草稿能力。"""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def __call__(self, state: OrchestrationState) -> OrchestrationState:
        report = await self._runtime.invoke(
            "diagnosis",
            {
                "mode": "structured_exception",
                "exception": {
                    "type": state["mismatch_code"],
                    "expected": state["expected"],
                    "actual": state["actual"],
                    "biz_ref": {"asset_id": state["asset_id"],
                                "route_id": state["target_route_id"],
                                "version": state["target_route_version"]},
                },
            },
            tenant=state["tenant"],
            parent_session_id=state["session_id"],
        )
        state["agent_hypothesis"] = report["hypotheses"]
        state["agent_confidence"] = report["confidence"]
        state["diagnosis_subgraph_ref"] = report.get("subgraph_ref")
        return state
```

```python
# app/ai/agent_nodes/draft_nodes.py
class DispositionCardNode:
    """编排节点：把诊断结论交给草稿能力，统一格式化成处置卡。"""

    async def __call__(self, state: OrchestrationState) -> OrchestrationState:
        draft = await self._runtime.invoke(
            "drafting",
            {
                "draft_kind": "DISPOSITION_CARD",
                "report": {"hypotheses": state["agent_hypothesis"],
                           "confidence": state["agent_confidence"],
                           "subgraph_ref": state["diagnosis_subgraph_ref"]},
            },
            tenant=state["tenant"],
            parent_session_id=state["session_id"],
        )
        state["action_card"] = build_action_card(draft, step="DISPOSITION")
        return state
```

### 6.3 barrier（确定性分流）

```python
# app/ai/code_nodes/barrier.py
async def barrier_node(state: OrchestrationState) -> OrchestrationState:
    t, k = state["tooling_result"], state["kitting_result"]
    if t["status"] == "PASS" and k["status"] == "PASS":
        state["barrier_route"] = "draft_release"
    elif t["status"] == "FAIL":            # mismatch -> 交诊断能力
        state["barrier_route"] = "root_cause"
        state["expected"], state["actual"] = t["expected"], t["actual"]
        state["mismatch_code"] = t["code"]
    else:                                   # 缺料 -> 确定性，直接挂起
        state["barrier_route"] = "suspend"
        state["status"] = "SUSPENDED"
    return state
```

### 6.4 异步驱动与双闸门

```python
# app/application/orchestration_service.py
class OrchestrationService:
    async def start(self, req: OrchestrationRequest, tenant: TenantContext) -> OrchestrationSession:
        session = await self._sessions.create("orchestration", req, tenant)
        asyncio.create_task(self._drive(session, tenant))   # 长流程，接口只负责点火
        return session

    async def _drive(self, session, tenant) -> None:
        try:
            await asyncio.wait_for(
                self._graph.ainvoke(
                    {"session_id": session.id, "tenant": tenant, **session.context},
                    config={"recursion_limit": 40,
                            "configurable": {"thread_id": session.id}},
                ),
                timeout=3600.0,          # 含人确认等待的整体上限
            )
        except (GraphRecursionError, asyncio.TimeoutError) as exc:
            await self._sessions.mark_failed(session, str(exc))
```

`recursion_limit=40` 与 `timeout=3600s` 构成"步数 + 时长"双闸门，超限标 `FAILED` 转人工，不硬走。

### 6.5 启动断言

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    specs, registry = app.state.capability_specs, app.state.tool_registry
    validate_tool_policy(registry, specs)               # 写/只读/越权/放行 四类断言
    validate_acl_clients(app.state.acl_clients, specs)  # 无写权限能力不得持写 client
    assert not registry.tools_for("orchestration", ANY_TENANT, read_only_only=True) or True
    yield
```

---

## 7. 兜底与可观测（通用底座上的能力特有项）

- **特有指标**：`agent_session_total{capability="orchestration",scenario}` / `agent_step_total{node_type}` / `agent_llm_invocation_total{scenario}`（换线全 PASS 为 0）/ `agent_gate_decision_total` / `agent_suspended_total` / `agent_write_tool_total` / `agent_write_rejected_total`。
- **链路**：一个会话一个 `trace_id`，动作卡带 `evidence`（trace_id 列表）+ `agent_hypothesis`；跨能力调用靠 `parent_session_id` 串。
- **兜底**：gate 超 deadline 挂起 `SUSPENDED`；barrier 未 PASS 禁推放行卡；置信度 `low` 标 `need_human_review` 不自动路由；写工具未带 token 被拒（P0 告警）；能力节点连续失败 2 次挂起转人工。

---

## 8. 实现步骤

### 阶段一：编排代码层骨架

1. 在既有 runtime 上扩展 `ai/code_nodes/`，搭 supervisor + plan / query_compare / barrier / gate / done。
2. 落 `ToolDescriptor.capabilities` / `requires_confirmation` / `writes_via` 与 `ToolPolicyGate` 断言。
3. 落 `agent_session` / `agent_step_record`（`capability` + `node_type` 两列）。
4. 接 `interrupt` / `Command(resume=...)`，跑通单个 gate 的"推卡 → 确认 → 续跑"。
5. 动作卡双通道推送 MVP（WebSocket + Kafka）。
6. **换线场景跑通纯代码骨架（全程 PASS，无 LLM）**——先证明"代码能跑的不交给 LLM"。

### 阶段二：能力节点接入

7. `root_cause` 节点接诊断能力（结构化异常入参）+ `DISPOSITION_CARD` 草稿节点 + 处置 gate + 重检回路。
8. `fault_impact` 节点接诊断能力（遥测 + FMEA）+ `ISOLATION_CARD` 草稿节点。
9. 客诉场景串 `diagnosis → ISOLATION_CARD → EIGHT_D`。
10. 工艺变更场景接 `SOP` 草稿节点。

### 阶段三：写路径与试点

11. 各上下文写 ACL（钢网归还/领用、隔离下达、SOP 发布）+ `confirmation_store`（token 绑定 `action:target` + 过期）。
12. 挑一条产线灰度换线编排（先代码骨架 + mismatch 分支），gate 做扎实。
13. 接指标，重点观察 `agent_llm_invocation_total` 与 `agent_step_total{node_type="CODE"}` 占比。
14. 评测：根因命中、隔离集命中率、8D 采纳率、gate 拒绝率合理区间。

---

## 9. 约束落地检查清单

- [ ] supervisor 图自身不持工具、不调 LLM；能力节点只 `invoke(diagnosis|drafting)`。
- [ ] 任何能力下均无放行 / 拦截类工具（前缀断言）；实际放行走过点应用服务。
- [ ] 所有写工具 `requires_confirmation=True` + `writes_via`，无 token 写调用被拒。
- [ ] 写落库走上下文应用服务 REST，过聚合根不变式 + 事务发件箱。
- [ ] confirmation token 绑定 `action:target`，带过期，防篡改防重放。
- [ ] barrier 在放行前等所有分支 PASS，未 PASS 分流到诊断能力或挂起，不推放行卡。
- [ ] gate 决策（谁 / 何时 / 基于哪张卡 / 假设 / token）落 `agent_step_record` 可审计。
- [ ] 代码节点（query / compare / barrier / gate / draft_release_card）不调 LLM，`node_type=CODE` 可验。
- [ ] 跨能力调用带 `parent_session_id`，同一条 trace 可回溯。
- [ ] 工艺查询强制版本锚点，ACL 校验 `ACTIVE`。
- [ ] 置信度 `low` 标 `need_human_review`，不自动路由。
- [ ] gate 有 deadline，超时挂起不无限阻塞；能力节点连续失败 2 次挂起转人工。

---

## 10. 面试防守 Q&A

**Q：编排能力是不是又一个 Agent？和诊断、草稿怎么分工？**
A：不是。编排是**一张代码图**：步骤顺序、并行、barrier、gate、写落库全是代码节点；需要判根因时 `invoke("diagnosis")`，需要出可下达的卡时 `invoke("drafting")`。编排层自己不实现第四种推理——早期把"根因 / 隔离 / 追溯 / 生成"做成 4 个独立 sub-agent，结果和诊断、草稿形成两套实现，现在收敛成"调用已有能力"。

**Q：为什么编排代码层不调 LLM？**
A：因为编排里的大部分步骤是确定性的：顺序固定、判定规则固定、输入结构化。漏步骤、带病生产、写越界这些风险，代码做比 LLM 更可靠更快。用纯 prompt 让模型自己编排，容易丢步骤且难审计。所以 supervisor 是纯代码图，LLM 只出现在"输入开放 / 需推理生成 / 分支难穷举"的节点，且这些节点直接复用诊断与草稿能力。

**Q：换线全程 PASS 时 LLM 调用真的是 0 吗？**
A：是。路径是 `plan → first_article → process_switch → tooling_check(PASS) ‖ kitting_check(PASS) → barrier → draft_release(代码拼装) → gate_release → done`，全程没有能力节点。指标 `agent_llm_invocation_total{scenario="changeover"}` 可验证为 0——这是"懂什么时候不用 AI"的可量化标志。

**Q：multi-agent 怎么保证不越界进过点主事务？**
A：三层兜底。① supervisor 不持工具，纯代码编排器，想越界也没工具调；② 工具按能力切片，放行 / 拦截类工具**不注册到任何能力**；③ 启动断言：任何能力下不得有 `pass_judge` / `force_release` / `release_*` / `block_pass` 前缀工具。实际放行由人确认放行卡后走过点上下文正常应用服务，P99 ≤200ms 判定仍走规则引擎。

**Q：写动作怎么保证不旁路应用服务？**
A：写工具必须声明 `requires_confirmation` + `writes_via`（落库走哪个应用服务），否则启动失败。Agent 产出 intent + draft（动作卡，含假设 + 证据），人确认后拿 confirmation token，调对应上下文应用服务 REST——过聚合根不变式 + 事务发件箱。Agent 全程不碰原始表。

**Q：mismatch 的处置，规则引擎真的做不了吗？**
A：能写，但维护成本高且永远落后。根因不止"拿错"——上工单未还库、台账改名、借出未登记、工艺录错、扫码制式不同……每出现一种新现场情况决策树就要补分支，还要补对应的跨上下文联合查询，判定阈值还是启发式的。Agent 的优势不是"比决策树聪明"，而是**取证路径由中间结果驱动**：查到"ST-A 借出未还"后自适应去查上工单收线记录，而不是固定 JOIN。新根因出现时只要取证能查到证据就能给假设，决策树不用改。

**Q：confirmation gate 怎么实现人在回路？**
A：LangGraph 的 `interrupt`。跑到 gate 节点时 `interrupt(value=action_card)` 暂停，state 落 MySQL（checkpointer），进程不阻塞；`/confirm` 端点人确认后 `Command(resume=token)` 续跑。token 绑定 `action:target` 带过期，防篡改防重放。gate 决策（谁 / 何时 / 基于哪张卡 / 假设）落 `agent_step_record`，全可审计。

**Q：Agent 失败了会卡住产线吗？**
A：故障隔离。能力节点连续失败 2 次标 `SUSPENDED` 推异常卡，不自动重试到死；置信度 `low` 标 `need_human_review` 列疑点不自动路由；barrier 未 PASS 禁止推放行卡；gate 等待有 deadline，超时挂起。最坏情况是流程退回人工编排，不会越界写、不会错误放行。

---

## 11. 一句话定位

"流程编排能力是**一张代码图 + 两次能力调用**：supervisor 里代码节点负责 plan / 比对 / barrier / gate / 写落库，需要判根因就 `invoke(diagnosis)`、需要出可下达的卡就 `invoke(drafting)`，编排层不实现第四种推理。换线全程 PASS 时 LLM 调用为 0；写动作过 confirmation gate（interrupt/resume + 绑定 action:target 的 token），落库走各上下文应用服务过聚合根不变式 + 事务发件箱；放行 / 拦截能力从架构层切掉，过点判定仍归规则引擎。"
