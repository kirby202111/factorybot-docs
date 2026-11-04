# FactoryAgent 流程编排能力（跨上下文长流程，人在回路）

> 本文是 [FactoryAgent-总体设计与实现方案.md](FactoryAgent-总体设计与实现方案.md) 中**流程编排能力（`orchestration`）**的落地展开。
> **核心形态**：编排 = **编排代码层（确定性）** + **在非确定决策点调用诊断 / 草稿能力**。代码层做所有"步骤顺序固定、判定规则固定、输入结构化"的活（编排 / 结构化比对 / barrier 硬防错 / confirmation gate / 写落库 / 版本钉死 / 放行能力隔离）；诊断与草稿能力只在代码做不了或做起来极复杂的决策点介入。
> **口径纪律**：写动作**过 confirmation gate**，落库走各上下文**正常应用服务**，绝不旁路写；**不进过点主事务**（[领域总览.md](../../领域模型/领域总览.md) §5.3），过点 P99 ≤200ms 判定仍走规则引擎；不编业务效果数字。
> **配套**：[诊断能力](FactoryAgent-诊断能力.md)、[草稿编写能力](FactoryAgent-草稿编写能力.md)、[现场痛点与能力嵌入点](FactoryAgent-现场痛点与能力嵌入点.md)、[能力边界与选型决策卡](FactoryAgent-能力边界与选型决策卡.md)。

---

## 0. 编排能力不做"第二套 Agent"

早期设计把编排拆成 5 个换线 sub-agent（首件 / 工艺 / 钢网程序 / 物料 / 放行），看似 multi-agent，实则其中 4 个是确定性查询比对——齐套率是不是 100%、钢网号等不等于工艺路线里的、程序版本是不是 ACTIVE，全是 `expected == actual` 的结构化判定，LLM 在这里是负资产。再后来又把"根因 / 隔离范围 / 追溯 / 生成"做成 4 类独立 sub-agent，于是根因推理、5M1E 排序、8D 生成这些逻辑与诊断能力、草稿能力形成了**两套实现**。

现在只有一条规则：

**编排 flow 里需要"判根因"就调诊断能力，需要"产出可下达的卡"就调草稿能力，需要"确定性判定"就写普通代码。编排层自己不实现第四种推理。**

| 步骤类型 | 例子 | 归属 | 实现 |
|---|---|---|---|
| 顺序 / 并行 / 汇合 | 换线 5 步编排 | 代码 | 应用层顺序 `await`；并行用 `asyncio.gather` 并发各自独立 `Agent.reply()`，汇合在应用层显式合并 |
| 结构化比对 | 齐套率 == 100%、钢网号匹配、程序版本 == ACTIVE | 代码 | `query_compare` 确定性步骤 |
| 硬防错 | 未双 PASS 不放行 | 代码 | `barrier` 确定性步骤 |
| 写落库 | 激活工艺、下达隔离、发布 SOP | 代码 | gate + 上下文应用服务 REST |
| 版本钉死 | 工艺查询强制版本锚点 | 代码 | ACL 校验 |
| 放行能力隔离 | 不给任何能力放行 API | 代码 | 工具禁注册 + 启动断言 |
| 动作卡推送 + 待办与超时升级 | gate 等人确认、卡住催办 | **MES 工单/审批流** | dispatcher 推卡 + 待办实体（催办与 deadline 由审批流承担，不在 agent 侧） |
| **核对异常根因分支** | mismatch 后判台账错 / 工艺录错 / 产线拿错 | **诊断能力** | `await` 诊断能力（结构化异常入参；`invoke` 只是本仓库的应用层调用语法，不是框架关键字） |
| **故障隔离范围** | 故障模式 × 漂移窗口 × 产品敏感度 | **诊断能力 + 草稿能力** | 取证判范围 → 出隔离集卡 |
| **客诉根因排序** | 5M1E 假设加权排序 + 跨源语义关联 | **诊断能力** | 同上，换入参 |
| **生成类** | SOP / 8D / 返工单 / 处置卡草拟 | **草稿能力** | `await` 草稿能力（draft_kind=...） |

**为什么不把"现在该查什么"交给模型编排**：编排步骤的串行 / 并行 / barrier / gate 在本仓库的 flow 代码里是显式 `await` 与 `asyncio.gather`，可审计、可回归、可断言；用纯 prompt 让模型自己编排，易丢步骤、难审计，而 MES 的漏步骤等于带病生产。

---

## 1. 定位与边界

### 1.1 这项能力解决什么

把现场跨上下文长流程里**非确定的那段**（根因处置 / 隔离范围判定 / 根因追溯 / 生成草拟）从"人脑拍 + 电话串 3 个系统 + 手写"升级为"Agent 取证 + 推理 + 草拟 + 人确认落库"；**确定性那段**（编排 / 比对 / 硬防错 / 写落库）用代码骨架做扎实。

### 1.2 能力节点（非确定步骤）的统一形态

flow 里所有 LLM 能力节点都长成同一种形状：**取证 → 结构化 → 交给草稿能力 → gate**。

```
[能力节点]  拿到结构化上下文（mismatch / 故障 / 批次 / 工艺 diff）
    ↓ await diagnosis_agent.reply(...)      -- 应用层调用，非框架移交原语；需要假设与排序时；纯生成的场景可跳过
[诊断能力]  多步只读取证 → 根因假设 + 置信度 + evidence_set_id + 版本锚点
    ↓ await drafting_agent.reply(...)       -- 需要可下达的产物时
[草稿能力]  按 evidence_set_id 回查关系型证据子集 + 模板 → Draft（requires_confirmation=True）
    ↓
[gate 步骤] 推动作卡 + 落成 MES 工单/待办实体 → **本次调用结束** → 人审批确认 → 回调重新发起下一段（重查当下事实）→ 写走上下文应用服务
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
| **写动作 confirmation gate** | 写工具 `requires_confirmation`，产 intent + draft，人确认后落库 | 工具网关**一次性动作令牌**为主闸（绑定 `action:target` + 用户，用后即废，过期 fail closed）；`gated_write_policy` + `ToolPolicyGate` + `confirmation_store` 为叠加层 |
| **写不旁路应用服务** | 不直写 MES 原始表 | 写工具声明 `writes_via`，走应用服务 REST，过聚合根不变式 + 事务发件箱 |
| **版本一致性** | 工艺查询强制版本锚点 | ACL 校验 `ACTIVE`（与诊断能力共用） |
| **协同范围** | `flows/` 只做**一次调用内的协同**（顺序 `await` + `asyncio.gather`）；**不做挂起/续跑**：到 gate 就产待办并返回。人工确认的等待、催办、超时、重试与补偿、审计时间线归 **MES 工单/审批流与工作流引擎**（ADR-05） | flow 不持有流程推进状态，也不保存挂起态；确认后由回调重新调用下一段，该段**重查当下现场事实** |
| **能力切片** | 每个能力节点只见它该见的工具 | `tools_for(capability, tenant)` |
| **硬防错** | 未双 PASS 不放行 | `barrier` 确定性校验，非 LLM 判定 |
| **确定性步骤不调 LLM** | query / compare / barrier / gate / draft_release_card | 纯 Python 步骤，`node_type=CODE` 落库可验 |
| **可观测兜底** | 每步 + 每个 gate 带 trace 与置信度，低置信度转人工 | 共用底座 |

---

## 2. 编排形态：`flows/` 代码 flow（应用层 Python）

```text
FastAPI ── POST /agent/orchestration/{scenario}/start ──▶ OrchestrationService
        ── POST /agent/orchestration/{session_id}/advance（审批后回调，起下一段）  │
                                                                   ▼
                     ┌──────────────────────────────────────────────────────┐
                     │ flows/changeover.py（代码编排层，不持工具、不调 LLM）    │
                     │  确定性步骤：plan / query+compare / barrier / gate / write│
                     │  能力节点：按需 await 诊断 / await 草稿                  │
                     └──┬─────────────┬─────────────┬─────────────┬─────────┘
                        │             │             │             │
                        ▼             ▼             ▼             ▼
                 diagnosis 循环  diagnosis 循环  drafting 链  drafting 链
                 (根因/追溯)     (隔离范围)      (处置卡)      (8D / SOP)
                        │             │             │             │
                        └─────────────┴──────┬──────┴─────────────┘
                                             ▼
                     ToolRegistry（按能力切片 + 写工具声明 + 启动断言）
                                             │
              ┌──────────────┬───────────────┴──────────────┬──────────────┐
              ▼              ▼                              ▼              ▼
         ACL 只读        ACL 只读                       ACL 写（带令牌）  RAG
         （各上下文）    （遥测 / FMEA / 台账）           -> 应用服务 REST
                                             ▲
                          动作卡 -> MES 工单/待办    │
              事件流(AGUI/SSE) + Kafka -> 责任人 UI -> 在工单上审批 -> /advance
```

**关键设计决策**：

- **flow 编排层不持工具、不调 LLM**：只做 plan / dispatch / barrier / gate。想越界也没工具调。
- **确定性步骤与能力节点物理分层**：`ai/flow_steps/`（确定性）与 `ai/capability_nodes/`（能力调用）分目录，评审与回归可分别覆盖。
- **gate 是显式确定性步骤，但是个"终点"不是"挂起点"**：推动作卡 + 落成 MES 工单/待办实体 → 本次调用返回；人审批确认后由回调**重新发起下一段**（新一次调用，短后台任务由 `BackgroundTaskManager` 托管）；gate 决策与令牌落库可审计。**不依赖 `SessionService` 载回快照续跑**，因为下一段重查当下事实更正确（MES 数据时变）。
- **barrier 是防错层不是决策层**：并行分支汇合后按结构化结果**确定性分流**（都 PASS → 放行卡；mismatch → 诊断能力；缺料 → 产催料待办后结束本段）。
- **⚠️ 并行分支不互看结论**：并行用 `asyncio.gather` 并发**各自独立**的 `Agent.reply()`，每个分支独立预算 / 独立超时 / 独立权限档位，只在应用层显式合并；单场景并行分支数受 `max_fanout ≤ 5` 约束；各分支读同一份 `evidence_set_id`（关系型证据子集），**不共享消息流、不看对方的中间结论**。原因：无交叉验证的扇出式互看把错误放大 **17.2×**，中心化 4.4×；汇合点必须有确定性裁决（ADR-02 中心校验闸门）。框架侧也不提供广播原语（`MsgHub` 等在 2.0 已删除）。
- **回退开关常驻**：编排与取证的并行度按 `AGGREGATE_MODE` / `REPORT_MODE ∈ {single, probe_fanout, fanout}` 三档切换（ADR-12），默认可一键退回单 Agent；今天需要并发的场景，下一代模型可能单 Agent 就够。

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
  ├─ tooling FAIL ─▶ await diagnosis(结构化 mismatch)  ★诊断能力
  │                    ↓ await drafting(DISPOSITION_CARD)  ★草稿能力
  │                  [gate: DISPOSITION] 人确认 -> 处置落库 -> 回 tooling_check 重检
  │
  └─ kitting FAIL ─▶ [WAITING] 代码：缺料是确定的，不调能力，产催料待办推线长后本段结束
```

**两个"懂不用"的点**：缺料直接产催料待办、不调 LLM（确定性）；放行卡由代码结构化拼装（碰过点红线附近，不让 LLM 生成）。**换线全程 PASS 时 LLM 调用次数为 0**。

### 3.2 设备故障复产（场景②）

```text
[设备故障事件触发]
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行                  ▼并行                    │
[draft_repair_order]    await diagnosis(遥测+FMEA)  ★诊断能力：故障模式 + 漂移窗口 + 敏感度
  代码：结构化拼装维修单      ↓
  ↓                     await drafting(ISOLATION_CARD)  ★草稿能力
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

复校 / 复产首件两道红线是确定性步骤，**任何能力都不碰**。

### 3.3 客诉 8D（场景③）

```text
[客诉触发]
  ↓
await diagnosis(batch_no)    ★诊断能力：5M1E 假设排序 + 证据链
  ↓
  ├──────────────────────┬──────────────────────┐
  ▼并行(代码)            ▼并行                    │
[供应商批次追溯]         await diagnosis(敏感度) ★诊断能力（隔离范围）
  代码：版本钉死后查在库品     ↓
  ↓                      await drafting(ISOLATION_CARD) ★草稿能力
  └──────────────────────┴──────────────────────┘
  ↓
[gate: ISOLATION]  人确认 -> 返工上下文下达
  ↓ PASS
await drafting(EIGHT_D)   ★草稿能力：拉追溯链 + 历史 8D 草拟正文
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
await drafting(SOP)  ★草稿能力：开放生成        [资质核对]  代码：操作工资质 ∈ 工艺要求资质集？
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

> **场景②③④里的"并行"一律是同一种实现**：`asyncio.gather` 并发**各自独立**的 `Agent.reply()`（确定性分支就是普通并发函数），汇合后在应用层显式合并并过 barrier 裁决；分支之间不共享消息流，只读同一份 `evidence_set_id`。框架不提供广播/共享上下文原语，本设计也刻意不做——理由见 §2 与 ADR-02。

---

## 4. 写路径与 confirmation gate

### 4.1 写工具白名单

| 能力 | 只读工具（示例） | 写工具（`requires_confirmation=True` + `writes_via`） |
|------|-----------------|------------------------------------------------------|
| 诊断 | 过点 / WIP / 物料 / 设备 / 质量 / 关系型追溯与文档检索（全套只读） | 无 |
| 草稿 | `fetch_evidence_set`（回查关系型证据子集） / 文档检索 / 工艺版本 | 无 |
| 编排 | 各上下文只读 + 遥测 / FMEA / 钢网台账 | `draft_isolation_card` → 返工/返修上下文 · `draft_rework_order` → 返工上下文 · `draft_sop_publish` → 工艺管理上下文 |
| — | 放行 / 拦截类 | **永不注册** |

> 编排的写工具大多是"草拟 + 卡片"（落库走应用服务），**没有直写原始表的工具**。

### 4.2 confirmation gate 的分段与确认回调（**不做挂起 / 续跑**）

> 设计目标骨架，尚未落地。**本项目不在 agent 侧做挂起/恢复**：框架级 `interrupt` / `Command(resume=…)` 不存在，也不自建同语义的机制。gate 的语义是"**本段终点**"：产出动作卡并落成 MES 已有工单/待办实体后返回；审批确认回来时，下一段是**新的一次调用 + 重查当下事实**。
>
> 为什么不挂起：① gate 之间几乎全是确定性代码节点（换线快路径 LLM 调用为 0），挂起期没有任何需要保住的计算态；② MES 现场事实**时变**，恢复历史快照比重查一次更危险；③ 确认本身就是工单/审批业务，MES 已有实体可靠，状态机/责任人分派/催办/超时全在现成的审批流里（ADR-05）。

```python
# app/ai/flow_steps/gate.py
class GateNode:
    """闸门节点：产待办 + 拦住写工具，然后本次调用就结束。不挂起、不 await 人。"""

    def __init__(self, dispatcher: ActionCardDispatcher,
                 tickets: OrchestrationTicketClient) -> None:
        self._dispatcher = dispatcher
        self._tickets = tickets

    async def open_gate(self, session_id: str, step: str, card: ActionCard) -> GateOutcome:
        await self._dispatcher.push(card)              # 事件流（AGUI/SSE）+ Kafka 兜底
        # 待办落成 MES 已有工单/审批实体（维修单 / 隔离单 / 首件放行 / 8D / SOP）
        # 责任人分派、催办、超时升级均在该实体生命周期内，agent 侧不管
        ticket = await self._tickets.create(card, session_id=session_id, step=step)
        # ★ 本段 flow 到此返回：不保存挂起态，不占执行槽，进程可退出
        return GateOutcome(step=step, waiting_ticket=ticket.id, decision="WAITING")
```

```python
# app/api/orchestration_router.py
@router.post("/agent/orchestration/{session_id}/advance")
async def advance_after_approval(
    session_id: str, req: AdvanceRequest,
    perm: PermissionEngine = Depends(get_permission),   # 确认记录签发（Permission 只是叠加层，主闸在工具网关，ADR-04）
    flows: FlowRegistry = Depends(get_flows),
    background: BackgroundTaskManager = Depends(get_background),
) -> AdvanceResponse:
    """MES 工单审批完成后的回调：签发一次性令牌 + 重新发起下一段。"""
    token = await perm.record_confirmation(session_id, req.step, req.approved, req.user_id)
    if not req.approved:
        return AdvanceResponse(step=req.step, decision="REJECT")
    ctx = await flows.load_context(session_id)          # 只取会话标识与 scenario，不取历史快照
    facts = await flows.requery_live_facts(ctx)         # ★ 重查当下现场事实（首件/路线/钢网/齐套…）
    await background.submit(flows.run_next_segment(ctx, facts, token))  # 新的一次短调用，幂等可重入
    return AdvanceResponse(step=req.step, decision="PASS")
```

> 幂等与可重入：每段以 `session_id + step` 为幂等键，写路径另用 `confirmation_id` 作幂等键；进程中途死亡无需特殊处理，下一段照常重跑。同一次调用内、人就在线上的确认（换线首件/放行）仍可直接拿令牌往下走，不需要绕工单。

### 4.3 写落库时序（以批次隔离为例）

```text
编排 flow 的 fault_impact 能力节点
  -> await diagnosis(...)                根因 / 隔离范围
  -> await drafting(ISOLATION_CARD)      隔离集卡（requires_confirmation）
  -> gate 产出隔离待办（落成 MES 工单实体）+ 推卡给质量工程师，本段调用结束
  -> 工程师查看 agent_hypothesis（隔离集 + 敏感度理由）+ 按 evidence_set_id 回查的关系型证据子集，在工单上审批确认
  -> 审批完成回调 /advance -> 网关一次性动作令牌（绑定 isolation.issue:{tenant} + 具体用户，带过期，用后即废）
  -> 编排写 ACL: POST /api/isolation-orders（header: X-Confirmation-Token / X-Confirmed-By）
  -> 返工上下文 application 层：
       IsolationAggregate.issue(batch_set, reason)   -- 聚合根不变式校验
       -> 事务发件箱落 BatchIsolated 事件
  -> 收到成功，gate_decision=PASS 落 agent_step_record；下一段由本次调用继续或再次回调驱动
```

**Agent 全程不碰 MES 原始表**；写路径与"人点按钮"完全一致，只是触发源变成"Agent 草拟 + 人确认"。

### 4.4 过点红线（代码层）

- **放行能力从架构层切掉**：任何能力下不存在 `pass_judge` / `force_release` / `release_*` / `block_pass` 工具（启动断言）。
- **放行卡是确定性步骤直出**：`draft_release_card` 是结构化拼装，非 LLM。
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
  - status (PLANNING / RUNNING / WAITING / DONE / FAILED)   -- WAITING = 已产待办、本段已结束，非挂起
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
  - waiting_ticket_id                  -- 本段结束时挂上的 MES 工单/待办实体 id
  - tool_call_traces (JSON)
  - occurred_at
```

```python
class ActionCard(BaseModel):
    card_id: str
    session_id: str
    step: str
    capability: str | None         # 来自哪个能力，None 表示确定性步骤直出
    intent: str                    # "激活工艺路线 RR-100 v4" / "下达批次隔离"
    draft_payload: dict            # 草稿内容（intent + draft，非已落库数据）
    writes_via: str                # "工艺管理上下文.application.activate_route"
    requires_confirmation: bool = True
    evidence_set_id: str | None    # 关系型证据子集 id：确认人用它回查证据（非图快照）
    evidence: list[str]            # trace_id 列表
    agent_hypothesis: dict | None  # 根因 / 隔离集（确定性步骤为 None）
    confidence: str | None
    risk_note: str
    sla_at: datetime | None            # 工单侧 SLA，随待办实体走审批流；agent 侧不计时
```

- 卡片带 `evidence_set_id` + `evidence` + `agent_hypothesis` + `confidence`，确认人点开可按 `evidence_set_id` 回查关系型证据子集与工具调用——**基于证据确认而非盲批**。`confidence` 由规则算，不采信 LLM 自评。
- `node_type` + `capability` 落库，让"本次会话调了几次 LLM、哪几步是代码、哪几步来自哪个能力"可量化。

---

## 6. 代码骨架

> **诚实边界**：本节与 §4.2 的代码均为**设计目标骨架，尚未落地**——当前代码仓 `src/` 只有 `agents/tool_test.py`、`api/{health,index,tool_test}.py`、`schemas/tool_test.py`、`tools/get_current_time.py`、`factory.py`，编排能力完全未实现。类名与步骤名是本仓库自命名，不对应任何框架原语。

### 6.1 编排 flow 装配（换线）

> 设计目标骨架，尚未落地。2.0 没有声明式协同原语（`MsgHub` / `sequential_pipeline` / `fanout_pipeline` 已删除，`GoalPipeline` / `TeamPipeline` 仅 experimental，观察不进生产），所以编排形状就是普通 Python：顺序 = 依次 `await`，并行 = `asyncio.gather`，分流 = 普通 `if`。步骤名（plan / tooling_check / barrier / gate_release…）是本仓库自命名的 flow 步骤，不是框架概念。

```python
# app/ai/flows/changeover.py
class ChangeoverFlow:
    """换线：代码骨架 + 仅在 mismatch 分支调用诊断/草稿能力。
    无状态分段：一次调用跑到 gate 即返回，确认后重新发起下一段。"""

    def __init__(self, steps, capability_nodes, gates, sessions: SessionService) -> None:
        self._s = steps                     # 确定性步骤（普通 async 函数，不调 LLM）
        self._agents = capability_nodes      # 能力节点（内部 await 诊断 / 草稿 Agent）
        self._gates = gates
        self._sessions = sessions

    async def run(self, state: OrchestrationState, *, budget: int) -> OrchestrationState:
        # 顺序协同：依次 await；每步消耗一次"步数与工具调用预算"
        state = await self._s.plan(state)
        state = await self._s.query_first_article(state)
        state = await self._gates.require("FIRST_ARTICLE", state)
        state = await self._s.query_active_route(state)          # 强制版本锚点
        state = await self._gates.require("PROCESS_SWITCH", state)

        # 并行协同：asyncio.gather 并发各自独立的执行体，汇合即返回，不持有流程状态
        tooling, kitting = await asyncio.gather(
            self._s.query_and_compare_tooling(state),
            self._s.query_and_compare_kitting(state),
            return_exceptions=True,
        )

        # barrier：确定性分流（代码判定，不是 ConditionalEdge DSL，也不是 LLM 决策）
        if tooling.ok and kitting.ok:
            state = await self._s.draft_release_card(state)      # 代码结构化拼装，非 LLM
            state = await self._gates.require("RELEASE", state)
            return await self._s.done(state)

        if kitting.failed_missing_material:
            # 缺料是确定的：不调能力，产缺料待办后本段结束（催料归工单侧）
            return await self._s.end_segment(state, waiting="MATERIAL_SHORTAGE")

        # mismatch 分支：能力节点（仅此处调 LLM）
        while self._budget_left(budget) and state.retry_tooling:
            state = await self._agents.root_cause(state)         # await 诊断能力
            state = await self._agents.disposition(state)        # await 草稿能力
            state = await self._gates.require("DISPOSITION", state)
            state = await self._s.query_and_compare_tooling(state)   # 回查重检
            if state.failure_count >= 2:
                # 连续失败 2 次 -> 本段置 WAITING 并转人工待办，不保挂起态
                return await self._s.end_segment(state, waiting="CAPABILITY_FAILED")
        return await self._s.done(state)
```

### 6.2 能力节点（统一形态）

```python
# app/ai/capability_nodes/root_cause.py
class RootCauseNode:
    """flow 能力节点：把结构化 mismatch 交给诊断能力，产物再交草稿能力。"""

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
        # 结论落成关系型证据子集引用；下游按需回查，不做成员间广播
        state["diagnosis_evidence_set_id"] = report.get("evidence_set_id")
        return state
```

```python
# app/ai/capability_nodes/draft_nodes.py
class DispositionCardNode:
    """flow 能力节点：把诊断的证据集引用交给草稿能力，统一格式化成处置卡。"""

    async def __call__(self, state: OrchestrationState) -> OrchestrationState:
        draft = await self._runtime.invoke(
            "drafting",
            {
                "draft_kind": "DISPOSITION_CARD",
                # 只传证据集引用与置信度，不传消息历史：草稿能力按 evidence_set_id
                # 回查关系型证据子集（fetch_evidence_set），不与诊断共享上下文
                "evidence_set_id": state["diagnosis_evidence_set_id"],
                "confidence": state["agent_confidence"],
            },
            tenant=state["tenant"],
            parent_session_id=state["session_id"],
        )
        state["action_card"] = build_action_card(draft, step="DISPOSITION")
        return state
```

### 6.3 barrier（确定性分流）

```python
# app/ai/flow_steps/barrier.py
async def barrier_node(state: OrchestrationState) -> OrchestrationState:
    t, k = state["tooling_result"], state["kitting_result"]
    if t["status"] == "PASS" and k["status"] == "PASS":
        state["barrier_route"] = "draft_release"
    elif t["status"] == "FAIL":            # mismatch -> 交诊断能力
        state["barrier_route"] = "root_cause"
        state["expected"], state["actual"] = t["expected"], t["actual"]
        state["mismatch_code"] = t["code"]
    else:                                   # 缺料 -> 确定性，产待办后本段结束
        state["barrier_route"] = "open_shortage_ticket"
        state["status"] = "WAITING"
    return state
```

### 6.4 异步驱动与双闸门

```python
# app/application/orchestration_service.py
class OrchestrationService:
    async def start(self, req: OrchestrationRequest, tenant: TenantContext) -> OrchestrationSession:
        # run 先落库再执行（ADR-07）：会话与步骤先写 MySQL，再点火
        session = await self._sessions.create("orchestration", req, tenant)
        self._background.spawn(self._drive, session, tenant)   # 单次调用的短后台任务（跑到 gate 即返回）
        return session

    async def _drive(self, session, tenant) -> None:
        try:
            await asyncio.wait_for(
                self._flow.run(
                    {"session_id": session.id, "tenant": tenant, **session.context},
                    budget=40,                 # 本段步数与工具调用预算上限
                    session_id=session.id,     # 会话标识按 session_id 取，副本无关
                ),
                timeout=300.0,           # ★ 单次调用的计算超时，**不含等待人确认的时间**
            )
        except (StepBudgetExceeded, asyncio.TimeoutError) as exc:
            await self._sessions.mark_failed(session, str(exc))
```

`本段步数上限 40` 与 `timeout=300s` 构成"步数 + 时长"双闸门，超限标 `FAILED` 转人工，不硬走。⚠️ 时长闸门只约束**一次调用内的计算**：等待人工确认不占用它——gate 之前本次调用就已返回，等待期落在 MES 工单实体的生命周期里，超时升级与催办由审批流负责（ADR-05）。

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

- **特有指标**：`agent_session_total{capability="orchestration",scenario}` / `agent_step_total{node_type}` / `agent_llm_invocation_total{scenario}`（换线全 PASS 为 0）/ `agent_gate_decision_total` / `agent_segment_waiting_total`（本段以产待办结束的次数）/ `agent_advance_callback_total`（确认后重发起下一段）/ `agent_write_tool_total` / `agent_write_rejected_total`。
- **链路**：一个会话一个 `trace_id`（由 `TracingMiddleware` 注入，动作卡带 `evidence_set_id` + `evidence` 列表 + `agent_hypothesis`）；跨能力调用靠 `parent_session_id` 串；研发期排障用 OTel + Tempo/Jaeger 的 trace 视图加 `reply_stream` 事件回放自建脚本（Studio 已归档，不作为工具列）。
- **兜底**：gate 超时升级由待办实体侧负责（agent 侧不计时）；barrier 未 PASS 禁推放行卡；置信度 `low` 标 `need_human_review` 不自动路由（`confidence` 由规则算，不采信 LLM 自评）；写工具未带网关一次性动作令牌被拒（P0 告警）；能力节点连续失败 2 次 -> 本段置 `WAITING` 转人工待办；`gaps`（证据缺口）是一等公民——取不到证据必须显式声明缺口并拒答，不许硬凑，版本锚点取不到同样拒答。

---

## 8. 实现步骤

### 阶段一：编排代码层骨架

1. 在既有 runtime 上扩展 `ai/flow_steps/` 与 `ai/flows/`，搭换线 flow + plan / query_compare / barrier / gate / done（普通 Python `await`）。
2. 落 `ToolDescriptor.capabilities` / `requires_confirmation` / `writes_via` 与 `ToolPolicyGate` 断言。
3. 落 `agent_session` / `agent_step_record`（`capability` + `node_type` 两列）+ `SessionService` / `AgentState` 会话存储。
4. 跑通 gate 的"推卡 → 落成 MES 工单/待办 → 本段返回"，再跑通 `/advance` 回调→ 重查事实→发起下一段（幂等可重入）。**不做挂起/续跑。**
5. 动作卡双通道推送 MVP（事件流 AGUI/SSE + Kafka 兜底）。
6. **换线场景跑通纯代码骨架（全程 PASS，无 LLM）**——先证明"代码能跑的不交给 LLM"。

### 阶段二：能力节点接入

7. `root_cause` 能力节点接诊断能力（结构化异常入参）+ `DISPOSITION_CARD` 草稿能力节点 + 处置 gate + 重检回路。
8. `fault_impact` 能力节点接诊断能力（遥测 + FMEA）+ `ISOLATION_CARD` 草稿能力节点。
9. 客诉场景串 `diagnosis → ISOLATION_CARD → EIGHT_D`（顺序 `await`）。
10. 工艺变更场景接 `SOP` 草稿能力节点。

### 阶段三：写路径与试点

11. 各上下文写 ACL（钢网归还/领用、隔离下达、SOP 发布）+ 工具网关一次性动作令牌（绑定 `action:target` + 用户 + 过期，用后即废）+ `confirmation_store`。
12. 挑一条产线灰度换线编排（先代码骨架 + mismatch 分支），gate 做扎实。
13. 接指标，重点观察 `agent_llm_invocation_total` 与 `agent_step_total{node_type="CODE"}` 占比。
14. 评测：根因命中、隔离集命中率、8D 采纳率、gate 拒绝率合理区间。

---

## 9. 约束落地检查清单

- [ ] `flows/` 编排层自身不持工具、不调 LLM；能力节点只 `await` 诊断 / 草稿能力。
- [ ] 并行分支只用 `asyncio.gather` 并发各自独立的 `Agent.reply()`，在应用层显式合并；**分支间不共享消息流、不看对方中间结论**，只读同一份 `evidence_set_id`。
- [ ] 任何能力下均无放行 / 拦截类工具（前缀断言）；实际放行走过点应用服务。
- [ ] 所有写工具 `requires_confirmation=True` + `writes_via`，无令牌写调用被拒。
- [ ] 写落库走上下文应用服务 REST，过聚合根不变式 + 事务发件箱。
- [ ] 一次性动作令牌绑定 `action:target` + 具体用户，带过期，不可从 requestId 推导，用后即废，过期 fail closed。
- [ ] barrier 在放行前等所有分支 PASS，未 PASS 分流到诊断能力或产待办结束本段，不推放行卡。
- [ ] gate 决策（谁 / 何时 / 基于哪张卡 / 假设 / token）落 `agent_step_record` 可审计。
- [ ] 确定性步骤（query / compare / barrier / gate / draft_release_card）不调 LLM，`node_type=CODE` 可验。
- [ ] 跨能力调用带 `parent_session_id`，同一条 trace 可回溯。
- [ ] 工艺查询强制版本锚点，ACL 校验 `ACTIVE`。
- [ ] 置信度 `low` 标 `need_human_review`，不自动路由。
- [ ] gate 的等待/催办/超时升级在 MES 工单实体侧；agent 侧只保证**本段在时长闸门内返回**，不持有挂起态；能力节点连续失败 2 次转人工待办。

---

## 10. 面试防守 Q&A

**Q：编排能力是不是又一个 Agent？和诊断、草稿怎么分工？**
A：不是。编排是**一段应用层代码 flow**（`flows/changeover.py`）：步骤顺序、并行、barrier、gate、写落库全是确定性步骤；需要判根因时 `await` 诊断能力，需要出可下达的卡时 `await` 草稿能力。编排层自己不实现第四种推理——早期把"根因 / 隔离 / 追溯 / 生成"做成 4 个独立 sub-agent，结果和诊断、草稿形成两套实现，现在收敛成"调用已有能力"。默认仍是单 Agent：拆多 Agent 要同时过四道闸（模式 / 可拆性 / 中心校验 / 成本）。

**Q：编排为什么不写成框架的 pipeline / 声明式图？**
A：因为底座框架 **AgentScope 2.0 不提供**这些原语。`MsgHub` / `sequential_pipeline` / `fanout_pipeline` 是 1.x API，2.0.0 起模块已不存在；`Handoffs` / `Routing` 从未提供；剩下的 `GoalPipeline` / `TeamPipeline` 官方标 experimental（且 `TeamPipeline` 明写"成员间不互相说话"），只观察、不进生产。所以形状就落在普通 Python：顺序 = 依次 `await`，并行 = `asyncio.gather` 并发各自独立的 `Agent.reply()` + 应用层显式合并，分流 = 普通 `if`。副作用是可控性一点没丢：步骤名、判定、gate、barrier 全在本仓库代码里，可审计、可回归，不随框架 API 漂移（2.0.x 已发生过模块删除与 `SessionManager`→`SessionService` 改名）。跨天的等待与补偿不在这一层，归工作流引擎（ADR-05）。

**Q：为什么编排代码层不调 LLM？**
A：因为编排里的大部分步骤是确定性的：顺序固定、判定规则固定、输入结构化。漏步骤、带病生产、写越界这些风险，代码做比 LLM 更可靠更快。用纯 prompt 让模型自己编排，容易丢步骤且难审计。所以 flow 编排层是纯代码，LLM 只出现在"输入开放 / 需推理生成 / 分支难穷举"的能力节点，且这些节点直接复用诊断与草稿能力。三问皆否即代码——这是本仓库自有判据，框架文档没有给"何时该拆多 Agent"的量化标准。

**Q：换线全程 PASS 时 LLM 调用真的是 0 吗？**
A：是。路径是 `plan → first_article → process_switch → tooling_check(PASS) ‖ kitting_check(PASS) → barrier → draft_release(代码拼装) → gate_release → done`，全程没有能力节点。指标 `agent_llm_invocation_total{scenario="changeover"}` 可验证为 0——这是"懂什么时候不用 AI"的可量化标志。

**Q：multi-agent 怎么保证不越界进过点主事务？**
A：三层兜底。① flow 编排层不持工具，纯代码编排器，想越界也没工具调；② 工具按能力切片（挂进各能力的 `Toolkit`），放行 / 拦截类工具**不注册到任何能力**；③ 启动断言：任何能力下不得有 `pass_judge` / `force_release` / `release_*` / `block_pass` 前缀工具。实际放行由人确认放行卡后走过点上下文正常应用服务，P99 ≤200ms 判定仍走规则引擎。

**Q：写动作怎么保证不旁路应用服务？**
A：写工具必须声明 `requires_confirmation` + `writes_via`（落库走哪个应用服务），否则启动失败。Agent 产出 intent + draft（动作卡，含假设 + 证据），人确认后拿 confirmation token，调对应上下文应用服务 REST——过聚合根不变式 + 事务发件箱。Agent 全程不碰原始表。

**Q：mismatch 的处置，规则引擎真的做不了吗？**
A：能写，但维护成本高且永远落后。根因不止"拿错"——上工单未还库、台账改名、借出未登记、工艺录错、扫码制式不同……每出现一种新现场情况决策树就要补分支，还要补对应的跨上下文联合查询，判定阈值还是启发式的。Agent 的优势不是"比决策树聪明"，而是**取证路径由中间结果驱动**：查到"ST-A 借出未还"后自适应去查上工单收线记录，而不是固定 JOIN。新根因出现时只要取证能查到证据就能给假设，决策树不用改。

**Q：confirmation gate 怎么实现人在回路？**
A：分两段，中间不挂任何 agent 侧状态。**本段**：flow 跑到 gate 步骤时产出动作卡，同时落成 MES 已有的工单/待办实体（维修单 / 隔离单 / 首件放行 / 8D / SOP），本次调用就返回，进程可退出。**等待期**：责任人分派、催办、超时升级、跨天全在那个工单实体的生命周期里（ADR-05），agent 不计时也不占执行槽。**下一段**：审批完成后回调 `POST /agent/orchestration/{session_id}/advance`，先由**工具网关签发一次性动作令牌**（主闸），再重新发起一次短调用——它只取 `session_id` + `scenario` 标识，现场事实按 `evidence_set_id` **重查当下**（不恢复快照，因为 MES 数据时变）。每段以 `session_id + step` 为幂等键，写路径另用 `confirmation_id`；进程中途死亡无需特殊处理，下一段幂等重跑即可。令牌绑定 `action:target` + 具体用户，带过期，不可从 requestId 推导，用后即废，过期或校验失败 fail closed；`PermissionEngine` 的 gate 规则只是叠加层，绕开 Agent 循环直接调函数时它不生效，所以不能当主闸。gate 决策（谁 / 何时 / 基于哪张卡 / 假设）落 `agent_step_record`，全可审计。

**Q：为什么不直接借用框架的挂起/续跑？**
A：一层原因是 **AgentScope 2.0 没有这个原语**——框架级 `interrupt` / `Command(resume=…)` / checkpoint 表不存在（LangGraph 语义），`RequireUserConfirmEvent` 只能表达**同一次调用内**的确认，载不回快照续跑。更关键的是我们逐项对过 4 个编排场景后，判定自建同语义机制也不值：① gate 之间几乎全是确定性代码节点（换线快路径 LLM 调用为 0），挂起期没有任何需要保住的计算态；② MES 现场事实**时变**，恢复 T+3s 的快照到 T+10min 可能已经不成立，重查比快照重放更正确；③ 每个 gate 产出的都是已存在的业务对象，审批本来就有可靠实体。所以这一层干脆不要，而不是换个框架继续做。

**Q：Agent 失败了会卡住产线吗？**
A：故障隔离。能力节点连续失败 2 次置 `WAITING` 推异常卡并转人工待办，不自动重试到死；置信度 `low` 标 `need_human_review` 列疑点不自动路由；barrier 未 PASS 禁止推放行卡；本段只受时长闸门约束，超时直接置 `FAILED` 转人工，不会无限占槽。最坏情况是流程退回人工编排，不会越界写、不会错误放行。

---

## 11. 一句话定位

"流程编排能力是**一段代码 flow + 两次能力调用**：`flows/` 里确定性步骤负责 plan / 比对 / barrier / gate / 写落库，需要判根因就 `await` 诊断能力、需要出可下达的卡就 `await` 草稿能力，编排层不实现第四种推理。换线全程 PASS 时 LLM 调用为 0；写动作过 confirmation gate（**Agent 无状态分段**：产动作卡落成 MES 工单/待办实体后本调用结束，确认回来重新发起下一段并重查当下事实，叠上绑定 action:target 的网关一次性动作令牌），落库走各上下文应用服务过聚合根不变式 + 事务发件箱；放行 / 拦截能力从架构层切掉，过点判定仍归规则引擎。"
