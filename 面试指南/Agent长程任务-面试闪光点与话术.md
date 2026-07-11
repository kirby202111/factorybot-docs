# Agent 长程任务 · 面试闪光点与话术（状态持久化 / 一致性保证 / 中断恢复 / 人在回路深度版）

> **定位**：本文是 [L3编排型Agent-实现方案.md](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) 与 [L3编排型Agent-LangGraph架构与流程图.md](../AGENT服务/L3编排型Agent/L3编排型Agent-LangGraph架构与流程图.md) 的**面试纵深展开**，聚焦 L3 编排型 Agent 作为系统中唯一面向长程任务设计的模块，钻进 **状态持久化、一致性保证、中断恢复、人在回路、超时兜底、部分失败回滚** 六条主线讲到底，每条都绑出处 + 防守话术。
>
> **口径纪律**（沿用 [项目亮点与指标卡片.md](项目亮点与指标卡片.md) §0）：这是**设计规划阶段**方案，不是"已上线长程任务引擎"。设计规模 / 架构 SLA 目标可给，须标注口径；不编造业务效果数字。长程任务的核心矛盾不是"跑得久不久"，而是"**既能跨小时跨人工确认保持状态一致、又能在进程重启/超时/人无响应/部分失败时安全降级、还要写动作绝不旁路 MES 的聚合根不变式**"——所有设计围绕这个矛盾展开。

---

## 0. 核心闪光点速览（先报这组，最硬）

| 闪光点 | 一句话 | 出处 |
|--------|--------|------|
| **异步驱动 + 中断恢复** | HTTP 只负责启动，`asyncio.create_task` 异步驱动，gate 处 `interrupt` 暂停等人，state 落 MySQL 可跨进程续跑 | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §7.3 |
| **状态外置持久化** | LangGraph `SqlSaver` 以 `thread_id=session_id` 持久化，进程重启从断点恢复，gate 等待状态不丢 | [L3流程图](../AGENT服务/L3编排型Agent/L3编排型Agent-LangGraph架构与流程图.md) §3 |
| **confirmation token 绑定 action:target** | 人确认后发 token，绑定具体写动作（`isolation.issue:tenant_001`）+ 过期，防篡改防重放 | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §5.7 |
| **写不旁路应用服务** | 写落库走各上下文**正常应用服务**，过聚合根不变式 + 事务发件箱，与正常写路径完全一致，只是触发源从"人点按钮"变成"Agent 草拟 + 人确认" | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §5.5 |
| **步数 + 时长双闸门** | `recursion_limit=40` + `timeout=3600s`，超限标 FAILED 转人工，不硬走 | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §7.3 |
| **gate deadline 挂起** | 每个 confirmation gate 有 deadline，超时自动 `SUSPENDED` 推责任人，不无限阻塞产线 | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §12 |
| **barrier 硬防错** | 并行分支汇合后 barrier 按结构化结果确定性分流，未 PASS 绝不推放行卡——编排层防错，非 agent 判定 | [L3流程图](../AGENT服务/L3编排型Agent/L3编排型Agent-LangGraph架构与流程图.md) §3 |
| **故障隔离不污染整链** | agent 连续失败 2 次标 `SUSPENDED` 推异常卡，不自动重试到死；低置信度标 `need_human_review` 不自动路由 | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §12 |
| **会话全生命周期可审计** | `l3_session` + `l3_step_record`（含 `node_type` CODE/AGENT 区分 + `gate_decision` 谁/何时/基于哪张卡/agent 假设），每一步决策可回溯 | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §5.3 |
| **代码节点 vs agent 节点物理分层** | `orchestration/code_nodes/` 与 `orchestration/agents/` 分开落点，`node_type` 落库可验——换线全程 PASS 时 LLM 调用为 0 | [L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §6 |

> 这组是长程任务设计的"门面"。被问"Agent 跑一半人没确认怎么办"或"进程重启了 task 状态还在吗"时，直接甩"异步驱动 + SqlSaver 持久化 + interrupt/resume + confirmation token + 双闸门 + 故障隔离"六件套。

---

## 1. 30 秒电梯陈述

"MES Agent 的长程任务设计，核心是 L3 编排型 Agent。它处理的是跨多个限界上下文、含人工确认的完整业务流程——比如一次换线从首件触发到最终放行，中间要等人确认工艺、等钢网齐套核对、等人确认处置，全程可能 30 分钟到 1 小时。

长程任务的一致性不是靠'一个长事务锁到底'，而是靠**四层设计**：

第一层**状态外置**：LangGraph `SqlSaver` 把 state 持久化到 MySQL，`interrupt` 暂停时进程不阻塞，`resume` 时从断点恢复，进程重启不丢状态。

第二层**写一致性**：所有写动作走 confirmation gate——Agent 草拟 intent + draft，人确认后发 confirmation token（绑定 `action:target` + 过期），落库走各上下文**正常应用服务**过聚合根不变式 + 事务发件箱，绝不旁路写。

第三层**超时兜底**：`recursion_limit=40` + `timeout=3600s` 步数+时长双闸门，gate deadline 超时挂起推责任人，不无限阻塞。

第四层**故障隔离**：agent 连续失败 2 次标 `SUSPENDED` 推异常卡，barrier 未 PASS 不放行，局部失败不污染整链——最坏情况退回人工编排，不会越界写、不会错误放行。"

**五个抓手**：状态外置持久化 / 写不旁路应用服务 / 双闸门兜底 / 故障隔离 / 全链路可审计。

---

## 2. 闪光点详解（按主线）

### A. 长程任务的状态管理——HTTP 请求不阻塞，进程重启不丢状态

#### A.1 异步驱动：HTTP 只负责"启动"

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §7.3）：

```python
async def start(self, req: L3Request, tenant: TenantContext) -> L3Session:
    session = await self._sessions.create(req, tenant)
    asyncio.create_task(self._drive(session, tenant))  # 异步驱动
    return session  # 立即返回 session_id，不阻塞 HTTP
```

- HTTP 请求 `/agent/l3/{scenario}/start` 只做两件事：创建 session 记录 + 启动异步任务，**立即返回** `session_id`。
- 后续流程（plan → gate → agent → gate → done）在后台异步执行，gate 处 `interrupt` 暂停等人，不占 HTTP 连接。
- 调用方拿到 `session_id` 后轮询 session 状态或通过 WebSocket 收动作卡推送。

**为什么是亮点**：长程任务最忌讳"一个 HTTP 请求从头等到尾"——MES 换线可能要 30 分钟，HTTP 连接早就超时了。异步驱动 + interrupt 让 HTTP 只做"点火"，真正的长跑在后台，gate 等人时进程不占连接、不占 CPU。这是**长程任务设计的第一个分水岭**：不懂的人会让 HTTP 一直 block 到流程结束；懂的人知道"启动后立即返回，状态靠查询/推送"。

**防守话术**："HTTP 请求只负责启动，立即返回 session_id。长流程在后台异步跑，gate 等人确认时 `interrupt` 暂停，进程不阻塞、不占连接。这和 MES 过点 P99 ≤200ms 不冲突——长程任务的等待不挂在过点路径上。"

#### A.2 状态外置持久化：进程重启不丢状态

**核心机制**（[L3流程图](../AGENT服务/L3编排型Agent/L3编排型Agent-LangGraph架构与流程图.md) §3）：

- LangGraph 的 `SqlSaver` checkpointer 以 `thread_id=session_id` 将 state 持久化到 MySQL。
- `interrupt` 暂停时，当前 state（包括已执行的步骤、待确认的 gate、agent 产出的假设）全部落库。
- 进程重启后，用同一个 `thread_id` 调 `graph.ainvoke(Command(resume=...), config={"configurable": {"thread_id": session_id}})` 即可从断点恢复。
- gate 等待状态不丢——即使 agent-service Pod 重启，人确认后 resume 仍能找到对应的 gate 继续。

**为什么是亮点**：MES 场景下的长程任务不能靠"进程内存"存状态——Pod 重启、发布滚动更新、OOM Kill 都会导致状态丢失。SqlSaver 把 state 外置到 MySQL，让长程任务**跨进程生命周期**存活。这是长程任务设计的第二个分水岭：不懂的人把状态放在进程内存里；懂的人知道"状态外置 + thread_id 恢复"。

**防守话术**："换线流程跑一半 Pod 重启了怎么办？LangGraph 的 SqlSaver 把 state 持久化在 MySQL，重启后用同一个 session_id 作为 thread_id 恢复，gate 等待状态、已执行的步骤、agent 产出的假设全在，不会从头重跑。这和 MES 的生产执行一样——状态在数据库里，不在内存里。"

#### A.3 会话状态表设计

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §5.3）：

```sql
l3_session
  - session_id (PK)
  - scenario (CHANGEOVER / FAULT_RESPONSE / COMPLAINT_8D / PROCESS_CHANGE)
  - work_order_id / batch_id / asset_id
  - tenant_context (JSON)
  - status (PLANNING / RUNNING / SUSPENDED / DONE / FAILED)
  - current_step
  - created_at / updated_at

l3_step_record
  - record_id (PK)
  - session_id (FK)
  - step
  - node_type (CODE / AGENT)              -- 区分代码节点与 agent 节点
  - capability (A/B/C/D / NULL)
  - status (PENDING / RUNNING / GATE_WAITING / CONFIRMED / FAILED)
  - action_card_payload (JSON)
  - agent_hypothesis (JSON)
  - agent_confidence (high/medium/low)
  - gate_decision (PASS / REJECT / RETRY)
  - gate_decided_by / gate_decided_at
  - tool_call_traces (JSON)
  - occurred_at
```

- `node_type` 区分代码节点与 agent 节点，可统计"本次换线调了几次 LLM"。
- `gate_decision` + `gate_decided_by` + `gate_decided_at` 让每次人工确认可审计——谁、何时、基于哪张卡、agent 假设是什么。
- `status` 枚举覆盖完整生命周期：PLANNING → RUNNING → SUSPENDED/DONE/FAILED。

**防守话术**："`l3_step_record` 的 `node_type` 字段把代码节点和 agent 节点区分落库——可观测时能量化'本次换线调了几次 LLM'。换线全程 PASS 时 agent 调用为 0，这个指标本身就是健康度信号。"

---

### B. 长程任务的一致性保证——写动作绝不旁路 MES 的聚合根不变式

#### B.1 写动作 confirmation gate（最大闪光点）

**核心机制**（[L3流程图](../AGENT服务/L3编排型Agent/L3编排型Agent-LangGraph架构与流程图.md) §5）：

```
Agent 草拟 intent + draft（动作卡）
  → gate interrupt 暂停，推卡给人
  → 人查看 evidence + agent_hypothesis + confidence
  → 人确认/拒绝
  → /confirm 端点发 confirmation token（绑定 action:target + 过期）
  → resume 后写落库走各上下文应用服务 REST
  → 应用服务过聚合根不变式 + 事务发件箱
```

**为什么是亮点**：写动作的一致性不是靠"Agent 记住要写什么"，而是靠"**草稿 + 人确认 + token + 走正常应用服务**"四段式。这保证了：
1. Agent 的输出是 intent + draft，不是最终写结果。
2. 人确认不是"盲批"——动作卡带 evidence（trace_id 列表）+ agent_hypothesis + confidence，确认人**基于证据确认**。
3. confirmation token 绑定具体写动作（`isolation.issue:tenant_001`），防篡改（不能拿 A 动作的 token 去做 B 动作）、防重放（带过期）。
4. 落库走正常应用服务，过聚合根不变式 + 事务发件箱——与 MES 正规写路径完全一致，只是触发源从"人点按钮"变成"Agent 草拟 + 人确认"。

**防守话术**："Agent 写动作不直写 MES 原始表。它生成的是 intent + draft（动作卡），人确认后发 confirmation token，token 绑定 `action:target` 带过期，落库走各上下文正常应用服务过聚合根不变式 + 事务发件箱。写路径和正常写完全一致，只是触发源从'人点按钮'变成'Agent 草拟 + 人确认'——所以写一致性受 MES 已有的聚合根不变式保护，Agent 不引入新的写风险。"

#### B.2 WriteToolGate：写工具不声明就启动失败

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §4.3）：

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
```

- 任何写工具必须声明 `requires_confirmation=True` 和 `writes_via`（指向具体上下文应用服务），否则**启动断言失败**。
- 这保证了"红线靠代码兜底，不靠人记"——写工具不可能被"不小心"注册成不需要确认的。

**防守话术**："不是靠文档或代码审查保证写工具的安全——是靠启动断言。任何写工具必须声明 `requires_confirmation` 和 `writes_via`，否则进程起不来。红线在代码里，不在人脑子里。"

#### B.3 放行能力从架构层切掉

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §7.5）：

```python
# 启动断言：任何 capability 下均无放行/拦截类工具
for cap in ("root_cause", "fault_impact", "traceability", "draft_sop", "draft_8d", "draft_rework_craft"):
    tools = reg.tools_for(cap, ANY_TENANT)
    assert not any(t.name.startswith(("pass_judge", "force_release", "release_")) for t in tools), cap
```

- 放行/拦截类工具**不注册到任何 capability**，Agent 从架构层不存在放行能力。
- 实际放行由人确认放行卡后，走过点上下文正常应用服务，过点 P99 ≤200ms 判定仍走规则引擎。

**防守话术**："Agent 能不能放行？不能。不是我'不让他放'，而是架构层就不存在放行工具——任何 capability 下都没有 `pass_judge` / `force_release` / `release_*` 前缀的工具，启动断言校验。放行卡是代码节点结构化拼装的（非 LLM），人确认后走过点上下文正常应用服务，过点 P99 ≤200ms 判定仍走规则引擎。Agent 从架构层切掉了放行能力。"

---

### C. 长程任务的可靠性——中间状态不丢、超时不卡死、失败不污染

#### C.1 步数 + 时长双闸门

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §7.3）：

```python
await asyncio.wait_for(
    self._supervisor.ainvoke(
        {"session_id": session.id, "tenant": tenant, **session.context},
        config={"recursion_limit": 40, "configurable": {"thread_id": session.id}},
    ),
    timeout=3600.0,  # 含人确认等待的整体上限
)
```

- `recursion_limit=40`：最多 40 步（含代码节点 + agent 节点 + gate 等待），超限抛 `GraphRecursionError` 标 FAILED 转人工。
- `timeout=3600.0`：整体 1 小时上限，超时抛 `asyncio.TimeoutError` 标 FAILED 转人工。
- 双闸门覆盖两类失控：步数爆炸（agent 循环调工具）和整体超时（人长时间不确认）。

**为什么是亮点**：长程任务最怕"无限等待"——人休假了、推送没收到、token 过期了没人管。双闸门确保**任何情况下都不会无限阻塞**。而且两个闸门是**互补的**：步数闸门防 agent 推理循环，时长沙门防人无响应。两者覆盖不同故障模式。

**防守话术**："长程任务怎么保证不无限阻塞？双闸门——`recursion_limit=40` 防 agent 循环调工具，`timeout=3600s` 防人长时间不确认。超限不是默默重试，是标 FAILED 推异常卡给责任人，和 MES 防错理念一致——宁可挂起让人判，也不硬走。"

#### C.2 gate deadline：每个确认点都有超时

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §12）：

- 每个 confirmation gate 有独立 deadline，超时自动挂起 `SUSPENDED`，推责任人。
- 不无限阻塞——不会因为某个 gate 的人休假导致整条产线的 agent 会话卡住。
- 挂起后可人工恢复或手动终止，不自动重试。

**防守话术**："每个 gate 确认点有独立 deadline，超时自动挂起推责任人，不无限阻塞。不是全局一个 timeout 等死，而是每个 gate 各自有 deadline——工艺确认超时挂工艺卡、放行确认超时悬挂行卡，互不污染。"

#### C.3 故障隔离：agent 失败不污染整链

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §12）：

| 触发条件 | 动作 | 观测 |
|---------|------|------|
| agent 连续失败 2 次 | 标 `SUSPENDED`，推异常卡，不自动重试到死 | `l3_suspended_total` +1 |
| agent 置信度 `low` | 处置卡标 `need_human_review`，列疑点，不自动路由 | `agent_low_confidence_total` +1 |
| barrier 未 PASS | 禁止推放行卡，分流到 agent 或挂起 | `l3_suspended_total` +1 |
| gate 超 deadline | 挂起 `SUSPENDED`，推责任人 | `l3_suspended_total` +1 |
| 写工具未带有效 token | `WriteToolGate` 拒绝 + P0 告警 | `l3_write_rejected_total` +1 |

**为什么是亮点**：长程任务中局部失败是常态——agent 某次推理置信度低、人迟迟不确认、工具调用偶发失败。设计的关键不是"保证不失败"，而是**失败隔离**：一个 agent 能力失败不污染其他能力，一个 gate 超时不影响其他 gate，单点失败挂起不整链雪崩。最坏情况是流程退回人工编排——不会越界写、不会错误放行。

**防守话术**："agent 推理失败了会卡住产线吗？不会。故障隔离——agent 连续失败 2 次标 `SUSPENDED` 推异常卡，不自动重试到死；低置信度不自动路由；barrier 未 PASS 不放行。最坏情况是流程退回人工编排，不会越界写、不会错误放行。和 MES 防错理念一致——宁可拦下让人判。"

#### C.4 部分完成不能回滚的场景——靠"不自动写"规避

**为什么这是设计取舍**：长程任务不像数据库事务可以 ROLLBACK——人已经确认了工艺激活、但后续钢网核对失败了，工艺已经切了，不能"回滚"工艺版本。L3 的设计选择是**不引入补偿事务，而是靠"不自动写"规避**：

- 写动作必须人确认，人确认前全是草稿态——草稿不落库，没有"回滚"问题。
- 人确认后的写动作走正常应用服务，应用服务内部的聚合根不变式负责一致性——这不是 Agent 层该管的事。
- 如果流程中途失败（如 agent 连续失败被挂起），已确认的步骤**不撤销**（因为人确认过），未确认的步骤不执行——这是"人在回路"的语义：人的确认就是 commit point。

**防守话术**："长程任务的中间状态怎么回滚？不回滚。这不是 bug 是设计取舍——L3 的写动作个个过 confirmation gate，人的确认就是 commit point。人确认过的步骤不撤销（因为那是人基于证据做的决策），未确认的步骤不执行（草稿不落库）。这和 MES 的实操一致——线长确认激活工艺后，不会因为后续钢网核对失败就'撤销工艺激活'，而是处置钢网问题。长程任务的一致性不靠分布式事务的 ROLLBACK，靠的是'草稿不落库 + 人确认 = commit + 聚合根不变式保护写'。"

---

### D. 长程任务的可观测——每一步都可审计

#### D.1 全生命周期落库

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §5.3）：

- `l3_session`：会话级（哪个场景、哪个工单、当前状态、起止时间）。
- `l3_step_record`：步骤级（代码节点还是 agent 节点、哪个能力、gate 决策、谁确认的、agent 假设 + 置信度）。
- 每一步都有 `occurred_at`，可以重建完整时间线。

**为什么是亮点**：长程任务出问题最怕"不知道卡在哪一步、谁确认的、为什么确认"。全生命周期落库让每次长程任务的执行过程可完整回溯——"这次换线为什么卡了 15 分钟？因为工艺 gate 等工程师确认等了 15 分钟，agent 假设是 confidence=high，工程师确认 PASS"——一条时间线全讲清。

**防守话术**："换线跑了一半卡住了，怎么排查？`l3_session` 看当前 `status` 和 `current_step`，`l3_step_record` 看每一步的 `occurred_at` 时间线，找到卡住的 gate——谁在等确认、agent 假设是什么、confidence 多少。不是看日志翻半天，是结构化可查询的。"

#### D.2 动作卡带证据链

**核心机制**（[L3实现方案](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) §5.4）：

```python
class ActionCard(BaseModel):
    card_id: str
    session_id: str
    step: str
    capability: str | None
    intent: str                    # "激活工艺路线 RR-100 v4"
    draft_payload: dict            # 草稿内容
    writes_via: str                # "工艺管理上下文.application.activate_route"
    requires_confirmation: bool = True
    evidence: list[str]            # trace_id 列表，给确认人看证据
    agent_hypothesis: dict | None  # agent 产出的根因/隔离集/假设
    confidence: str | None
    risk_note: str
    deadline: datetime | None
```

- 确认人看到的不是"请确认"三个字，是完整的 intent + draft + evidence + agent_hypothesis + confidence。
- 确认人点开 evidence 里的 trace_id 可以回溯到具体工具调用入参出参。
- **基于证据确认，而非盲批**。

**防守话术**："人确认时不是盲批一个按钮。动作卡带 intent+draft+evidence（trace_id 列表）+ agent_hypothesis+confidence。确认人可以看到 agent 的推理过程与证据链，点 trace_id 能回溯到具体下游 REST 调用——基于证据确认，而非盲批。这是 MES 防错理念在 Agent 人机交互层的延伸。"

---

## 3. 面试防守 Q&A（按追问深度分四层）

### 第一层：基础追问（面试官想确认你理解长程任务的本质）

**Q：你这套 Agent 系统里，哪个模块是长程任务？为什么它是长程？**

A：L3 编排型 Agent。它处理的是跨多个限界上下文、含人工确认的完整业务流程——一次换线从首件触发到最终放行，中间要等人确认工艺、等钢网齐套核对、等处置确认，全程可能 30 分钟到 1 小时。L0/L1/L2 都是短程——L0 单次问答，L1 多步只读推理秒级完成，L2 草拟后即结束。只有 L3 涉及"启动 → 等人确认 → 继续 → 再等人确认 → 完成"的异步长链。

**Q：长程任务的状态怎么管理？HTTP 请求一直等到结束吗？**

A：不。HTTP 只负责启动，`asyncio.create_task` 异步驱动后立即返回 session_id。后续流程在后台跑，gate 处 `interrupt` 暂停等人，进程不占连接。调用方通过轮询 session 状态或 WebSocket 收动作卡推送了解进度。这和 MES 过点 P99 ≤200ms 不冲突——长程任务的等待不挂在过点路径上。

**Q：进程重启了，跑到一半的换线会丢吗？**

A：不会。LangGraph 的 `SqlSaver` 以 `thread_id=session_id` 把 state 持久化到 MySQL。`interrupt` 暂停时当前 state 全落库，重启后用同一个 `thread_id` 恢复即可从断点续跑。这和 MES 的生产执行一样——状态在数据库里，不在内存里。

---

### 第二层：深度追问（面试官想确认你理解一致性保证的细节）

**Q：Agent 写动作的一致性怎么保证？万一 Agent 写了一半挂了怎么办？**

A：Agent 不直接写——它生成 intent + draft（动作卡），人确认后发 confirmation token，落库走各上下文正常应用服务。一致性靠四层保护：

1. **草稿不落库**：Agent 的输出是意图，不是已落库数据——不存在"写了一半"的问题。
2. **confirmation token**：token 绑定 `action:target` + 过期，防篡改（A 动作的 token 不能用于 B 动作）、防重放（过期作废）。
3. **应用服务不变式**：落库走正常应用服务 REST，过聚合根不变式 + 事务发件箱——与正常写路径完全一致。
4. **启动断言**：写工具必须声明 `requires_confirmation` 和 `writes_via`，否则进程起不来。

所以"写了一半挂了"的问题在 Agent 层不存在——Agent 不持有写能力，只是草拟 + 传递人确认后的 token。真正的写一致性由各上下文的应用服务保证，Agent 不引入新的写风险。

**Q：人确认了工艺激活，但后续钢网核对失败，怎么回滚？**

A：不回滚——这是设计取舍，不是 bug。L3 的 confirmation gate 就是 commit point：人确认过的事不撤销，因为那是人基于证据做的决策。实际 MES 现场也一样——线长确认激活工艺后，不会因为后续钢网核对失败就"撤销工艺激活"，而是处置钢网问题（归还 ST-A、领用 ST-B、重检）。

长程任务的一致性不靠分布式事务的 ROLLBACK，靠的是"草稿不落库 + 人确认 = commit + 聚合根不变式保护写"。如果流程中途失败被挂起，已完成的人确认步骤不撤销，未执行的步骤不执行——人在回路保证了 commit 语义。

**Q：confirmation token 怎么防止被伪造或重放？**

A：三层：
1. **绑定 action:target**：token 绑定具体写动作（如 `isolation.issue:tenant_001`），gate 校验 `valid_for(action)`——拿激活工艺的 token 不能去下达隔离。
2. **过期**：token 有过期时间，超期作废。
3. **服务端签发**：token 由 `/confirm` 端点的 `ConfirmationStore` 签发（存在 Redis），不是 Agent 自己生成。ACL 写客户端校验 token 时查 `ConfirmationStore` 验证有效性。

**Q：为什么写不走 Agent 进程而要绕到应用服务 REST？直接在 Agent 进程里写不是更快？**

A：因为 Agent 进程不持有 MES 的聚合根不变式。如果 Agent 直写 MES 表，就绕过了返工上下文的 `IsolationAggregate.issue()` 校验、绕过了工单管理上下文的工单状态校验、绕过了事务发件箱——这意味着一次"直写"可能产生幽灵数据，下游事件订阅者收不到事件，最终一致性被破坏。

走应用服务 REST 意味着写路径和正常写完全一致——触发源从"人点按钮"变成"Agent 草拟 + 人确认"，但**写路径不变**。这不是"慢"，是"安全"——MES 的写路径经过聚合根不变式 + 事务发件箱，Agent 不能绕过它们。

---

### 第三层：压力陷阱题（面试官想看你是否考虑过极端情况）

**Q：如果人一直不确认怎么办？gate 卡住 3 小时？**

A：不会。每个 gate 有独立 deadline，超时自动挂起 `SUSPENDED` 推责任人。加上整体 `timeout=3600s` 双闸门——即使 gate deadline 没配，整体超时也会兜底。挂起后 session 标 `SUSPENDED`，推异常卡给线长/工程师，不自动重试、不无限阻塞。最坏情况是流程退回人工编排——和 MES 防错理念一致，宁可挂起让人判，也不硬走。

**Q：agent 推理出错了怎么办？比如给了一个错的根因，人照着确认了？**

A：三个兜底：
1. **动作卡带证据链**：人确认时不盲批——卡上有 evidence（trace_id 列表）+ agent_hypothesis + confidence。人可以看到 agent 查了什么、推理过程是什么，基于证据做判断。
2. **低置信度不自动路由**：confidence=low 时卡上标 `need_human_review`，列疑点，不自动路由到执行——强制人仔细看。
3. **gate 决策可审计**：谁、何时、基于哪张卡做的确认，全落 `l3_step_record`。事后可追溯"这次确认是基于 agent 的什么假设和证据"。

但最终——如果 agent 推理错了且人没看出来且确认了，责任在人。Agent 是辅助决策工具，不是决策者。这和 MES 的"设备防错 + 人确认"双保险一脉相承——防错能拦住的由代码拦，拦不住的由人确认，出了事可审计。

**Q：如果 agent-service 整个挂掉（比如 OOM Kill），正在跑的 10 个换线会话怎么办？**

A：状态全在 MySQL（`l3_session` + `l3_step_record` + SqlSaver checkpointer），不在 agent-service 进程内存里。Pod 重启后：
- 10 个 session 的状态都在，status 可能是 RUNNING/SUSPENDED/GATE_WAITING。
- 对于 GATE_WAITING 的 session：人仍可通过 `/confirm` 端点 resume（因为 state 从 MySQL 恢复）。
- 对于 RUNNING 的 session：需要重新触发或手动恢复。

关键是**状态外置**——agent-service 是无状态的，状态在 MySQL。这是架构层面的选择：agent-service 只是执行器，不是状态持有者。

**Q：如果 MySQL 挂了（SqlSaver 写不进去），长程任务还能跑吗？**

A：不能——这是有意的设计取舍。SqlSaver 写失败意味着 state 无法持久化，此时继续跑风险太高（gate 等人期间如果进程重启，状态全丢）。所以降级策略是：SqlSaver 写失败 → session 标 FAILED → 推异常卡 → 流程退回人工编排。

这反过来验证了"状态外置"的必要性——如果 state 在进程内存里，MySQL 挂了进程还能跑，但进程重启就全丢。把 state 绑在 MySQL 上，MySQL 挂了你明确知道"现在不能跑长程任务"，而不是"假装能跑但随时可能丢状态"。

**Q：10 个换线同时跑，agent 能力 subgraph 之间会不会互相干扰？**

A：不会。三层隔离：
1. **工具集互斥**：每个 agent 能力（A/B/C/D）注册时声明 `capability`，`ToolRegistry.tools_for(capability)` 只返回该能力的工具。RootCauseAgent 看不到 FaultImpactAgent 的设备遥测工具。
2. **subgraph 独立**：每个能力是独立 `StateGraph`，有自己的 `recursion_limit`、system prompt、工具集，互不污染上下文。
3. **thread_id 隔离**：每个 session 有独立 `thread_id`，state 隔离在 MySQL。

所以 10 个换线同时跑，每个有自己的 session state、自己的 agent 实例、自己的工具集——互不干扰，并发安全。

---

### 第四层：设计权衡题（面试官想看你是否理解"为什么这么选"）

**Q：为什么不用 Saga 做长程任务的补偿回滚？**

A：两个原因。一是 L3 的写动作个个过 confirmation gate，人的确认就是 commit point——Saga 的补偿语义是"自动撤销"，但人确认过的事不应该自动撤销，那是推翻人的决策。二是 MES 场景下补偿事务的副作用太大——比如"撤销工艺激活"可能把已经在跑的新工艺在制品打回旧工艺，这个风险远大于"不撤销、让人处置"。

设计选择：**不引入 Saga，靠"草稿不落库 + 人确认 = commit"控制写边界**。这也和 [领域总览.md](../领域模型/领域总览.md) §5.3 的"过点主事务零分布式事务"一脉相承——能不跨事务就不跨，能用单点确认就不搞分布式协调。

**Q：为什么用 LangGraph 的 interrupt 而不用消息队列做"暂停/恢复"？**

A：LangGraph 的 `interrupt` / `Command(resume=...)` 是**图级别的暂停恢复**——暂停时 state 落 MySQL，恢复时从同一节点继续，图的其他状态（已执行步骤、中间结果、agent 假设）全保留。如果改用消息队列，需要自己实现 state 的序列化/反序列化、节点定位、恢复逻辑——本质上是在重写一个 LangGraph checkpointer。

而且 `interrupt` 是显式的图节点——在 StateGraph 的边上看得到"这里有个 gate 等人"，可审计、可追踪。消息队列的"暂停/恢复"是隐式的——你看不到图里哪些节点会等人。

**Q：为什么 L3 的 timeout 是 3600 秒而不是更长或更短？**

A：3600 秒（1 小时）是**设计目标**，不是硬上限。选取基于两个假设：换线/故障复产/客诉 8D 的最长路径（含人工确认）在经验上不会超过 1 小时；超过 1 小时说明某环节出了问题（人休假、系统卡死、agent 循环），应挂起转人工而非继续等。

同时每个 gate 有独立 deadline（比如 15 分钟），整体 timeout 是最后兜底。实际值应在试点阶段按产线数据调优——🔴 待定。

**Q：如果未来要支持"跨天的长程任务"（比如等备件维修要等 2 天），现有设计够用吗？**

A：骨架够用，需要调整两个参数。`SqlSaver` 的状态持久化本身就支持跨天——state 在 MySQL 里，放多久都行。`interrupt` 暂停也是持久的。需要改的是：
1. `timeout` 从 3600s 调到更长时间（或去掉整体 timeout，只靠 gate deadline）。
2. 增加"休眠唤醒"机制——比如备件到货后通过领域事件唤醒 session，而不是干等。
3. deadline 策略从"固定超时"改为"相对超时 + 事件驱动唤醒"。

但核心的"状态外置 + interrupt/resume + confirmation gate + 写不旁路"这套骨架不需要改。这是长程任务设计的可扩展性——**状态持久化与流程编排解耦**，流程时长从 1 小时变 1 天，改的是超时参数，不是架构。

**Q：和传统工作流引擎（Camunda、Temporal）比，L3 这套有什么不同？为什么不用它们？**

A：传统工作流引擎做的是"确定性步骤编排 + 人工任务 + 补偿"——换线 5 步的顺序、gate 等人、超时挂起，这些工作流引擎都能做。但 L3 多了一个工作流引擎做不了的东西：**在非确定决策点嵌入 agent 能力**。

- 工作流引擎能做：plan → 查首件 → gate → 查工艺 → gate → 查钢网/齐套 → barrier → 放行 → gate。
- 工作流引擎做不了：钢网 mismatch 时，自适应取证（查钢网借还记录 → 查到 ST-A 借出未还 → 自适应查上工单收线记录），根因推理（产线拿错 / 未还库 / 台账改名 / 工艺录错），草拟处置卡（路由给谁 + 建议动作）。

所以 L3 不是"用 LangGraph 替代工作流引擎"，而是"工作流引擎能做的用代码节点做（零 LLM 调用），工作流引擎做不了的用 agent 能力做"。如果某场景完全确定、没有非确定决策点，那就不该上 L3，直接上工作流引擎就够了——这是"懂什么时候不用 AI"的体现。

---

## 4. 一句话定位（收尾用）

"MES Agent 的长程任务设计，核心是 L3 编排型 Agent 的**状态外置 + 人在回路 + 写不旁路**三层一致性保证——LangGraph `SqlSaver` 把 state 持久化到 MySQL 可跨进程续跑，`interrupt`/`resume` 在 confirmation gate 等人确认时不占连接不丢状态，confirmation token 绑定 `action:target` + 过期防篡改防重放，写落库走各上下文正常应用服务过聚合根不变式 + 事务发件箱；`recursion_limit=40` + `timeout=3600s` 双闸门 + gate deadline 防无限阻塞，agent 连续失败/低置信度挂起隔离不污染整链。长程任务的一致性不靠分布式事务 ROLLBACK，靠的是'草稿不落库 + 人确认 = commit + 聚合根不变式保护写'——写的闸门始终在人手里，最坏情况退回人工编排，不会越界写、不会错误放行。"