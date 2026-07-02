# L3 编排型 Agent 实现方案（Python + LangGraph Multi-Agent）

> 本文是 [AGENT服务引入路线.md](../AGENT服务引入路线.md) §2.4 L3 编排型 Agent 的落地展开，输出**技术栈、multi-agent 架构、实现方案、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + LangGraph + Pydantic），与 [L1诊断型Agent-实现方案.md](../L1诊断型Agent/L1诊断型Agent-实现方案.md) 同栈，复用其 ACL / 工具注册 / 可观测底座。
> **核心形态**：**multi-agent**——一个编排 supervisor + 若干专职 sub-agent，每个 sub-agent 对齐一个限界上下文。**上下文边界同时是 agent 边界和工具边界**，权限在 agent 层就隔离掉。
> **口径纪律**：L3 是"跨上下文编排 + 人在回路"，写动作**过 confirmation gate**，落库走各上下文的**正常应用服务**，绝不旁路写；编排 Agent **不进过点主事务**（[领域总览.md](../../领域模型/领域总览.md) §5.3），过点 P99 ≤200ms 的判定仍走规则引擎。

---

## 0. 为什么 L3 用 multi-agent（选型判断）

L3 换线流程跨**首件处理 / 工艺管理 / 点检保养 / 计量检定 / 物料 / WIP / 过点执行**多个上下文，且满足 multi-agent 的三个吃满条件：

1. **可并行**：点检 / 计量校验 ‖ 物料齐套核对 无依赖，单 agent 串行要两倍时间；multi-agent 下两个 sub-agent 并发，supervisor 只 barrier 在放行门禁。换线是停线动作，省分钟就是省钱。
2. **可分权**：每个上下文的 confirmation gate 不同、写工具白名单不同。把放行 / 拦截类能力**只挂在过点 sub-agent**，其他 sub-agent 根本没有该工具——能力从架构层切掉，而不是靠 prompt 约束。
3. **可隔离故障**：点检 sub-agent 发现计量超差，可单独挂起转人工，不污染整条换线链；supervisor 收到失败信号决定回退或等修。

L1 诊断虽也跨上下文，但推理链**强串行依赖**（必须先拿 `routeVersion` 才能查工艺），单 agent + ReAct 更合适，**不必硬上 multi-agent**。这是按"并行度 / 分权需求 / 故障隔离"做的选型，不是为多而多。

---

## 1. 设计目标与边界

### 1.1 目标

把"换线"这个跨上下文流程从"线长凭经验串 5 个界面"升级成**Agent 编排 + 人在回路**：supervisor 推进步骤，每步给操作工 / 线长一张**动作卡**，需要写 / 放行的步骤走正常 MES 流程由人确认，Agent 只做"下一步该干啥 + 卡片推送 + 异常提醒"。

换线标准流程（5 步，对应 5 个 sub-agent）：

| 步骤 | sub-agent | 上下文 | 可并行？ | confirmation gate |
|------|-----------|--------|---------|-------------------|
| ① 首件触发 | FirstArticleAgent | 首件处理 | 前置（串行起点） | 触发首件工单，人确认 |
| ② 工艺版本切换确认 | ProcessSwitchAgent | 工艺管理 | ② 之后 ③④ 可并行 | 新工艺版本激活确认 |
| ③ 点检 / 计量校验 | InspectionAgent | 点检保养 + 计量检定 | ③ ‖ ④ | 点检结果 + 计量合格确认 |
| ④ 物料齐套核对 | MaterialKittingAgent | 物料 + WIP | ③ ‖ ④ | 齐套率达标确认 |
| ⑤ 首件放行门禁 | PassReleaseAgent | 过点执行 | barrier（③④ 都过才放行） | 首件放行卡，人下达 |

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **不进过点主事务** | supervisor / 其他 sub-agent 不调过点引擎放行 / 拦截 API | 放行类工具**只注册在 PassReleaseAgent**，且仅生成"放行意图卡"，实际放行走过点上下文应用服务 |
| **写动作 confirmation gate** | 所有写工具标记 `requires_confirmation`，生成 intent + draft，人确认后才落库 | `WriteToolGate` 拦截：未带人确认 token 的写工具调用直接拒绝 |
| **写不旁路应用服务** | Agent 不直写 MES 原始表，落库走各上下文正常应用服务 | 写工具 handler 调的是上下文 REST 的"应用服务接口"，过聚合根不变式 + 事务发件箱（§5.4） |
| **版本一致性** | 工艺切换必须校验新版本 `ACTIVE` 且与过点记录 `routeVersion` 对齐 | ProcessSwitchAgent ACL 强制 `route_version` 校验（继承 L1 §4.3） |
| **权限隔离** | sub-agent 只能见本上下文 toolset | `tools_for(agent_role)` 按 agent 角色裁剪工具，越界工具不注册 |
| **可观测兜底** | 每步推理 + 每个 gate 带 trace + 置信度，低置信度转人工 | 复用 L1 的 `tool_call_trace` + OTel |

### 1.3 与 L1 的复用关系

- **同栈**：Python + FastAPI + LangGraph + Pydantic，同包结构（§6）。
- **复用底座**：ACL 客户端、`ToolDescriptor` / `ToolRegistry`、`tool_call_trace`、OTel / prometheus 指标、`TenantContext`。
- **新增**：multi-agent 编排（LangGraph supervisor + subgraph）、写工具白名单与 `WriteToolGate`、confirmation gate 状态机、动作卡推送。

### 1.4 典型场景

L3 适用于"跨 ≥3 个限界上下文、有 ≥1 处无依赖可并行、有 ≥1 个写红线动作需人确认"的现场流程。三者都满足才上 multi-agent；只满足"跨上下文推理"用 L1，只满足"草拟单据"用 L2——按价值/风险/依赖分层，不是逢流程就上 multi-agent。

| # | 场景 | 跨上下文 | 并行点 | confirmation gate |
|---|------|---------|--------|-------------------|
| ① | **换线**（主场景，§1.1 已详述） | 首件→工艺→点检/计量‖物料/WIP→过点 | 点检 ‖ 物料齐套 | 工艺版本激活、首件放行（仅意图卡） |
| ② | **异常批次隔离与返工处置** | 过点锁定→质量追溯(L1)→返工工单→返工工艺→物料→过点再入点 | 返工工艺草拟 ‖ 返工物料齐套 | 批次隔离锁、返工单下达、返工再入点放行 |
| ③ | **设备故障响应与复产** | 设备数据→设备台账→维修工单→计量复校→点检→过点复产 | 维修单草拟 ‖ 故障影响批次排查 | 维修单下达、计量复校确认、复产首件放行 |
| ④ | **工艺变更落地** | 工艺版本→SOP 草拟(RAG B)→人员资质→首件验证→过点放行 | 新 SOP 草拟 ‖ 操作工资质核对 | 新工艺激活、SOP 发布、首件验证放行 |
| ⑤ | **客诉追溯与 8D 闭环** | 过点/WIP 逆向追溯→质量追溯(L1)→物料(供应商批次)→返修处置→8D 报告 | 供应商批次追溯 ‖ 同批次在库品隔离 | 在库品隔离/召回、8D 发布、纠正措施下达 |

**场景②③④⑤ 与主场景①的差异**：

- **② 异常批次返工**：把 L1 诊断作为子步骤嵌入编排——L3 调 L1 给根因，再草拟返工单 + 工艺 + 物料，三处写均需 gate。误发返工单可能批量报废，是除放行外最敏感的写红线。
- **③ 设备故障复产**：长流程"停线—维修—复校—复产"，每步写动作有风险；故障期间相关批次是否需隔离要与维修并行判断。
- **④ 工艺变更落地**：典型防错场景——版本切换 + SOP + 人员资质 + 首件验证缺一不可，否则未培训操作工可能按新工艺生产。订阅 `ProcessRouteActivated` 事件触发。
- **⑤ 客诉 8D**：跨"追溯 + 隔离 + 报告 + 纠正"四类动作，隔离/召回决策与 8D 发布需人确认；追溯复用 L1，隔离编排 L2 做不了。

**通用形态**：这 5 个场景都可套用同一套 supervisor 编排骨架（§5.1）——只是 sub-agent 组合、并行点、gate 列表不同。新增场景 = 配置 sub-agent 集合 + 步骤图，不动框架——这是 multi-agent 编排的扩展性收益。

---

## 2. 技术栈

### 2.1 选型总览（仅列与 L1 差异项）

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| Agent 编排 | **LangGraph（supervisor + subgraph）** | supervisor 用 `StateGraph` 编排 sub-agent，每个 sub-agent 是独立 subgraph；天然支持并发节点（`add_node` + 并行边）、barrier、条件路由 |
| 并发原语 | LangGraph `Send` + asyncio.gather | ③ ‖ ④ 用并行分支实现，supervisor 在 barrier 节点等待两者都完成 |
| 状态机 | LangGraph `StateGraph` + Pydantic `ChangeoverState` | 换线状态（各步骤 status / gate 决策 / 动作卡）外置 MySQL，可中断恢复 |
| 写工具闸门 | `WriteToolGate`（新增） | 区别于 L1 的 `ReadOnlyToolGate`：允许注册写工具，但强制 `requires_confirmation` + 人确认 token |
| 动作卡推送 | WebSocket（SSE）+ Kafka 动作卡事件 | 操作工 / 线长 UI 实时收卡片，确认动作回写 |
| 持久化 | SQLAlchemy 2.0 (async) + LangGraph `SqlSaver` | 换线会话 + 子 agent 状态 + gate 决策记录 |

### 2.2 为什么用 LangGraph supervisor 模式

- **显式图 > 隐式 prompt**：换线 5 步的串行 / 并行 / barrier 关系在 `StateGraph` 里是显式边，可对每条边加条件（"③④ 都 PASS 才进 ⑤"）。用纯 prompt 让一个模型自己编排 5 步，易丢步骤、难审计。
- **subgraph = sub-agent**：每个 sub-agent 是独立 `StateGraph`，有自己的工具集、system prompt、recursion_limit，互不污染上下文——这是 multi-agent 的本质。
- **可中断**：LangGraph 支持 `interrupt`，confirmation gate 处暂停等人工确认，确认后 `Command(resume=...)` 续跑——对应"人在回路"。

### 2.3 为什么不用 AutoGen / CrewAI

- AutoGen 的 conversational 模式适合"多 agent 互相对话"，但换线是**有严格步骤顺序的编排**，不是对话，supervisor + subgraph 更贴。
- CrewAI 抽象偏高，难做细粒度权限拦截与 gate 状态机；LangGraph 离原语更近，可控性更强，与 L1 同构。

---

## 3. 总体架构

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ agent-service（Python + FastAPI + LangGraph）                            │
│                                                                           │
│  FastAPI ── POST /agent/changeover ──▶ ChangeoverOrchestrator            │
│                                          │ 构建 supervisor StateGraph     │
│                                          ▼                                 │
│              ┌───────────────────────────────────────────────────────┐   │
│              │  SupervisorGraph（编排）                                  │   │
│              │  plan_node → dispatch(并行/串行) → barrier → gate_node  │   │
│              └──┬──────────┬──────────┬──────────┬──────────┬─────────┘   │
│                 │ subgraph │ subgraph │ subgraph │ subgraph │ subgraph    │
│      ┌──────────▼┐ ┌───────▼────────┐ ┌▼────────┐ ┌▼────────┐ ┌▼──────┐  │
│      │FirstArticle│ │ProcessSwitch   │ │Inspection│ │Material │ │PassRel│  │
│      │Agent       │ │Agent           │ │Agent     │ │Kitting  │ │Agent  │  │
│      │(首件)      │ │(工艺版本)      │ │(点检/计量)│ │(物料/WIP)│ │(过点) │  │
│      └─────┬──────┘ └───────┬────────┘ └────┬────┘ └────┬────┘ └───┬──┘  │
│            │ toolset         │ toolset        │ toolset   │ toolset  │     │
│            ▼                 ▼                ▼           ▼          ▼     │
│      ┌──────────────────────────────────────────────────────────────┐    │
│      │  ToolRegistry（按 agent 角色裁剪 + WriteToolGate）            │    │
│      │  只读 toolset 通用 │ 写 toolset 白名单（requires_confirmation）│    │
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
                                                          ▲
                              动作卡推送 + 人工确认        │
                      ┌──────────────────────────────────┴──┐
                      │  WebSocket(SSE) + Kafka 动作卡事件     │
                      │  → 操作工/线长 UI                       │
                      └──────────────────────────────────────┘
```

### 3.1 关键设计决策

- **supervisor 不持写工具**：supervisor 只做"下一步该干啥 + 派发 + 收口"，**没有任何写工具**。写工具只挂在对应 sub-agent。这是"能力从架构层切掉"的核心——supervisor 想越界也调不到。
- **sub-agent 工具集互斥**：每个 sub-agent 注册时声明 `agent_role`，`ToolRegistry.tools_for(role)` 只返回该角色的工具。工艺 sub-agent 看不到过点放行工具。
- **gate 是显式节点**：每个 confirmation gate 是 supervisor 图里的一个节点，`interrupt` 暂停 → 推动作卡 → 等人确认 → `resume` 续跑。gate 决策落库可审计。
- **barrier 在放行门禁**：③ ④ 并发执行，supervisor 在 ⑤ 之前 barrier 等两者都 PASS。

---

## 4. 工具注册：按 agent 角色裁剪 + 写工具白名单

### 4.1 工具与 sub-agent 映射

| sub-agent (role) | 只读工具 | 写工具（`requires_confirmation=True`） |
|------------------|---------|----------------------------------------|
| `first_article` | `query_fa_status` / `query_work_order` | `draft_first_article_order`（草拟首件工单，人确认下达） |
| `process_switch` | `query_process_route(route_id, route_version)` / `query_active_route` | `draft_route_activation`（草拟新版本激活，人确认） |
| `inspection` | `query_checklist_status` / `query_calibration_status` | `draft_checklist_record`（草拟点检记录，人确认） |
| `material_kitting` | `query_kit_status` / `query_wip_position` / `query_bom_version` | 无（齐套只读核对，写走工单上下文） |
| `pass_release` | `query_pass_records` / `query_test_results` | `draft_pass_release_card`（**仅生成放行意图卡**，不调放行 API） |
| `supervisor` | 无 | 无 |

### 4.2 工具元数据（扩展 L1）

```python
class ToolDescriptor(BaseModel):
    name: str
    description: str
    bounded_context: str
    agent_role: str                  # 新增：归属哪个 sub-agent
    read_only: bool
    requires_confirmation: bool = False   # 写工具必须 True
    writes_via: str | None = None        # 新增：写落库走哪个上下文应用服务
    args_schema: type[BaseModel]
    required_tenant_scopes: list[str]
```

### 4.3 WriteToolGate

```python
class WriteToolGate(Exception):
    """写工具未声明 requires_confirmation 或未声明 writes_via，拒绝注册。"""

class ToolRegistry:
    def register(self, d: ToolDescriptor) -> None:
        if not d.read_only:
            if not d.requires_confirmation:
                raise WriteToolGate(f"写工具必须 requires_confirmation: {d.name}")
            if not d.writes_via:
                raise WriteToolGate(f"写工具必须声明 writes_via（落库走哪个应用服务）: {d.name}")
        self._descriptors[d.name] = d

    def tools_for(self, role: str, tenant: TenantContext) -> list[ToolDescriptor]:
        return [
            d for d in self._descriptors.values()
            if d.agent_role == role and tenant.can_access(d.required_tenant_scopes)
        ]
```

- 写工具**必须**声明 `requires_confirmation` 与 `writes_via`，否则启动断言失败——红线靠代码兜底。
- supervisor 的 `agent_role="supervisor"` 工具集为空，启动断言校验。

---

## 5. 实现方案

### 5.1 编排状态机（LangGraph supervisor）

```text
[plan_node]  supervisor 决定下一步（首件 / 工艺 / 并行点检+物料 / 放行）
     │
     ├─ step① ─▶ [FirstArticleAgent subgraph] ─▶ [gate: 首件触发确认]
     │                                            │ interrupt → 推卡 → 人确认 → resume
     ├─ step② ─▶ [ProcessSwitchAgent subgraph] ─▶ [gate: 工艺版本激活确认]
     │
     ├─ step③④ (并行分支)
     │     ├─ [InspectionAgent subgraph] ─▶ [gate: 点检/计量确认]
     │     └─ [MaterialKittingAgent subgraph] ─▶ [gate: 齐套确认]
     │
     ▼
[barrier_node]  等 ③④ gate 都 PASS
     │
     ├─ step⑤ ─▶ [PassReleaseAgent subgraph] ─▶ [gate: 首件放行确认]  ← 最终人在回路
     │
     ▼
[done_node]  换线完成，落 changeover_session.status=DONE
```

- **并行**：③ ④ 用 LangGraph 并行分支（两条边从 dispatch 出发），barrier_node 用 `asyncio.gather` 语义等待两个 subgraph 都返回。
- **gate 中断**：每个 gate 节点调 `interrupt(value=action_card)`，FastAPI 收到人工确认后调 `graph.ainvoke(Command(resume=confirmation), config)` 续跑。
- **失败隔离**：sub-agent 返回 `FAILED` 时，supervisor 不直接终止，而是推"异常卡"给线长，state 标 `SUSPENDED`，等人工决策（回退 / 修 / 强制继续）。

### 5.2 会话与状态

```sql
changeover_session
  - session_id (PK)
  - work_order_id            -- 换线目标工单
  - target_route_id / target_route_version
  - tenant_context (JSON)
  - status (PLANNING / RUNNING / SUSPENDED / DONE / FAILED)
  - current_step (FIRST_ARTICLE / PROCESS_SWITCH / INSPECTION / KITTING / RELEASE)
  - created_at / updated_at

changeover_step_record
  - record_id (PK)
  - session_id (FK)
  - step                      -- ① ~ ⑤
  - sub_agent_role
  - status (PENDING / RUNNING / GATE_WAITING / CONFIRMED / FAILED)
  - action_card_payload (JSON)   -- 推给 UI 的动作卡
  - gate_decision (PASS / REJECT / RETRY)
  - gate_decided_by             -- 确认人
  - gate_decided_at
  - tool_call_traces (JSON)     -- 关联的 trace_id 列表
  - occurred_at
```

- gate 决策落库，**谁确认的、什么时候、基于哪张卡**全可审计——这是 MES 写红线的可观测兜底。
- sub-agent 内部仍复用 L1 的 `tool_call_trace` 表，按 `session_id` 关联。

### 5.3 动作卡（confirmation gate 的载体）

Agent 不直接写，而是生成**结构化动作卡**推给 UI，人确认后才触发写落库：

```python
class ActionCard(BaseModel):
    card_id: str
    session_id: str
    step: ChangeoverStep
    sub_agent_role: str
    intent: str                    # "激活工艺路线 RR-100 v4"
    draft_payload: dict            # 草稿内容（intent + draft，非已落库数据）
    writes_via: str                # "工艺管理上下文.application.activate_route"
    requires_confirmation: bool = True
    evidence: list[str]            # 关联 trace_id，给确认人看证据
    risk_note: str                 # "此操作将切换产线当前工艺版本"
    deadline: datetime | None      # 换线有时间压力，可带建议时限
```

- 卡片含 `evidence`（trace_id 列表），确认人点开可回溯 sub-agent 的推理与工具调用。
- 人确认后，写落库**不发生在 agent 进程**——agent 只把 confirmation token 交给 `writes_via` 指向的上下文应用服务，由该服务过聚合根不变式 + 事务发件箱落库。

### 5.4 写路径：不旁路应用服务

以"工艺版本激活"为例，写落库链路：

```text
ProcessSwitchAgent
  → draft_route_activation 工具（生成 ActionCard，requires_confirmation）
  → gate 中断，推卡给工艺工程师
  → 工程师在 UI 确认
  → agent 拿 confirmation token
  → 调工艺管理上下文 REST: POST /api/process-routes/{id}/activate
       （这是工艺上下文的【应用服务接口】，不是直改表）
  → 工艺上下文 application 层：
       ProcessRouteAggregate.activate(version)  -- 聚合根不变式校验
       → 事务发件箱落 ProcessRouteActivated 事件（§5.4）
  → agent 收到应用服务返回的成功，gate.decision=PASS，续跑
```

- Agent 全程不碰 MES 原始表，写动作过聚合根不变式 + 事务发件箱——与 [实现说明](../../实现说明/) 的写路径完全一致，只是触发源从"人点按钮"变成"Agent 生成草稿 + 人确认"。

### 5.5 过点 sub-agent 的特殊约束

PassReleaseAgent 是最敏感的 sub-agent：

- **只生成放行意图卡**（`draft_pass_release_card`），**绝不调过点引擎的放行 / 拦截 API**。
- 实际放行由人确认后，走过点执行上下文的正常应用服务（过点主事务 + 规则引擎判定，P99 ≤200ms）。
- supervisor 在 ⑤ 之前 barrier 等 ③ ④ 都 PASS，**避免在点检 / 物料未齐时推放行卡**——这是编排层的防错。
- 启动断言：`pass_release` role 下不得注册任何调过点放行 / 拦截 API 的工具，工具名前缀禁止含 `pass_judge` / `force_release`。

### 5.6 ACL 防腐层

复用 L1 的 ACL 模式（[L1 §5.4](../L1诊断型Agent/L1诊断型Agent-实现方案.md)），每个 sub-agent 一个 ACL 客户端，外部 DTO → 内部 View。新增写工具的 ACL：

```python
class ProcessManagementWriteAclClient:
    """工艺管理上下文写 ACL：只接受带 confirmation token 的激活请求。"""

    async def activate_route(
        self, route_id: str, route_version: str,
        confirmation: ConfirmationToken, tenant: TenantContext,
    ) -> ActivationResult:
        if not confirmation.valid_for(f"process_route.activate:{route_id}:{route_version}"):
            raise PermissionError("confirmation token 无效或已过期")
        resp = await self._http.post(
            f"/api/process-routes/{route_id}/activate",
            json={"version": route_version, "confirmation_id": confirmation.id},
            headers=tenant.headers(),
            timeout=3.0,
        )
        resp.raise_for_status()
        return ActivationResult.model_validate(resp.json())
```

- confirmation token 绑定具体写动作（`action:target`），防篡改、防重放、带过期。
- ACL 不信任 agent 传的任意字段，按上下文应用服务的契约严格校验。

---

## 6. 推荐包结构（在 L1 基础上扩展）

```text
agent_service/
  app/
    api/
      changeover_router.py        # 新增：POST /agent/changeover, POST /agent/changeover/{id}/confirm
      diagnosis_router.py         # L1
      schemas.py
    application/
      changeover_orchestrator.py  # 新增：构建 supervisor 图、驱动编排
      diagnosis_service.py        # L1
      session_manager.py
      action_card_dispatcher.py   # 新增：推卡片（WebSocket + Kafka）
    domain/
      session.py
      report.py
      tool.py                     # 扩展：agent_role / requires_confirmation / WriteToolGate
      tenant.py
      changeover.py               # 新增：ChangeoverSession / ChangeoverStep / ActionCard / GateDecision
    orchestration/                # 新增：multi-agent 编排
      supervisor_graph.py         # supervisor StateGraph
      subagents/
        first_article_agent.py
        process_switch_agent.py
        inspection_agent.py
        material_kitting_agent.py
        pass_release_agent.py
      gates.py                    # confirmation gate 节点（interrupt / resume）
      barriers.py                 # 并行 barrier 节点
    infrastructure/
      ai/
        graph_builder.py          # L1 诊断图
        llm_factory.py
      acl/
        pass_execution.py         # L1
        process_management.py     # 扩展写 ACL
        material.py
        device_data.py
        first_article.py          # 新增
        inspection.py             # 新增
      rag/
      kafka/
        listeners.py
        action_card_producer.py   # 新增：推动作卡事件
      persistence/
        models.py                 # 扩展 changeover_session / changeover_step_record
        session_repo.py
        trace_repo.py
        report_repo.py
        changeover_repo.py        # 新增
        checkpointer.py
      redis_/
        tool_cache.py
        confirmation_store.py     # 新增：confirmation token 存储
      obs/
        tracing.py
        metrics.py
    config.py
    main.py
  tests/
  pyproject.toml
```

- `orchestration/subagents/` 是 multi-agent 的落点：每个文件一个 sub-agent 的 `StateGraph` 工厂 + system prompt + 专属工具集。
- `orchestration/gates.py` 集中confirmation gate 逻辑，便于审计与统一兜底。

---

## 7. 关键代码骨架

### 7.1 sub-agent 工厂（以工艺切换为例）

```python
# app/orchestration/subagents/process_switch_agent.py
class ProcessSwitchAgent:
    """工艺管理上下文 sub-agent：确认新工艺版本激活。"""

    ROLE = "process_switch"

    def __init__(self, llm: BaseChatModel, registry: ToolRegistry,
                 trace_repo: ToolCallTraceRepo) -> None:
        self._graph = self._build(llm, registry, trace_repo)

    def _build(self, llm, registry, trace_repo) -> CompiledGraph:
        g = StateGraph(AgentState)
        g.add_node("model", self._model_node(llm))
        g.add_node("tools", ToolNode(registry, trace_repo, role=self.ROLE))
        g.add_edge(START, "model")
        g.add_conditional_edges("model", route_tools, ["tools", END])
        g.add_edge("tools", "model")
        return g.compile()

    async def _model_node(self, llm, state: AgentState) -> AgentState:
        tools = self._registry.tools_for(self.ROLE, state["tenant"])
        # 工艺切换 system prompt：强制 route_version、说明只能草拟激活需人确认
        prompt = self._system_prompt(state, tools)
        resp = await llm.ainvoke(prompt, tools=to_json_schema(tools))
        state["pending_tool_calls"] = resp.tool_calls
        return state
```

### 7.2 supervisor 编排（并行 + barrier + gate）

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
        # 工艺确认后并行派发点检 + 物料
        g.add_conditional_edges(
            "gate_process_switch",
            lambda s: ["inspection", "kitting"],  # 并行两条边
        )
        g.add_edge("inspection", "gate_inspection")
        g.add_edge("kitting", "gate_kitting")
        g.add_edge("gate_inspection", "barrier")
        g.add_edge("gate_kitting", "barrier")
        g.add_edge("barrier", "release")     # barrier 等 ③④ 都 PASS 才进 ⑤
        g.add_edge("release", "gate_release")
        g.add_edge("gate_release", "done")
        g.add_edge("done", END)
        return g.compile()

    async def _barrier_node(self, state: ChangeoverState) -> ChangeoverState:
        # LangGraph 并行分支汇合点：两个 gate 都 PASS 才放行
        if not (state["gate_inspection"] == "PASS" and state["gate_kitting"] == "PASS"):
            # 任一未过 → 挂起转人工，不推放行卡
            state["status"] = "SUSPENDED"
            await self._gates.push_exception_card(state, "点检或齐套未通过，禁止放行")
        return state

    def _gate(self, step: ChangeoverStep):
        async def fn(state: ChangeoverState) -> ChangeoverState:
            # 生成动作卡 → interrupt 等人确认
            card = build_action_card(state, step)
            await self._repo.save_step(state["session_id"], step, "GATE_WAITING", card)
            # LangGraph interrupt：暂停，等外部 resume
            decision = await self._gates.await_confirmation(state["session_id"], step, card)
            state[f"gate_{step.lower()}"] = decision
            await self._repo.record_gate(state["session_id"], step, decision)
            return state
        return fn
```

### 7.3 confirmation gate（interrupt / resume）

```python
# app/orchestration/gates.py
class GateManager:
    def __init__(self, dispatcher: ActionCardDispatcher,
                 confirmation_store: ConfirmationStore) -> None:
        self._dispatcher = dispatcher
        self._store = confirmation_store

    async def await_confirmation(self, session_id: str, step: ChangeoverStep,
                                 card: ActionCard) -> str:
        # 1. 推卡片到 UI（WebSocket + Kafka）
        await self._dispatcher.push(card)
        # 2. LangGraph interrupt：阻塞直到 FastAPI /confirm 端点 resume
        confirmation = await interrupt(value=card)  # 返回 ConfirmationToken
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
    # resume supervisor 图
    await graph.ainvoke(
        Command(resume=token),
        config={"configurable": {"thread_id": session_id}},
    )
    return ConfirmResponse(step=req.step, decision="PASS" if req.approved else "REJECT")
```

### 7.4 应用服务编排

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
        # 异步驱动 supervisor，gate 处会 interrupt 暂停
        asyncio.create_task(self._drive(session, tenant))
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
                timeout=1800.0,   # 换线整体上限 30 分钟（含人确认等待）
            )
        except (GraphRecursionError, asyncio.TimeoutError) as e:
            await self._sessions.mark_failed(session, str(e))
```

### 7.5 启动断言（multi-agent 红线）

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    reg = app.state.tool_registry
    # 1. 写工具必须 requires_confirmation + writes_via
    for d in reg.all():
        if not d.read_only:
            assert d.requires_confirmation and d.writes_via, d.name
    # 2. supervisor 无工具
    assert not reg.tools_for("supervisor", ANY_TENANT)
    # 3. pass_release 下无放行/拦截类工具
    pr_tools = reg.tools_for("pass_release", ANY_TENANT)
    assert not any(t.name.startswith(("pass_judge", "force_release")) for t in pr_tools)
    # 4. 各 sub-agent 工具集互斥（无跨 role 注册）
    reg.assert_role_partition()
    yield
```

---

## 8. 可观测性与兜底

### 8.1 指标（在 L1 基础上新增）

| 指标 | 含义 |
|------|------|
| `changeover_session_total` | 换线会话数（按 status label） |
| `changeover_step_latency_seconds` | 各步耗时（含 gate 等待，Histogram） |
| `changeover_gate_decision_total` | gate 决策数（按 step / decision label） |
| `changeover_parallel_save_seconds` | ③‖④ 并行相对串行的节省时间 |
| `changeover_suspended_total` | 故障隔离挂起次数 |
| `changeover_write_tool_total` | 写工具调用数（按 tool / confirmed label） |
| `changeover_write_rejected_total` | 未带 confirmation 被拒次数 |

### 8.2 trace 串联

- 一个换线会话一个 `trace_id`，透传到所有 sub-agent 与下游 Java 服务（`traceparent`）。
- 每张动作卡带 `evidence`（trace_id 列表），确认人可点开回溯 sub-agent 的工具调用链。

### 8.3 兜底

- gate 等待人确认有 deadline，超时自动挂起 `SUSPENDED` 推线长，不无限阻塞。
- barrier 检测到 ③ 或 ④ 未 PASS，**禁止推放行卡**，直接挂起——编排层防错。
- 写工具未带有效 confirmation token → `WriteToolGate` 拒绝 + 指标 +1 + 告警。
- sub-agent 连续失败 2 次 → 标 `SUSPENDED`，推异常卡，不自动重试到死。

---

## 9. 实现步骤

### 阶段一：multi-agent 骨架（3 周）

1. 在 L1 的 `agent_service` 上扩展 `orchestration/` 包，搭 supervisor + 2 个 sub-agent（工艺切换 + 物料齐套，先串行跑通）。
2. 实现 `ToolDescriptor.agent_role` / `requires_confirmation` / `writes_via` 与 `WriteToolGate` 启动断言。
3. 实现 `changeover_session` / `changeover_step_record` 表 + repo。
4. 接 LangGraph `interrupt` / `Command(resume=...)`，跑通单个 gate 的"推卡 → 确认 → 续跑"。
5. WebSocket + Kafka 动作卡推送 MVP。

### 阶段二：并行与 barrier（2 周）

6. 补全 InspectionAgent / FirstArticleAgent / PassReleaseAgent。
7. supervisor 加并行分支（③ ‖ ④）+ barrier_node，验证 barrier 等两者都 PASS。
8. 实现 barrier 的"未 PASS 禁止推放行卡"防错。

### 阶段三：写路径与 confirmation（3 周）

9. 实现 `ConfirmationStore`（redis，token 绑定 `action:target` + 过期）。
10. 各上下文写 ACL 客户端（工艺激活 / 首件工单 / 点检记录），调对应应用服务 REST。
11. PassReleaseAgent 启动断言：无放行 / 拦截类工具，仅 `draft_pass_release_card`。
12. gate 决策落库可审计，evidence 关联 trace。

### 阶段四：试点与加固（2 周）

13. 挑一条产线灰度换线 Agent，confirmation gate 做扎实。
14. 接 OTel + prometheus 指标（§8.1），观察并行节省时间与挂起率。
15. 评测：换线耗时、gate 拒绝率、故障隔离触发次数。
16. 沉淀换线场景评测集，回归提示词 / 模型变更。

---

## 10. 约束落地检查清单

- [ ] supervisor 注册的工具集为空，启动断言校验。
- [ ] 各 sub-agent 工具集互斥（`assert_role_partition`），越界工具不注册。
- [ ] 所有写工具 `requires_confirmation=True` 且声明 `writes_via`，否则启动失败。
- [ ] `pass_release` role 下无 `pass_judge` / `force_release` 类工具，仅 `draft_pass_release_card`。
- [ ] 写落库走各上下文应用服务 REST，过聚合根不变式 + 事务发件箱，不旁路写。
- [ ] confirmation token 绑定 `action:target`，带过期，防篡改防重放。
- [ ] barrier 在 ⑤ 之前等 ③ ④ 都 PASS，未 PASS 禁止推放行卡。
- [ ] gate 决策（谁 / 何时 / 基于哪张卡）落 `changeover_step_record` 可审计。
- [ ] Agent 不调过点引擎放行 / 拦截 API，过点 P99 ≤200ms 判定仍走规则引擎。
- [ ] `query_process_route` 强制 `route_version`，ACL 校验返回 `ACTIVE`（继承 L1）。
- [ ] 工具调用前按 `TenantContext` 权限过滤，sub-agent 只见本上下文 toolset。
- [ ] 每步带 trace，动作卡含 evidence（trace_id 列表），OpenTelemetry 透传 `traceparent`。
- [ ] gate 等待有 deadline，超时挂起不无限阻塞；sub-agent 连续失败 2 次挂起转人工。

---

## 11. 面试防守 Q&A

**Q：为什么换线 Agent 用 multi-agent，而 L1 诊断用单 agent？**
A：看并行度、分权需求、故障隔离三点。换线跨 5 个上下文，点检 ‖ 物料齐套无依赖可真并行，省停线时间；每个上下文写工具白名单不同，把放行能力只挂在过点 sub-agent，别的 agent 调不到，能力从架构层切掉；点检超差可单独挂起不污染整条链。L1 诊断是强串行推理链（必须先拿 routeVersion 才能查工艺），单 agent + ReAct 更自然。这是按"并行度 / 分权 / 故障隔离"选型，不是为多而多。

**Q：multi-agent 怎么保证不越界进过点主事务？**
A：三层兜底。第一层，supervisor 不持任何工具，想越界也没工具调。第二层，工具按 `agent_role` 裁剪，过点放行类工具只注册在 PassReleaseAgent，且只有 `draft_pass_release_card`（生成放行意图卡），不调放行 / 拦截 API。第三层，启动断言：`pass_release` role 下不得有 `pass_judge` / `force_release` 前缀工具。实际放行由人确认后走过点上下文正常应用服务，过点 P99 ≤200ms 判定仍走规则引擎——和 RAG 路线 D 的边界完全一致。

**Q：写动作怎么保证不旁路应用服务？**
A：写工具必须声明 `requires_confirmation` 和 `writes_via`（落库走哪个应用服务），否则启动断言失败。Agent 生成的是 intent + draft（动作卡），人确认后拿 confirmation token，调对应上下文的应用服务 REST——比如工艺激活调 `POST /api/process-routes/{id}/activate`，这是工艺上下文的应用服务接口，过聚合根不变式 + 事务发件箱落库。Agent 全程不碰 MES 原始表，触发源从"人点按钮"变成"Agent 草拟 + 人确认"，写路径完全不变。

**Q：③ ④ 并行怎么实现？barrier 怎么等？**
A：LangGraph 的 `StateGraph` 支持从一个节点出并行边——`gate_process_switch` 的 conditional_edges 返回 `["inspection", "kitting"]`，两个 subgraph 并发执行。barrier_node 是两条边的汇合点，LangGraph 自动等两个分支都返回才执行；我在 barrier_node 里再校验 `gate_inspection == PASS and gate_kitting == PASS`，任一未过就挂起，不推放行卡。这是编排层的防错，不靠 prompt。

**Q：confirmation gate 怎么实现人在回路？**
A：用 LangGraph 的 `interrupt`。supervisor 跑到 gate 节点时调 `interrupt(value=action_card)` 暂停，state 落 MySQL（checkpointer），进程不阻塞。FastAPI 暴露 `/confirm` 端点，人确认后调 `graph.ainvoke(Command(resume=token))` 续跑。confirmation token 绑定 `action:target` 带过期，防篡改防重放。gate 决策（谁 / 何时 / 基于哪张卡）落 `changeover_step_record`，全可审计。

**Q：换线 Agent 失败了怎么办？会卡住产线吗？**
A：故障隔离。sub-agent 连续失败 2 次标 `SUSPENDED`，推异常卡给线长，不自动重试到死。barrier 检测到点检或齐套未过，禁止推放行卡，直接挂起。gate 等待有 deadline，超时自动挂起不无限阻塞。最坏情况是换线退回人工编排，不会越界写、不会错误放行——和 MES 防错理念一致，宁可拦下让人判。

**Q：换线涉及这么多上下文，权限怎么管？**
A：工具注册时声明 `agent_role` 和 `required_tenant_scopes`，`ToolRegistry.tools_for(role, tenant)` 双重过滤：先按 role 裁剪到本上下文 toolset，再按 tenant 裁剪到本车间 / 产线可见。权限在工具调用前过滤，不是答完再裁剪。本 MES 的 14 个限界上下文边界本身就是天然的权限切分面，multi-agent 把这层切分从"工具级"升级到"agent 级"，隔离更硬。

**Q：为什么用 LangGraph supervisor 而不是 AutoGen / CrewAI？**
A：换线是有严格步骤顺序的编排，不是多 agent 对话。LangGraph 的 `StateGraph` 把串行 / 并行 / barrier / gate 做成显式图，每条边可加条件与中断，可控可审计；subgraph 就是 sub-agent，工具集 / prompt / recursion_limit 互不污染。AutoGen 的 conversational 模式适合对话协作，难做严格步骤编排；CrewAI 抽象偏高，难做细粒度权限拦截与 gate 状态机。LangGraph 离原语更近，且与 L1 同构，复用底座。

---

## 12. 一句话定位

"L3 编排型 Agent 用 LangGraph 的 supervisor + subgraph 做 multi-agent，**一个上下文一个专职 sub-agent，上下文边界同时是 agent 边界和工具边界**——supervisor 不持工具、放行能力只挂在过点 sub-agent 且仅生成放行意图卡，能力从架构层切掉；点检 ‖ 物料齐套真并行缩短停线，barrier 在放行前等两者都 PASS；写动作过 confirmation gate（interrupt / resume + 绑定 action:target 的 token），落库走各上下文正常应用服务过聚合根不变式 + 事务发件箱，绝不旁路写、不进过点主事务——让 Agent 在 RAG 护城河上把'查完→诊断→草拟处置→推动作'串成一条人在回路的自动链，而写的闸门始终在人手里。"
