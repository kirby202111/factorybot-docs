# L3 编排型 Agent 实现方案（Python + LangGraph：编排代码层 + 4 类非确定 agent 能力）

> 本文是 [AGENT服务引入路线.md](../AGENT服务引入路线.md) §2.4 L3 编排型 Agent 的落地展开，输出**技术栈、分层架构、实现方案、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + LangGraph + Pydantic），与 [L1诊断型Agent-实现方案.md](../L1诊断型Agent/L1诊断型Agent-实现方案.md) 同栈，复用其 ACL / 工具注册 / 可观测底座。
> **核心形态（重述）**：L3 = **编排代码层（确定性）** + **4 类非确定 agent 能力**。代码层做所有"步骤顺序固定、判定规则固定、输入结构化"的活（编排 / 结构化比对 / barrier 硬防错 / confirmation gate / 写落库 / 版本钉死 / 放行能力隔离）；agent 只在 4 类痛点对应的非确定决策点介入（A 核对异常根因 / B 故障隔离范围 / C 客诉根因追溯 / D 生成类）。这条边界与 [痛点操作步骤与解决方案.md](L3编排型Agent-痛点操作步骤与解决方案.md) §0 完全一致——**凡是代码能做的，不交给 LLM**。
> **口径纪律**：写动作**过 confirmation gate**，落库走各上下文**正常应用服务**，绝不旁路写；agent **不进过点主事务**（[领域总览.md](../../领域模型/领域总览.md) §5.3），过点 P99 ≤200ms 判定仍走规则引擎；不编业务效果数字。

---

## 0. 选型判断：为什么是"编排代码层 + 4 类 agent 能力"而非"5 个换线 sub-agent"

旧设计把换线拆成 5 个 sub-agent（首件 / 工艺 / 钢网程序 / 物料 / 放行），看似 multi-agent，实则其中 4 个是确定性查询比对——齐套率是不是 100%、钢网号等不等于工艺路线里的、程序版本是不是 ACTIVE，全是 `expected == actual` 的结构化判定，LLM 的非确定性在这里是负资产。重写后用一个判断标准贯穿全文档：

**一个步骤是否需要 agent，看三问**：
1. 输入是否开放（非结构化、需语义理解）？
2. 是否需要推理 / 生成（非固定规则）？
3. 分支是否难以穷举（决策树永远落后于现场）？

三问皆否 -> **代码节点**；三问有一 -> **agent 节点**。

按此标准重划 L3 全域：

| 步骤类型 | 例子 | 归属 | 实现 |
|---|---|---|---|
| 顺序 / 并行 / 汇合 | 换线 5 步编排 | 代码 | supervisor StateGraph 的边 |
| 结构化比对 | 齐套率 == 100%、钢网号匹配、程序版本 == ACTIVE | 代码 | query + compare 节点 |
| 硬防错 | 未双 PASS 不放行 | 代码 | `barrier_node` |
| 写落库 | 激活工艺、下达隔离、发布 SOP | 代码 | gate + 应用服务 REST |
| 版本钉死 | `routeVersion` 强制过滤 | 代码 | ACL |
| 放行能力隔离 | 不给 agent 放行 API | 代码 | 工具裁剪 + 启动断言 |
| 动作卡推送 + 超时升级 | gate 等人确认、卡住催办 | 代码 | dispatcher + deadline |
| **核对异常根因分支** | mismatch 后判台账错 / 工艺录错 / 产线拿错 | **agent A** | RootCauseAgent |
| **故障隔离范围** | 故障模式 × 漂移窗口 × 产品敏感度 | **agent B** | FaultImpactAgent |
| **客诉根因排序** | 5M1E 假设加权排序 + 跨源语义关联 | **agent C** | TraceabilityAgent（复用 L1） |
| **生成类** | SOP / 8D / 返工工艺草拟 | **agent D** | DraftAgents |

**为什么仍用 multi-agent 而非单 agent**：A/B/C/D 四能力各自工具集互斥（A 查钢网库台账 + 工艺审计 + 上工单收线记录，B 查设备遥测 + 工艺 FMEA + 历史不良，C 查过点 + 物料 + 设备 + 质量，D 查工艺 diff + 历史案例库），且可并行（B 的故障影响排查与维修单草拟并行、C 的供应商批次追溯与同批次在库品隔离判定并行）。单 agent 工具集过大易调错工具、上下文污染；multi-agent 按能力切分，工具集小而专注，且故障可隔离（某能力 sub-agent 失败挂起，不污染整条链）。L1 诊断强串行用单 agent，L3 多能力可并行 / 可分权用 multi-agent——选型仍按"并行度 / 分权需求 / 故障隔离"，不是为多而多。

---

## 1. 设计目标与边界

### 1.1 目标

把现场跨上下文长流程里**非确定的那段**（根因处置 / 隔离范围判定 / 根因追溯 / 生成草拟）从"人脑拍 + 电话串 3 个系统 + 手写"升级为"agent 自适应取证 + 推理假设 + 草拟 + 人确认落库"。**确定性那段**（编排 / 结构化比对 / 硬防错 / 写落库）用代码骨架做扎实，不交给 LLM。

### 1.2 4 类 agent 能力（本文核心）

| 能力 | sub-agent | 触发 | 输入 | 输出 | 对应痛点 |
|---|---|---|---|---|---|
| **A 核对异常根因** | RootCauseAgent | barrier 检出结构化 mismatch | mismatch 结构化结果（expected / actual / code） | 根因假设 + 置信度 + 处置卡（路由给谁 + 建议动作） | [痛点 A](L3编排型Agent-痛点操作步骤与解决方案.md) |
| **B 故障隔离范围** | FaultImpactAgent | 设备故障事件 | 故障设备 ID + 报警时刻 + 遥测窗口 | 故障模式 + 漂移窗口 + 隔离批次集 + 敏感度理由 | [痛点 B](L3编排型Agent-痛点操作步骤与解决方案.md) |
| **C 客诉根因追溯** | TraceabilityAgent | 客诉触发 | 批次号 / 序列号 | 5M1E 假设排序 + 证据链 + 8D 草稿素材 | [痛点 C](L3编排型Agent-痛点操作步骤与解决方案.md) |
| **D 生成类** | DraftAgents（SOP / 8D / ReworkCraft） | 工艺变更 / 客诉 / 返工触发 | 工艺 diff / 追溯链 / 不良模式 | SOP / 8D / 返工工艺草稿 | [痛点 D](L3编排型Agent-痛点操作步骤与解决方案.md) |

### 1.3 硬边界（代码层兜底，agent 不碰）

| 边界 | 说明 | 落地 |
|------|------|------|
| **不进过点主事务** | agent 不调过点引擎放行 / 拦截 API | 放行类工具不注册；实际放行走过点上下文应用服务 |
| **写动作 confirmation gate** | 所有写工具 `requires_confirmation`，生成 intent + draft，人确认后落库 | `WriteToolGate` 拦截无 token 写 |
| **写不旁路应用服务** | agent 不直写 MES 原始表 | 写走 `writes_via` 指向的应用服务 REST，过聚合根不变式 + 事务发件箱 |
| **版本一致性** | 工艺查询强制 `route_version`，与过点记录 `routeVersion` 对齐 | ACL 校验（继承 L1 §4.3） |
| **能力隔离** | agent 只见本能力 toolset | `tools_for(capability)` 按 agent 角色裁剪 |
| **硬防错** | 未双 PASS 不放行 | `barrier_node` 确定性校验，非 agent 判定 |
| **可观测兜底** | 每步推理 + 每个 gate 带 trace + 置信度，低置信度转人工 | 复用 L1 的 `tool_call_trace` + OTel |

### 1.4 典型场景（编排代码层 + agent 能力组合）

每个场景 = 代码骨架（编排 / 比对 / barrier / gate / 写）+ 在非确定决策点嵌入对应 agent 能力。**agent 只在异常 / 开放分支触发**。

| # | 场景 | 代码骨架 | 嵌入的 agent 能力 |
|---|------|---------|------------------|
| ① | **换线** | 首件 gate -> 工艺激活 gate ->（钢网程序比对 ‖ 齐套比对）-> barrier -> 放行 gate | 仅在钢网 / 程序 mismatch 分支嵌 **A** |
| ② | **设备故障复产** | 维修单 gate ->（维修 ‖ 故障排查）-> 复校 gate -> 复产首件 gate | 嵌 **B**（隔离范围判定） |
| ③ | **客诉 8D** | 自动追溯 ->（供应商批次追溯 ‖ 隔离判定）-> 隔离 gate -> 8D 发布 gate | 嵌 **C**（根因排序）+ **D**（8D 草拟） |
| ④ | **工艺变更落地** | 订阅 `ProcessRouteActivated` ->（SOP 草拟 ‖ 资质核对）-> barrier -> 首件验证 gate | 嵌 **D**（SOP 草拟） |

**关键体现"懂什么时候不用 AI"**：换线场景①如果全程 PASS（无 mismatch、齐套达标），agent A 根本不触发——纯代码骨架跑完换线。agent 只在异常分支赚回成本。同理场景④的资质核对是确定性查询（操作工资质 ∈ 工艺要求资质集），代码做，不嵌 agent；只有 SOP 草拟（开放生成）嵌 D。

### 1.5 与 L1 的复用关系

- **同栈**：Python + FastAPI + LangGraph + Pydantic，同包结构（§6）。
- **复用底座**：ACL 客户端、`ToolDescriptor` / `ToolRegistry`、`tool_call_trace`、OTel / prometheus 指标、`TenantContext`。
- **能力 C 直接嵌入 L1**：TraceabilityAgent 把 L1 诊断图作为子图调用，复用其 5M1E 假设排序 + 证据链 + 置信度兜底，不重写。
- **新增**：编排代码层（supervisor StateGraph 的代码节点 + barrier + gate）、4 类 agent 能力 sub-agent、`WriteToolGate`、confirmation gate 状态机、动作卡推送。

---

## 2. 技术栈

### 2.1 选型总览（仅列与 L1 差异项）

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| Agent 编排 | **LangGraph（supervisor + subgraph）** | supervisor 用 `StateGraph` 编排**代码节点 + agent 调用节点**混合图；天然支持并发节点、barrier、条件路由、`interrupt` |
| 并发原语 | LangGraph `Send` + asyncio.gather | 无依赖步骤并行，supervisor 在 barrier 节点等待两者都完成 |
| 状态机 | LangGraph `StateGraph` + Pydantic `L3State` | 会话状态（各步骤 status / gate 决策 / 动作卡 / agent 假设）外置 MySQL，可中断恢复 |
| 写工具闸门 | `WriteToolGate`（新增） | 区别于 L1 的 `ReadOnlyToolGate`：允许注册写工具，但强制 `requires_confirmation` + 人确认 token |
| 动作卡推送 | WebSocket（SSE）+ Kafka 动作卡事件 | 操作工 / 线长 UI 实时收卡片，确认动作回写 |
| 持久化 | SQLAlchemy 2.0 (async) + LangGraph `SqlSaver` | 会话 + agent 状态 + gate 决策记录 |

### 2.2 为什么用 LangGraph supervisor 模式

- **显式图 > 隐式 prompt**：编排步骤的串行 / 并行 / barrier / gate 关系在 `StateGraph` 里是显式边，可对每条边加条件（"钢网程序比对与齐套比对都 PASS 才进放行"）。用纯 prompt 让一个模型自己编排，易丢步骤、难审计。
- **代码节点与 agent 节点同图**：LangGraph 的节点可以是任意 Python 函数——结构化比对、barrier 校验是纯代码节点；根因推理、隔离范围判定是 agent 调用节点。两类节点在同一张图里混合编排，**代码节点不消耗 LLM 调用**。
- **subgraph = agent 能力**：4 类 agent 能力各自是独立 subgraph，有自己的工具集、system prompt、recursion_limit，互不污染上下文。
- **可中断**：`interrupt` 在 confirmation gate 处暂停等人工确认，确认后 `Command(resume=...)` 续跑——对应"人在回路"。

### 2.3 为什么不用 AutoGen / CrewAI

- AutoGen 的 conversational 模式适合"多 agent 互相对话"，但 L3 是**有严格步骤顺序的编排**（代码骨架驱动），不是对话，supervisor + subgraph 更贴。
- CrewAI 抽象偏高，难做细粒度权限拦截与 gate 状态机；LangGraph 离原语更近，可控性更强，与 L1 同构。

---

## 3. 总体架构

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ agent-service（Python + FastAPI + LangGraph）                            │
│                                                                           │
│  FastAPI ── POST /agent/l3/{scenario}/start ──▶ L3Orchestrator           │
│   POST /agent/l3/{session_id}/confirm           │ 构建 supervisor StateGraph│
│                                                  ▼                         │
│              ┌─────────────────────────────────────────────────────────┐ │
│              │  SupervisorGraph（编排代码层，不持任何工具）                │ │
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
                      │  -> 责任人 UI  ->  /confirm 端点         │
                      └──────────────────────────────────────┘
```

### 3.1 关键设计决策

- **supervisor 不持工具**：supervisor 只做"下一步该干啥 + 派发 + 收口"，**没有任何写工具**，也没有 LLM 调用——它是纯代码编排器。写工具只挂在对应 agent 能力（且仅生成意图卡）。
- **代码节点 vs agent 节点**：`query+compare`、`barrier`、`gate`、`write_via_appservice` 是纯 Python 函数节点，不调 LLM；`root_cause`、`fault_impact`、`traceability`、`draft_*` 是 agent 调用节点，调 LLM + 工具。**换线全程 PASS 时，agent 节点根本不执行**——LLM 调用次数为 0。
- **agent 能力工具集互斥**：每个 agent 注册时声明 `capability`（A/B/C/D），`ToolRegistry.tools_for(capability)` 只返回该能力的工具。RootCauseAgent 看不到 FaultImpactAgent 的设备遥测工具。
- **gate 是显式代码节点**：每个 confirmation gate 是 supervisor 图里的代码节点，`interrupt` 暂停 -> 推动作卡 -> 等人确认 -> `resume` 续跑。gate 决策落库可审计。
- **barrier 在放行门禁**：并行分支汇合，supervisor 在放行前 barrier 等所有 gate 都 PASS（确定性硬校验）。

---

## 4. 工具注册：按 agent 能力裁剪 + 写工具白名单

### 4.1 工具与 agent 能力映射

| agent 能力 (capability) | 只读工具 | 写工具（`requires_confirmation=True`） |
|------------------------|---------|----------------------------------------|
| `root_cause` (A) | `query_stencil_lending`（钢网借还记录）/ `query_stencil_master_history`（主数据变更）/ `query_route_audit`（工艺录入审计）/ `query_last_changeover_close`（上工单收线记录）/ `query_program_local_version`（设备本地程序库） | `draft_disposition_card`（草拟处置卡：根因+路由+建议动作，人确认） |
| `fault_impact` (B) | `query_equipment_telemetry`（设备遥测时序）/ `query_fault_history`（历史故障形态）/ `query_process_fmea`（工艺 FMEA 敏感度）/ `query_batches_in_window`（窗口内批次）/ `query_product_sensitivity`（产品敏感度） | `draft_isolation_card`（草拟隔离集卡，人确认后走返工上下文下达） |
| `traceability` (C) | L1 全部只读 toolset（过点 / WIP / 物料 / 设备 / 质量）+ `query_supplier_batch_trace`（供应商批次追溯） | 无（诊断只读，隔离下达走 B 或单独 gate） |
| `draft_sop` (D) | `query_route_diff`（工艺版本 diff）/ `query_prior_sop`（旧 SOP）/ `query_fmea` | `draft_sop`（草拟新 SOP，人确认发布） |
| `draft_8d` (D) | `query_trace_chain`（追溯链，由 C 产出）/ `query_history_8d`（历史同类 8D） | `draft_8d_report`（草拟 8D，人确认发布） |
| `draft_rework_craft` (D) | `query_original_route` / `query_defect_mode` / `query_history_rework` | `draft_rework_craft`（草拟返工工艺建议，人确认下达） |
| `supervisor` | 无 | 无 |

**注**：换线场景里"查钢网当前状态、查齐套率、查程序版本"这些**确定性查询比对不进 ToolRegistry**——它们是代码节点的直接 ACL 调用，不经过 LLM。ToolRegistry 只装 agent 能力用的工具。

### 4.2 工具元数据（扩展 L1）

```python
class ToolDescriptor(BaseModel):
    name: str
    description: str
    bounded_context: str
    capability: str                 # 新增：归属哪个 agent 能力 (A/B/C/D)
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
                raise WriteToolGate(f"写工具必须声明 writes_via: {d.name}")
        self._descriptors[d.name] = d

    def tools_for(self, capability: str, tenant: TenantContext) -> list[ToolDescriptor]:
        return [
            d for d in self._descriptors.values()
            if d.capability == capability and tenant.can_access(d.required_tenant_scopes)
        ]
```

- 写工具**必须**声明 `requires_confirmation` 与 `writes_via`，否则启动断言失败——红线靠代码兜底。
- supervisor 的 `capability="supervisor"` 工具集为空，启动断言校验。
- 放行类工具**不注册到任何 capability**——过点放行能力从架构层不存在于 agent 工具集。

---

## 5. 实现方案

### 5.1 编排代码层（supervisor StateGraph 的代码节点 + agent 调用节点）

以**换线场景①**为例（其他场景同骨架，换 sub-agent 组合）：

```text
[plan_node]  代码：决定步骤序列
  ↓
[query_first_article + gate: FIRST_ARTICLE]  代码：查首件状态 -> gate 人确认触发
  ↓ PASS
[query_active_route + gate: PROCESS_SWITCH]  代码：查工艺版本 -> gate 人确认激活
  ↓ PASS
  ├──────────────────────┬──────────────────────┐
  ▼并行(代码)            ▼并行(代码)              │
[tooling_check]         [kitting_check]          │  代码：query+compare
  │ expected=ST-B        │ kit_rate==100%?       │   产出结构化结果
  │ actual=扫码           │                       │
  ├─ PASS                 ├─ PASS                 │
  └─ FAIL(mismatch)       └─ FAIL(缺料)           │
  ↓                        ↓                      │
[barrier_node]  代码：等两条分支汇合，校验结果
  │
  ├─ 都 PASS -> [draft_release_card + gate: RELEASE]  代码：草拟放行卡（非LLM，结构化拼装）-> gate 人确认
  │             -> 过点上下文应用服务实际放行
  │
  ├─ tooling FAIL -> [RootCauseAgent (A)]  agent：自适应取证 + 根因假设 + 草拟处置卡
  │                  -> [gate: DISPOSITION]  人确认处置 -> 处置落库 -> 回 tooling_check 重检
  │
  └─ kitting FAIL -> [SUSPENDED]  代码：缺料是确定的，不嵌 agent，直接挂起推线长催料
  ↓
[done_node]  代码：session.status=DONE
```

**节点性质区分**（核心）：

| 节点 | 性质 | 是否调 LLM | 说明 |
|------|------|----------|------|
| `plan` / `query_*` / `compare` / `barrier` / `gate` / `done` / `draft_release_card` | 代码 | 否 | 确定性，Python 函数 |
| `RootCauseAgent` (A) | agent | 是 | 仅 tooling mismatch 时触发 |
| `FaultImpactAgent` (B) | agent | 是 | 仅故障场景触发 |
| `TraceabilityAgent` (C) | agent | 是 | 仅客诉场景触发 |
| `DraftAgents` (D) | agent | 是 | 仅生成类需求触发 |

- **并行**：tooling_check ‖ kitting_check 用 LangGraph 并行分支，barrier_node 等两条都返回。
- **gate 中断**：每个 gate 节点调 `interrupt(value=action_card)`，FastAPI 收到人工确认后 `Command(resume=token)` 续跑。
- **失败隔离**：agent 节点返回低置信度 / FAILED 时，supervisor 不直接终止，推"异常卡"给线长，state 标 `SUSPENDED`，等人工决策。
- **mismatch 的处置回路**：A 草拟处置卡 -> 人确认 -> 处置落库（如归还 ST-A、领用 ST-B）-> 回 `tooling_check` 重检——重检仍是代码节点，agent 不参与"重检通过没"的判定。

### 5.2 4 类 agent 能力实现

#### A. RootCauseAgent（核对异常根因）

```python
# app/orchestration/agents/root_cause_agent.py
class RootCauseAgent:
    """能力 A：拿到结构化 mismatch，自适应取证 + 根因假设 + 草拟处置卡。"""

    CAPABILITY = "root_cause"

    def __init__(self, llm, registry, trace_repo):
        self._graph = self._build(llm, registry, trace_repo)

    def _build(self, llm, registry, trace_repo):
        g = StateGraph(AgentState)
        g.add_node("model", self._model_node(llm, registry))
        g.add_node("tools", ToolNode(registry, trace_repo, capability=self.CAPABILITY))
        g.add_edge(START, "model")
        g.add_conditional_edges("model", route_tools, ["tools", END])
        g.add_edge("tools", "model")
        return g.compile()

    async def _model_node(self, llm, state):
        tools = self._registry.tools_for(self.CAPABILITY, state["tenant"])
        # system prompt：拿到 mismatch 结构化结果，按需取证，输出根因假设+处置卡
        prompt = f"""你是 MES 换线钢网/程序核对异常的根因诊断 agent。
输入：结构化 mismatch 结果（expected={state['expected']}, actual={state['actual']}, code={state['mismatch_code']}）。
按需调用只读工具取证（钢网借还记录 / 主数据变更 / 工艺审计 / 上工单收线记录），不要套固定决策树。
取证充分后输出：
  - root_cause_hypothesis: 根因假设（产线拿错 / 上工单未还库 / 台账改名 / 工艺录错 / ...）
  - confidence: 置信度（high/medium/low），低置信度必须列出仍需人确认的疑点
  - disposition_card: 处置卡（route_to 责任人 + suggested_actions 建议动作）
不得直接下达处置，处置卡需人确认。"""
        resp = await llm.ainvoke(prompt, tools=to_json_schema(tools))
        return {**state, "pending_tool_calls": resp.tool_calls}
```

- **自适应取证**：agent 根据中间结果决定下一步查什么——查到"ST-A 借出未还"后，自适应去查上工单收线记录，而不是固定 JOIN。
- **根因分支不穷举**：根因空间开放（产线拿错 / 未还库 / 台账改名 / 借出未登记 / 工艺录错 / 扫码制式不同 / ...），agent 按取证证据给假设，新根因出现时决策树不用改。
- **低置信度转人工**：置信度 low 时，处置卡标 `need_human_review=True`，列疑点，不自动路由。

#### B. FaultImpactAgent（故障隔离范围）

输入：故障设备 ID + 报警时刻。agent 拉`query_equipment_telemetry`推理故障模式（硬停 / 软漂移 / 间歇）-> 软漂移估漂移起始窗口 -> `query_batches_in_window` 取窗口内批次 -> `query_process_fmea` + `query_product_sensitivity` 关联"漂移参数 × 产品敏感度" -> 对受影响产品标隔离、不受影响产品标放行 -> `draft_isolation_card`。隔离集人确认后走返工 / 返修上下文应用服务下达。

#### C. TraceabilityAgent（客诉根因追溯，嵌入 L1）

把 L1 诊断图作为子图调用。版本钉死由 ACL 代码做（`routeVersion` 强制过滤，从过点记录取当时生产用的版本，不会用当前版本套历史）；agent 在此之上汇聚跨上下文证据，做 5M1E 假设排序 + 置信度。隔离判定并行派给 B 或单独 gate。8D 草拟交 D。

#### D. DraftAgents（生成类）

三个 sub-agent（`draft_sop` / `draft_8d` / `draft_rework_craft`），各自基于工艺 diff / 追溯链 / 不良模式 + 历史案例库，草拟文档。**这是 agent 价值最干净的一类——开放生成，代码完全做不了**。草稿人确认后走对应上下文应用服务发布。

### 5.3 会话与状态

```sql
l3_session
  - session_id (PK)
  - scenario (CHANGEOVER / FAULT_RESPONSE / COMPLAINT_8D / PROCESS_CHANGE)
  - work_order_id / batch_id / asset_id   -- 按场景填
  - tenant_context (JSON)
  - status (PLANNING / RUNNING / SUSPENDED / DONE / FAILED)
  - current_step
  - created_at / updated_at

l3_step_record
  - record_id (PK)
  - session_id (FK)
  - step
  - node_type (CODE / AGENT)              -- 区分代码节点与 agent 节点
  - capability (A/B/C/D / NULL for code)  -- agent 节点填
  - status (PENDING / RUNNING / GATE_WAITING / CONFIRMED / FAILED)
  - action_card_payload (JSON)
  - agent_hypothesis (JSON)               -- agent 产出的根因/隔离集/假设
  - agent_confidence (high/medium/low)
  - gate_decision (PASS / REJECT / RETRY)
  - gate_decided_by / gate_decided_at
  - tool_call_traces (JSON)               -- trace_id 列表
  - occurred_at
```

- `node_type` 字段把代码节点与 agent 节点区分落库——可观测时可统计"本次会话调了几次 LLM"。
- agent 产出的 `agent_hypothesis` + `agent_confidence` 落库，事后可审计根因推理过程。
- gate 决策落库，谁确认的、什么时候、基于哪张卡、agent 假设是什么，全可审计。

### 5.4 动作卡（confirmation gate 的载体）

```python
class ActionCard(BaseModel):
    card_id: str
    session_id: str
    step: str
    capability: str | None         # 来自哪个 agent 能力，None 表示代码节点直出
    intent: str                    # "激活工艺路线 RR-100 v4" / "下达批次隔离"
    draft_payload: dict            # 草稿内容（intent + draft，非已落库数据）
    writes_via: str                # "工艺管理上下文.application.activate_route"
    requires_confirmation: bool = True
    evidence: list[str]            # trace_id 列表，给确认人看证据
    agent_hypothesis: dict | None  # agent 产出的根因/隔离集/假设（代码节点为 None）
    confidence: str | None         # agent 置信度
    risk_note: str
    deadline: datetime | None
```

- 卡片含 `evidence` + `agent_hypothesis` + `confidence`，确认人点开可回溯 agent 的推理与工具调用，**基于证据确认而非盲批**。
- 人确认后，写落库**不发生在 agent 进程**——confirmation token 交给 `writes_via` 指向的上下文应用服务落库。

### 5.5 写路径：不旁路应用服务

以"批次隔离下达"（B 能力产出）为例：

```text
FaultImpactAgent
  -> draft_isolation_card 工具（生成 ActionCard，requires_confirmation）
  -> gate 中断，推卡给质量工程师
  -> 工程师查看 agent_hypothesis（隔离集 + 敏感度理由）+ evidence，确认
  -> agent 拿 confirmation token
  -> 调返工/返修上下文 REST: POST /api/isolation-orders
       （返工上下文的应用服务接口，不是直改表）
  -> 返工上下文 application 层：
       IsolationAggregate.issue(batch_set, reason)  -- 聚合根不变式校验
       -> 事务发件箱落 BatchIsolated 事件
  -> agent 收到应用服务返回成功，gate.decision=PASS，续跑
```

- Agent 全程不碰 MES 原始表，写动作过聚合根不变式 + 事务发件箱——与正规写路径完全一致，只是触发源从"人点按钮"变成"Agent 草拟 + 人确认"。

### 5.6 过点红线（代码层，agent 不碰）

- **放行能力不注册到任何 capability**：工具集里不存在放行 / 拦截类工具，启动断言校验（§7.5）。
- **放行卡是代码节点直出**：`draft_release_card` 是结构化拼装（非 LLM），仅生成放行意图卡。
- **实际放行走过点应用服务**：人确认放行卡后，走过点执行上下文正常应用服务（过点主事务 + 规则引擎判定，P99 ≤200ms），agent 不进过点主事务。
- **barrier 前置防错**：supervisor 在放行前 barrier 等所有 gate PASS，避免在核对未完成时推放行卡——这是编排层防错（确定性代码）。

### 5.7 ACL 防腐层

复用 L1 的 ACL 模式（[L1 §5.4](../L1诊断型Agent/L1诊断型Agent-实现方案.md)）。代码节点的查询 ACL 与 agent 能力的工具 ACL 共用客户端，但代码节点直接调方法、agent 能力经 ToolNode 调用。写工具的 ACL 带 confirmation token：

```python
class ReworkWriteAclClient:
    """返工上下文写 ACL：只接受带 confirmation token 的隔离/返工请求。"""

    async def issue_isolation(
        self, batch_set: list[str], reason: str,
        confirmation: ConfirmationToken, tenant: TenantContext,
    ) -> IsolationResult:
        if not confirmation.valid_for(f"isolation.issue:{tenant.tenant_id}"):
            raise PermissionError("confirmation token 无效或已过期")
        resp = await self._http.post(
            "/api/isolation-orders",
            json={"batches": batch_set, "reason": reason, "confirmation_id": confirmation.id},
            headers=tenant.headers(),
            timeout=3.0,
        )
        resp.raise_for_status()
        return IsolationResult.model_validate(resp.json())
```

- confirmation token 绑定具体写动作（`action:target`），防篡改、防重放、带过期。
- ACL 不信任 agent 传的任意字段，按上下文应用服务的契约严格校验。
- 版本钉死在 ACL：`query_process_route` 强制 `route_version` 入参，校验返回 `ACTIVE`（继承 L1 §4.3）。

---

## 6. 推荐包结构（在 L1 基础上扩展）

```text
agent_service/
  app/
    api/
      l3_router.py               # 新增：POST /agent/l3/{scenario}/start, /confirm
      diagnosis_router.py        # L1
      schemas.py
    application/
      l3_orchestrator.py         # 新增：构建 supervisor 图、驱动编排
      diagnosis_service.py       # L1
      session_manager.py
      action_card_dispatcher.py  # 新增：推卡片（WebSocket + Kafka）
    domain/
      session.py
      report.py
      tool.py                    # 扩展：capability / requires_confirmation / WriteToolGate
      tenant.py
      l3_state.py                # 新增：L3Session / L3Step / ActionCard / GateDecision / AgentHypothesis
    orchestration/               # 新增：编排代码层 + agent 能力
      supervisor_graph.py        # supervisor StateGraph（代码节点 + agent 调用节点）
      code_nodes/                # 代码节点（确定性，不调 LLM）
        plan.py
        query_compare.py         # 钢网/程序/齐套的结构化比对
        barrier.py
        gate.py
        write_via_appservice.py
      agents/                    # 4 类 agent 能力（非确定，调 LLM）
        root_cause_agent.py      # A
        fault_impact_agent.py    # B
        traceability_agent.py    # C（嵌入 L1 诊断图）
        draft_agents.py          # D（SOP / 8D / ReworkCraft）
      scenarios/                 # 各场景的图装配
        changeover_graph.py      # 场景①
        fault_response_graph.py  # 场景②
        complaint_8d_graph.py    # 场景③
        process_change_graph.py  # 场景④
    infrastructure/
      ai/
        graph_builder.py         # L1 诊断图
        llm_factory.py
      acl/
        pass_execution.py        # L1
        process_management.py
        material.py
        device_data.py
        tooling.py               # 钢网夹具 / 设备程序 ACL（含借还记录 / 主数据变更）
        equipment_telemetry.py   # 新增：设备遥测 ACL
        rework.py                # 新增：返工 / 隔离写 ACL
      rag/
      kafka/
        listeners.py
        action_card_producer.py
      persistence/
        models.py                # 扩展 l3_session / l3_step_record
        session_repo.py
        trace_repo.py
        l3_repo.py               # 新增
        checkpointer.py
      redis_/
        tool_cache.py
        confirmation_store.py
      obs/
        tracing.py
        metrics.py
    config.py
    main.py
  tests/
  pyproject.toml
```

- `orchestration/code_nodes/` 与 `orchestration/agents/` 物理分层——代码节点与 agent 能力分开落点，避免混淆。
- `orchestration/scenarios/` 是各场景的图装配，复用同一套 code_nodes + agents，只是组合不同。

---

## 7. 关键代码骨架

### 7.1 supervisor 编排（代码节点 + agent 调用节点 + barrier + gate）

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

        # 代码节点
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

        # agent 节点（仅 mismatch 分支触发）
        g.add_node("root_cause", self._run_agent("root_cause"))          # A
        g.add_node("gate_disposition", self._gate("DISPOSITION"))

        g.add_edge(START, "plan")
        g.add_edge("plan", "first_article")
        g.add_edge("first_article", "gate_first_article")
        g.add_edge("gate_first_article", "process_switch")
        g.add_edge("process_switch", "gate_process_switch")
        # 工艺确认后并行派发钢网程序比对 + 齐套比对（两条并行边）
        g.add_conditional_edges("gate_process_switch", lambda s: ["tooling_check", "kitting_check"])
        g.add_edge("tooling_check", "barrier")
        g.add_edge("kitting_check", "barrier")
        # barrier 按结果分流：都 PASS -> 放行；tooling mismatch -> A；缺料 -> 挂起
        g.add_conditional_edges("barrier", self._barrier_route, ["draft_release", "root_cause", "suspend"])
        g.add_edge("draft_release", "gate_release")
        g.add_edge("gate_release", "done")
        g.add_edge("root_cause", "gate_disposition")
        g.add_conditional_edges("gate_disposition", lambda s: ["tooling_check"] if s["retry_tooling"] else ["done"])
        g.add_edge("done", END)
        return g.compile()

    async def _barrier_node(self, state: L3State) -> L3State:
        # 代码：等并行分支汇合，按结构化结果分流（确定性，非 agent 判定）
        t, k = state["tooling_result"], state["kitting_result"]
        if t["status"] == "PASS" and k["status"] == "PASS":
            state["barrier_route"] = "draft_release"
        elif t["status"] == "FAIL":           # 钢网/程序 mismatch -> 交 agent A
            state["barrier_route"] = "root_cause"
            state["expected"] = t["expected"]; state["actual"] = t["actual"]
            state["mismatch_code"] = t["code"]
        else:                                  # 缺料 -> 确定性，不嵌 agent，直接挂起催料
            state["barrier_route"] = "suspend"
            state["status"] = "SUSPENDED"
            await self._gates.push_exception_card(state, "物料齐套未达标，请催料")
        return state

    def _barrier_route(self, state: L3State) -> str:
        return state["barrier_route"]

    def _run_agent(self, capability: str):
        async def fn(state: L3State) -> L3State:
            sub = self._agents.get(capability)              # 嵌入对应 agent 能力
            result = await sub.ainvoke(state, config={"configurable": {"thread_id": state["session_id"]}})
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

- **agent 只在 mismatch 分支触发**：`_barrier_node` 的分流是确定性代码——PASS 走放行、mismatch 走 A、缺料挂起。换线全程 PASS 时 A 根本不执行。
- **重检回路**：A 草拟处置 -> 人确认 -> 处置落库 -> 回 `tooling_check` 重检（代码节点），agent 不参与"重检通过没"的判定。

### 7.2 confirmation gate（interrupt / resume）

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
        confirmation = await interrupt(value=card)         # 阻塞直到 /confirm resume
        if not confirmation.valid_for(card.writes_via_action()):
            return "REJECT"
        return "PASS" if confirmation.approved else "REJECT"
```

```python
# app/api/l3_router.py
@router.post("/agent/l3/{session_id}/confirm")
async def confirm_gate(
    session_id: str, req: ConfirmRequest,
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

### 7.3 应用服务编排（异步驱动 + 超时兜底）

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
                    config={"recursion_limit": 40,
                            "configurable": {"thread_id": session.id}},
                ),
                timeout=3600.0,   # 含人确认等待的整体上限
            )
        except (GraphRecursionError, asyncio.TimeoutError) as e:
            await self._sessions.mark_failed(session, str(e))
```

- `asyncio.create_task` + `interrupt`：L3 流程是长流程（含人工确认），HTTP 请求只负责"启动"，不阻塞等待全程。
- `recursion_limit=40` 与 `timeout` 形成"步数 + 时长"双闸门，超限标 FAILED 转人工。

### 7.4 代码节点示例（结构化比对，不调 LLM）

```python
# app/orchestration/code_nodes/query_compare.py
class QueryCompareNodes:
    """确定性查询比对节点：不调 LLM，直接 ACL 调用 + 结构化比对。"""

    def __init__(self, tooling_acl: ToolingAclClient, route_acl: ProcessManagementAclClient,
                 repo: L3Repo) -> None:
        self._tooling_acl = tooling_acl
        self._route_acl = route_acl
        self._repo = repo

    async def query_and_compare_tooling(self, state: L3State) -> L3State:
        # 1. 查工艺路线 v4 里的钢网号 / 程序号（ACL 强制 route_version）
        route = await self._route_acl.query_route(
            state["target_route_id"], state["target_route_version"], state["tenant"])
        expected_stencil = route.tooling.stencil_id
        expected_program = route.tooling.program_id

        # 2. 查产线当前钢网（扫码读到）+ 设备本地程序版本
        actual_stencil = await self._tooling_acl.query_current_stencil(state["asset_id"])
        actual_program = await self._tooling_acl.query_local_program_version(state["asset_id"])

        # 3. 结构化比对（确定性，非 agent）
        if actual_stencil != expected_stencil:
            state["tooling_result"] = {
                "status": "FAIL", "code": "TOOLING_STENCIL_MISMATCH",
                "expected": expected_stencil, "actual": actual_stencil,
            }
        elif actual_program != expected_program:
            state["tooling_result"] = {
                "status": "FAIL", "code": "PROGRAM_VERSION_NOT_ACTIVE",
                "expected": expected_program, "actual": actual_program,
            }
        else:
            state["tooling_result"] = {"status": "PASS"}
        await self._repo.save_step(state["session_id"], "TOOLING_CHECK", "CODE", state["tooling_result"])
        return state
```

- 这是**代码节点**：query + compare，产出结构化结果（`expected/actual/code`），不调 LLM。mismatch 检出后，agent A 拿这个结构化结果去做根因推理。

### 7.5 启动断言（红线）

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
    # 3. 任何 capability 下均无放行/拦截类工具
    for cap in ("root_cause", "fault_impact", "traceability", "draft_sop", "draft_8d", "draft_rework_craft"):
        tools = reg.tools_for(cap, ANY_TENANT)
        assert not any(t.name.startswith(("pass_judge", "force_release", "release_")) for t in tools), cap
    # 4. 各 capability 工具集互斥
    reg.assert_capability_partition()
    yield
```

---

## 8. 可观测性与兜底

> **完整设计见** [可观测性方案](../Agent可观测性-设计与实现方案.md)（L1/L2/L3 共用可观测底座，事实源唯一）。本节仅列 L3 特有点索引。

L3 复用 L1 的可观测底座，层级特有点对应新文档章节：

- **特有指标**：`l3_session_total` / `l3_node_total`（node_type=CODE/AGENT）/ `l3_llm_invocation_total`（换线 PASS 时为 0）/ `l3_step_latency_seconds` / `l3_gate_decision_total` / `l3_agent_confidence_total` / `l3_suspended_total` / `l3_write_tool_total` / `l3_write_rejected_total` -> 新文档 §5.2 L3 新增。
- **链路**：一个会话一个 `trace_id`，动作卡带 `evidence`（trace_id 列表）+ `agent_hypothesis` -> 新文档 §7。
- **特有兜底**：gate 超 deadline 挂起 `SUSPENDED`；barrier 未 PASS 禁推放行卡；agent 置信度 `low` 标 `need_human_review` 不自动路由；写工具未带 token 被 `WriteToolGate` 拒（P0 告警）；agent 连续失败 2 次挂起 -> 新文档 §12。

---

## 9. 实现步骤

### 阶段一：编排代码层骨架（3 周）

1. 在 L1 的 `agent_service` 上扩展 `orchestration/code_nodes/`，搭 supervisor + 代码节点（plan / query_compare / barrier / gate / done）。
2. 实现 `ToolDescriptor.capability` / `requires_confirmation` / `writes_via` 与 `WriteToolGate` 启动断言。
3. 实现 `l3_session` / `l3_step_record` 表 + repo（含 `node_type` 区分）。
4. 接 LangGraph `interrupt` / `Command(resume=...)`，跑通单个 gate 的"推卡 -> 确认 -> 续跑"。
5. WebSocket + Kafka 动作卡推送 MVP。
6. 换线场景跑通**纯代码骨架**（全程 PASS 路径，无 agent），验证"代码能跑的不交给 LLM"。

### 阶段二：agent 能力 A 嵌入（2 周）

7. 实现 RootCauseAgent (A) + 其只读 toolset（钢网借还 / 主数据变更 / 工艺审计 / 上工单收线记录）。
8. supervisor 加 mismatch 分支：barrier 分流到 A -> 处置 gate -> 重检回路。
9. 评测 A 的根因假设准确率 + 置信度标定，低置信度转人工。

### 阶段三：agent 能力 B / C / D + 写路径（4 周）

10. FaultImpactAgent (B)：设备遥测 ACL + FMEA 关联 + 隔离集草拟，故障场景图装配。
11. TraceabilityAgent (C)：嵌入 L1 诊断图作为子图，客诉场景图装配。
12. DraftAgents (D)：SOP / 8D / 返工工艺草拟，工艺变更 / 客诉场景图装配。
13. 各上下文写 ACL（隔离下达 / SOP 发布 / 返工工艺下达），调对应应用服务 REST。
14. `ConfirmationStore`（redis，token 绑定 `action:target` + 过期）。

### 阶段四：试点与加固（2 周）

15. 挑一条产线灰度换线 Agent（先代码骨架 + A），confirmation gate 做扎实。
16. 接 OTel + prometheus 指标（[可观测性方案](../Agent可观测性-设计与实现方案.md) §5.2），重点观察 `l3_llm_invocation_total` 与 `l3_node_total{node_type=CODE}` 占比。
17. 评测：根因假设准确率、隔离集命中率、8D 草稿采纳率、gate 拒绝率。
18. 沉淀场景评测集，回归提示词 / 模型变更。

---

## 10. 约束落地检查清单

- [ ] supervisor 注册的工具集为空，启动断言校验。
- [ ] 各 capability 工具集互斥（`assert_capability_partition`），越界工具不注册。
- [ ] **任何 capability 下均无放行 / 拦截类工具**（`pass_judge` / `force_release` / `release_*` 前缀）。
- [ ] 所有写工具 `requires_confirmation=True` 且声明 `writes_via`，否则启动失败。
- [ ] 写落库走各上下文应用服务 REST，过聚合根不变式 + 事务发件箱，不旁路写。
- [ ] confirmation token 绑定 `action:target`，带过期，防篡改防重放。
- [ ] barrier 在放行前等所有 gate PASS，未 PASS 分流到 agent A 或挂起，不推放行卡。
- [ ] gate 决策（谁 / 何时 / 基于哪张卡 / agent 假设）落 `l3_step_record` 可审计。
- [ ] agent 不调过点引擎放行 / 拦截 API，过点 P99 ≤200ms 判定仍走规则引擎。
- [ ] `query_process_route` 强制 `route_version`，ACL 校验返回 `ACTIVE`（继承 L1）。
- [ ] **代码节点（query/compare/barrier/gate）不调 LLM**，`node_type=CODE` 落库可验。
- [ ] agent 置信度 `low` 时处置卡标 `need_human_review`，不自动路由。
- [ ] 工具调用前按 `TenantContext` 权限过滤，agent 只见本 capability toolset。
- [ ] 每步带 trace，动作卡含 evidence + agent_hypothesis，OpenTelemetry 透传 `traceparent`。
- [ ] gate 等待有 deadline，超时挂起不无限阻塞；agent 连续失败 2 次挂起转人工。

---

## 11. 面试防守 Q&A

**Q：为什么从"5 个换线 sub-agent"改成"编排代码层 + 4 类 agent 能力"？**
A：旧设计把换线拆成 5 个 sub-agent，其中 4 个是"查钢网号、查齐套率、查程序版本"的确定性查询比对——`expected == actual` 的结构化判定，LLM 在这里是负资产（非确定性进过点红线附近是风险）。重写后用一个标准判断："输入是否开放 / 是否需推理生成 / 分支是否难穷举"，三问皆否走代码节点，三问有一才走 agent。于是换线 5 步里编排、结构化比对、barrier、gate、写落库全是代码节点；agent 只在钢网 / 程序 mismatch 时触发 A（根因推理）。换线全程 PASS 时 LLM 调用次数为 0——这是"懂什么时候不用 AI"的体现，比硬塞 5 个 agent 得分高。

**Q：multi-agent 怎么保证不越界进过点主事务？**
A：三层兜底。第一层，supervisor 不持任何工具，纯代码编排器，想越界也没工具调。第二层，工具按 `capability` 裁剪，放行 / 拦截类工具**不注册到任何 capability**——A/B/C/D 四能力工具集里根本不存在放行工具。第三层，启动断言：任何 capability 下不得有 `pass_judge` / `force_release` / `release_*` 前缀工具。实际放行由人确认放行卡后走过点上下文正常应用服务，过点 P99 ≤200ms 判定仍走规则引擎。

**Q：写动作怎么保证不旁路应用服务？**
A：写工具必须声明 `requires_confirmation` 和 `writes_via`（落库走哪个应用服务），否则启动断言失败。Agent 生成的是 intent + draft（动作卡，含根因假设 + 证据），人确认后拿 confirmation token，调对应上下文的应用服务 REST——比如隔离下达调 `POST /api/isolation-orders`，过聚合根不变式 + 事务发件箱。Agent 全程不碰 MES 原始表，触发源从"人点按钮"变成"Agent 草拟 + 人确认"，写路径完全不变。

**Q：代码节点和 agent 节点怎么区分？怎么保证代码节点不偷偷调 LLM？**
A：物理分层——`orchestration/code_nodes/` 是纯 Python 函数节点，`orchestration/agents/` 是带 LLM 的 subgraph。代码节点（query/compare/barrier/gate/draft_release_card）直接调 ACL，不经过 ToolRegistry，不调 LLM。落库时 `l3_step_record.node_type` 标 CODE/AGENT，指标 `l3_node_total{node_type=CODE}` 可量化代码节点占比。换线全程 PASS 时 `l3_llm_invocation_total=0`，可观测验证"该用代码的没用 AI"。

**Q：钢网 mismatch 的处置，规则引擎真的做不了吗？写个决策树不行？**
A：能写，但维护成本高且永远落后。mismatch 的根因不止"拿错"——上工单未还库、台账改名、借出未登记、工艺录错、扫码制式不同……每出现一种新现场情况，决策树就要补一行分支，还要补对应的跨上下文联合查询。更难的是判定阈值是启发式的，新情况一来就漏。agent 的优势不是"比决策树聪明"，而是**取证路径由中间结果驱动**——查到"ST-A 借出未还"后自适应去查上工单收线记录，而不是固定 JOIN。新根因出现时，只要取证能查到证据，agent 就能给假设，决策树不用改。这是把"维护一棵永远落后的决策树"降成"维护一组只读 toolset"。

**Q：换线防错如果代码（barrier + 应用服务不变式）就能做，为什么还要上 agent？**
A：该问。判断标准是"这一步输入是否开放、是否需要推理 / 生成"。换线里：步骤顺序、齐套率判定、钢网号比对、程序版本校验、barrier 未双 PASS 不放行——全是确定规则，**代码做，agent 不掺和**，我把这层划给代码节点 + barrier。agent 只在四个地方赚回成本：① mismatch 后推理根因 + 草拟处置（A），根因分支组合爆炸；② 故障隔离范围动态判定（B），故障模式 × 漂移窗口 × 敏感度三维动态；③ 客诉根因 5M1E 假设排序（C），跨源语义关联 + 加权推理；④ SOP/8D/返工工艺草拟（D），开放生成。如果某场景全程确定，那就不该上 agent，上个工作流引擎就够了。

**Q：confirmation gate 怎么实现人在回路？**
A：用 LangGraph 的 `interrupt`。supervisor 跑到 gate 节点时调 `interrupt(value=action_card)` 暂停，state 落 MySQL（checkpointer），进程不阻塞。FastAPI 暴露 `/confirm` 端点，人确认后调 `graph.ainvoke(Command(resume=token))` 续跑。confirmation token 绑定 `action:target` 带过期，防篡改防重放。gate 决策（谁 / 何时 / 基于哪张卡 / agent 假设）落 `l3_step_record`，全可审计。动作卡带 `evidence` + `agent_hypothesis`，确认人基于证据确认而非盲批。

**Q：agent 失败了怎么办？会卡住产线吗？**
A：故障隔离。agent 连续失败 2 次标 `SUSPENDED`，推异常卡给责任人，不自动重试到死。agent 置信度 `low` 时处置卡标 `need_human_review`，列疑点不自动路由。barrier 检测到未 PASS 禁止推放行卡，直接挂起。gate 等待有 deadline，超时自动挂起不无限阻塞。最坏情况是流程退回人工编排，不会越界写、不会错误放行——和 MES 防错理念一致，宁可拦下让人判。

**Q：为什么用 LangGraph supervisor 而不是 AutoGen / CrewAI？**
A：L3 是有严格步骤顺序的编排，不是多 agent 对话。LangGraph 的 `StateGraph` 把串行 / 并行 / barrier / gate 做成显式图，且节点可以是任意 Python 函数——代码节点（query/compare/barrier）不消耗 LLM 调用，agent 节点才调 LLM，两类节点同图混合编排。AutoGen 的 conversational 模式适合对话协作，难做严格步骤编排；CrewAI 抽象偏高，难做细粒度权限拦截与 gate 状态机。LangGraph 离原语更近，且与 L1 同构，复用底座。

---

## 12. 一句话定位

"L3 编排型 Agent = **编排代码层（确定性）+ 4 类非确定 agent 能力（A 根因处置 / B 故障隔离范围 / C 客诉根因追溯 / D 生成类）**——supervisor StateGraph 里代码节点（plan / query+compare / barrier / gate / write）和 agent 调用节点混合编排，代码节点不调 LLM，agent 只在非确定决策点触发（换线全程 PASS 时 LLM 调用为 0）；supervisor 不持工具、放行能力不注册到任何 capability，能力从架构层切掉；写动作过 confirmation gate（interrupt / resume + 绑定 action:target 的 token），落库走各上下文正常应用服务过聚合根不变式 + 事务发件箱，绝不旁路写、不进过点主事务——agent 只在'核对异常推理根因 / 故障隔离范围动态判定 / 客诉根因 5M1E 排序 / 生成类草拟'这 4 类代码做不了或做起来极复杂的非确定段赚回成本，硬防错与过点判定仍是代码 / 规则引擎的活，写的闸门始终在人手里。"
