# Agent 长程任务 · 实现细节深度剖析

> **定位**：本文从零开始，逐层拆解 流程编排能力长程任务的**全部实现细节**——从 AgentScope 2.0 的 `AgentState` 存什么、Permission gate 怎么拦截并挂起、事件系统的 HITL 确认怎么触发续跑，到 confirmation token 的 Redis 存储结构、gate deadline 怎么计时、进程重启后怎么从断点续跑、多个 session 并发怎么隔离。每一层都落到代码级细节。
>
> **阅读前提**：已读过 [FactoryAgent-流程编排能力.md](FactoryAgent/FactoryAgent-流程编排能力.md) 和 [FactoryAgent-统一架构与流程图.md](FactoryAgent/FactoryAgent-统一架构与流程图.md)，本文是它们的**实现细节补全**——面试时被追问"怎么实现的"时，本文提供可直接引用的技术细节。
>
> **口径纪律**：这是设计规划阶段的实现方案，**尚未落地**，所有代码为设计级骨架（非生产可运行），但足够回答"怎么实现"级别的追问。底座为 **AgentScope 2.0**（Python `>=3.12`）；`Agent` / `AgentState` / `SessionService` / `Permission` / `event.RequireUserConfirmEvent` / `BackgroundTaskManager` / `SchedulerManager` / `Toolkit` 取框架**已核实**公开能力名（`SessionService` 为 2.0.5 起名，旧名 `SessionManager` 已失效）。文中出现的 `self._xxx.method(...)` 一类方法签名是本项目的内部封装命名，不代表框架 API。**本框架 2.0 不提供声明式编排原语**（`MsgHub` / `sequential_pipeline` / `fanout_pipeline` 均为 1.x API，2.0.0 起模块已删除），编排形状由本仓库 `flows/` 目录的应用层 Python 代码承担。

---

## 1. 长程任务的全生命周期（先看全景）

一次换线长程任务从启动到结束，经历以下阶段：

```
HTTP POST /agent/orchestration/changeover/start
  -> OrchestrationService.start()
    -> agent_session 写入 MySQL (status=PLANNING)
    -> BackgroundTaskManager 提交 _drive(session)   ← 异步点火，HTTP 立即返回 session_id
    -> 返回 session_id 给调用方

BackgroundTaskManager 托管的后台任务:
  _drive(session)
    -> flows/ 编排：run(initial_state, session_id)  ← 请求内编排（应用层 async 代码）开始执行
      -> plan_node (代码)                     ← 每一步都把 AgentState 写入 SessionService
      -> first_article (代码)
      -> gate_first_article                   ← Permission gate 拦截！AgentState 落 Session store，任务挂起
        ... 等人确认 ...
        POST /agent/orchestration/{session_id}/confirm   ← 人确认
          -> 事件系统投递确认事件              ← 触发续跑
      -> process_switch (代码)
      -> gate_process_switch                  ← 再次 Permission gate 拦截...
        ... 等人确认 ...
      -> tooling_check ‖ kitting_check (并行) ← asyncio.gather 两条分支并发
      -> barrier_node (代码)
      -> ... 继续直到 done_node
    -> flows/ 编排返回
  -> session.status = DONE
```

**关键认知**：整个长程任务的生命周期由四个组件协作完成：
1. **BackgroundTaskManager**：托管编排执行的后台协程任务，gate 挂起时让出 CPU 但不销毁
2. **SessionService（Redis-backed）+ `AgentState`**：显式状态外置，让任务可以跨进程存活、任意副本恢复
3. **`event.RequireUserConfirmEvent` + 事件系统**：HITL 确认的挂起/续跑由框架**已核实**的事件原语驱动，不是进程内的锁或忙等
4. **Permission 系统（Redis 确认记录）**：把 gate 的下钻拦截落在每一次工具执行上；它是**叠加层**——确认记录/令牌的**主闸在工具网关**（绑定"具体动作 + 具体 target + 具体用户"的一次性令牌，用后即废、过期 fail closed），见 §5、§7 与 ADR-04

### 1.1 诚实边界：框架只承担"请求内协同"（ADR-05）

本文描述的挂起/续跑，真实边界如下，越界的部分不由 Agent 框架承担：

| 需求 | 承担者 | 说明 |
|------|--------|------|
| 单请求内的顺序步骤、只读并行取证、条件分流、barrier 汇合 | **本仓库 `flows/` 目录的应用层 Python 代码** | 顺序=应用层 `await` 串联；并行=`asyncio.gather` 并发多个**各自独立**的 `Agent.reply()`，结果在应用层显式合并。2.0 无 `pipeline`/`MsgHub` 原语可用 |
| 会话状态持久化、按 `session_id` 任意副本恢复 | 框架 `AgentState` + `SessionService`（Redis-backed） | 会话持久化 ≠ durable workflow |
| 人确认的挂起与事件驱动续跑 | 框架 `event.RequireUserConfirmEvent` + 事件系统 | 框架侧真实可用的"挂起/续跑"原语只有这一组 |
| 后台任务托管、定时 deadline | 框架 `BackgroundTaskManager` / `SchedulerManager` | 同为请求内/进程内的任务托管 |
| **跨天 / 跨进程的持久编排**：流程定义、步骤状态机、gate deadline 超时 `SUSPENDED`、严格重试语义、可审计补偿、崩溃续跑的流程推进 | **工作流引擎层（Temporal 类，选型待定）** | ⚠️ **不由 Agent 框架承担**。AgentScope 只在引擎调用的单个节点内部跑 `Agent` 推理与工具执行；节点内并发用 `asyncio.gather`，汇合即返回，**不持有流程状态** |

**红线**：跨天 gate 一律在工作流引擎侧；`flows/` 只做请求内协同。过点主事务 200ms P99 不得被任何编排层触碰，Agent 与编排全部在旁路。

---

## 2. 状态持久化：AgentState + SessionService 详解

### 2.1 AgentScope 2.0 的显式状态机制

AgentScope 2.0 把"跑到一半的状态"建成**显式类型化的 `AgentState`**，而不是引擎内部的 channel 快照。编排每执行一步，就把新的 `AgentState` 交给 `SessionService` 持久化，落点是 **Redis-backed storage**。一份会话状态里承载：
- 当前全部业务字段值（工单、路线版本、各步骤结果、gate 决策）
- 当前执行到哪个步骤（进度指针）
- 会话/租户上下文与消息（`Msg`）历史，供恢复后继续推理与计费溯源

`SessionService` 随 FastAPI Agent Service 的 lifespan 装配，会话状态与进程解耦——这是"无状态水平扩展"的地基。

### 2.2 存储分工：Session store 管续跑，MySQL 管台账

原方案用 MySQL 三张 checkpoint 表持久化图状态；本架构下 **MySQL 不再承担会话 checkpoint**，分工变成：

| 落点 | 介质 | 存什么 | 键 | 角色 |
|------|------|--------|----|------|
| 会话状态快照 | **Redis-backed Session store**（`SessionService`） | 恢复续跑所需的完整 `AgentState` | `session_id`（能力节点子会话：`{session_id}:{capability}`） | 跨进程恢复的**唯一依赖** |
| `agent_session` | MySQL（业务可观测表） | 会话生命周期、状态机、失败计数、挂起原因 | `session_id` | 编排台账，不参与续跑 |
| `agent_step_record` / `node_trace` | MySQL（业务可观测表） | 每步执行记录（CODE/AGENT 性质、gate 决策、工具调用引用） | `(session_id, step)` | 审计与工程师回溯 |
| `tool_call_trace` | MySQL（业务可观测表） | 工具调用全文证据链 | `trace_id` | "证据不空"红线 |
| `AgentState` 归档 | MySQL（**可选**） | 状态快照的历史留痕 | `session_id` + 快照序号 | 只做审计，不做恢复 |

**要点**：续跑路径只读 Redis-backed Session store；MySQL 侧全部是可观测/审计用途，挂了这个库也不影响"能不能恢复"。

### 2.3 session_id 作为恢复键

```python
# app/application/orchestration_service.py
run_ctx = {
    "session_id": session.id,     # ← session_id 直接作为恢复键
    "step_budget": 40,            # Agent 步数与工具调用预算上限（示意，非框架 API 名）
}
```

**为什么用 `session_id` 作恢复键**：
- `session_id` 是会话隔离键——不同会话的 `AgentState` 完全隔离。
- 一个 编排能力会话 = 一份 `AgentState` + 一条 Session store 记录，天然一一对应。
- 恢复时只需知道 `session_id`，**任意副本**都能从 Session store 找回状态并续跑；不需要"同进程""同线程"这类前提。
- 单会话的步数与工具调用次数由预算上限封顶（超限即 FAILED），不依赖图递归计数器。

### 2.4 AgentState 里到底存了什么

以换线场景为例，state 在 gate 处挂起等待确认时，`AgentState` 快照展开后大致是：

```json
{
  "session_id": "S-20260712-001",
  "tenant": {
    "tenant_id": "WS-A",
    "workshop": "SMT-1",
    "line": "L-01",
    "scopes": ["WORKSHOP", "LINE"]
  },
  "scenario": "CHANGEOVER",
  "work_order_id": "WO-2026-0701",
  "target_route_id": "RR-B",
  "target_route_version": "v4",
  "asset_id": "ASSET-01",
  "status": "RUNNING",
  "current_step": "GATE_FIRST_ARTICLE",
  "first_article_result": {"status": "PASS", "article_id": "FA-001"},
  "gate_first_article": "PASS",
  "process_switch_result": {"route_id": "RR-B", "version": "v4", "status": "ACTIVE"},
  "gate_process_switch": null,
  "tooling_result": null,
  "kitting_result": null,
  "barrier_route": null,
  "agent_hypothesis": null,
  "agent_confidence": null,
  "action_card": null,
  "pending_tool_calls": [],
  "tool_results": []
}
```

**关键点**：
- state 是完整快照，包含所有已执行步骤的中间结果——显式字段，不是引擎私有结构。
- gate 拦截时推给责任人的**动作卡以事件形式流出**，快照里持久化的是 state 本身（`action_card` 字段只在需要"这步要落库"时留一份意图）。
- 恢复时，从 Session store 取最新 `AgentState`，然后从 gate 拦截点的下一步继续执行。

### 2.5 状态快照序列

```
snapshot_1 (parent=null)   ← plan_node 执行完
  ↑
snapshot_2 (parent=snap_1) ← first_article 执行完
  ↑
snapshot_3 (parent=snap_2) ← gate_first_article 挂起等待确认处
  ↑
snapshot_4 (parent=snap_3) ← process_switch 执行完（确认后续跑）
  ↑
...
```

- 每次 `AgentState` 更新都带 parent 指针，形成可追溯的快照序列（可选归档到 MySQL 审计表）。
- 编排能力不用回退（gate 确认后不撤销），序列只服务审计与"当时到底看到了什么"的复盘。

---

## 3. gate 挂起 / 续跑的底层机制

### 3.1 Permission gate 拦截时发生了什么

```python
# gate 代码节点：写动作执行前，由 Permission 策略（gated_write_policy）要求人工确认
async def await_confirmation(self, session_id: str, step: str,
                             card: ActionCard) -> str:
    await self._dispatcher.push(card)        # ① 经事件系统推送动作卡
    confirmation = await self._permission.await_hitl(session_id, step, card)  # ② 挂起等待确认事件
    # ⬆ 执行到这里挂起，直到事件系统投来一条确认事件
    # ⬇ 人确认后，从下面继续执行
    if not confirmation.valid_for(card.writes_via_action()):
        return "REJECT"
    return "PASS" if confirmation.approved else "REJECT"
```

Permission gate 拦截的内部行为：

1. **先落状态**：当前 `AgentState` 写入 SessionService（Redis-backed），保证"进程可以死、状态不会丢"。
2. **拦住工具执行**：`writes_via` 指向的写工具此刻**不被执行**——闸门下沉到每一次工具执行，而不是只拦在提示词或流程说明里。
3. **会话进入待确认态**：gate 步骤标 `GATE_WAITING`，挂起是持久化的会话事实，不是进程内的锁或忙等。
4. **流出确认请求**：动作卡经事件系统（`AGUIProtocolMiddleware`）实时渲染给责任人，Kafka 作离线兜底；协程让出但不销毁。

> **命名核对**：框架侧真实可用且已核实的挂起/续跑原语是 **`event.RequireUserConfirmEvent`**（HITL 确认事件）+ `SessionService` + `BackgroundTaskManager`/`SchedulerManager`；文中的 `await_hitl(...)` / `GateConfirmed` / `record_confirmation(...)` 是本项目在上述事件流之上的**封装命名，不是框架 API**。框架 2.0 没有"图级 interrupt/resume"这类原语，本节机制的全部依据就是这一组已核实事件。

### 3.2 确认事件如何触发续跑

```python
# POST /agent/orchestration/{session_id}/confirm
@router.post("/agent/orchestration/{session_id}/confirm")
async def confirm_gate(session_id: str, req: ConfirmRequest, ...):
    token = await self._permission.record_confirmation(
        session_id, req.step, req.approved, req.user_id)   # 签发确认记录（Redis）
    # 确认事件进入事件总线，由事件系统按 session_id 重新驱动编排任务（任意副本可接管）
    await self._events.publish(GateConfirmed(
        session_id=session_id, step=req.step, token_id=token.id))
```

续跑的内部行为：

1. **按 `session_id` 加载最新 AgentState**：`SessionService` 从 Redis-backed storage 取回状态并反序列化。
2. **找到挂起的 gate**：定位到处于 `GATE_WAITING` 的那一步。
3. **把确认记录交回 gate 判定**：`confirmation.valid_for(card.writes_via_action())` 通过才允许写工具执行。
4. **继续执行**：从 gate 拦截点的下一行开始（`if not confirmation.valid_for(...)` 之后的分支）。

### 3.3 为什么挂起不占执行槽位

挂起不是把 Pod 卡住，而是三段解耦：状态外置（Session store）+ 执行让出（协程不销毁但不再消耗 CPU）+ 续跑由事件驱动（一次确认 = 一次新的任务驱动，可能落在另一个副本）。

```python
# 第一次驱动：启动编排，跑到 gate 挂起
await self._orchestrator.run(initial_state, session_id=session_id)
# -> gate 拦截，AgentState 已落 Session store，本次驱动结束（不占槽等待）

# 第二次驱动（可以由别的副本、由确认事件触发）：
await self._orchestrator.resume(session_id)      # 项目内部封装名
# -> 按 session_id 取回 AgentState，gate 拿到确认记录后继续执行
# -> 如果又遇到 gate，再次挂起等待
```

> 关键差别：等待期**不绑定任何协程槽位**，也不要求"唤醒同一个在途调用"——一次确认就是一次新的事件驱动续跑，人等一小时只占事件存储（Session store 里的待确认记录），不占一个 worker。

### 3.4 跨进程恢复的完整时序

```
副本 A (agent-service pod-1):
  BackgroundTaskManager 托管 _drive(session)
    -> 编排 run(initial_state, session_id=S-001)
      -> plan_node ✓
      -> first_article ✓
      -> gate_first_article -> Permission gate 拦截！AgentState 落 Redis-backed Session store
      -> 本次驱动结束，会话状态=待确认，协程让出

  ──── pod-1 被 OOM Kill 或滚动更新重启 ────

副本 B (agent-service pod-2):
  # 人确认了，POST /confirm 到达，确认事件进入事件系统
  -> 按 session_id=S-001 从 Session store 取回 AgentState
    -> 反序列化 state（gate_first_article 处）
    -> 确认记录交回 gate 判定
    -> 继续执行: process_switch -> gate_process_switch -> ...
```

**关键**：副本 A 和副本 B 可以是完全不同的进程，甚至不同的 Pod——只要 `session_id` 相同、Redis 可达，就能从断点续跑。恢复路径不碰 MySQL。

---

## 4. 异步驱动：BackgroundTaskManager / SchedulerManager

### 4.1 为什么不自建 Celery worker，也不裸用 create_task

```python
# OrchestrationService.start()
async def start(self, req: Request, tenant: TenantContext) -> Session:
    session = await self._sessions.create(req, tenant)
    await self._background.submit(self._drive(session, tenant))  # ← 框架托管后台任务
    return session  # 立即返回
```

| 方案 | 特点 | 为什么不选 |
|------|------|-----------|
| FastAPI `BackgroundTasks` | 请求结束后执行，与请求生命周期绑定 | 无法跨请求恢复——gate 等人时 background task 虽然活着，但没法从另一个 HTTP 请求唤醒 |
| Celery / 自建 worker 队列 | 独立 worker 进程，任务持久化到 broker | 会话状态已由 `SessionService` 外置，再叠一层 broker 属重复建设；还要多运维一套队列与 beat |
| 裸 `asyncio.create_task` | 同事件循环的协程 Task | 任务生命周期、重启遗孤、并发上限全得自己写 |
| **`BackgroundTaskManager` / `SchedulerManager`**（选定） | 框架托管的后台任务 + 定时任务，随 Agent Service lifespan 装配 | 长程编排与定时巡检由框架托管；协程语义与事件系统同循环衔接，确认事件可直接驱动续跑 |

**选择理由**：编排能力的"暂停/续跑"由 **Permission gate + 事件系统 HITL 确认**承载，不是消息队列的"暂停消费/恢复消费"。挂起时状态已在 Session store，续跑由确认事件触发，所以需要的是"**托管的协程任务 + 外置状态**"，而不是自建 worker 池。定时巡检类（如扫未归还钢网）走 `SchedulerManager`，不再自己维护调度器。

### 4.2 Task 的生命周期管理

```python
class OrchestrationService:
    def __init__(self, ..., background: BackgroundTaskManager):
        self._background = background
        self._active_tasks: dict[str, TaskHandle] = {}  # session_id -> 托管任务句柄（项目内部封装名）

    async def start(self, req, tenant):
        session = await self._sessions.create(req, tenant)
        handle = await self._background.submit(self._drive(session, tenant))
        self._active_tasks[session.id] = handle
        handle.add_done_callback(lambda t: self._on_task_done(session.id, t))
        return session

    def _on_task_done(self, session_id: str, handle: TaskHandle):
        self._active_tasks.pop(session_id, None)
        if handle.exception():
            # 记录异常，session 状态由 _drive 内部的 try/except 管理
            pass
```

- `BackgroundTaskManager` 负责"任务别被请求生命周期带走"；`self._active_tasks` 只是本项目为"同副本内别重复点火同一会话"维护的映射。
- 副本重启后这张映射表清空**不影响恢复**：待确认的会话事实记在 Session store，确认事件到来时由任意副本重新提交任务。

### 4.3 超时实现

```python
async def _drive(self, session, tenant):
    try:
        await asyncio.wait_for(
            self._orchestrator.run(
                {"session_id": session.id, ...},
                session_id=session.id,
                step_budget=40,   # Agent 步数与工具调用预算上限（示意名）
            ),
            timeout=3600.0,   # 1 小时
        )
    except asyncio.TimeoutError:
        await self._sessions.mark_failed(session, "整体超时")
    except StepBudgetExceeded:
        await self._sessions.mark_failed(session, "步数超限")
```

**`asyncio.wait_for` 的计时细节**：
- `timeout=3600.0` 是从 `_drive` 开始计时，**包含** gate 等人的时间。
- gate 挂起释放的是执行槽（协程让出、状态外置），不是墙钟——`wait_for` 的计时器**继续走**。
- 如果人在 gate 处等了 1 小时零 1 秒才确认，`asyncio.wait_for` 会在 1 小时整时抛出 `TimeoutError`，本次驱动失败，会话按超时处理。

**这意味着**：1 小时的 timeout 是"从启动到完成"的总时长，包括所有人确认等待时间。如果单个 gate 确认可能等很久，需要确保 timeout 足够大，或者后续优化为"只计非等待时间"。

---

## 5. Permission 确认记录：Token 的签发与校验

> Permission 系统把"闸门"下沉到每一次工具执行，但**确认这件事仍然要有可持久、可校验、可幂等的载体**。本项目里它落成 Redis 上的确认记录存储（代码角色名保留 `ConfirmationStore`，语义上已并入 Permission，不再是与 Permission 并列的自研三件套）。

### 5.1 Redis 存储结构

```python
# app/infrastructure/permission_/confirmation_store.py
import hashlib, secrets, time
from redis.asyncio import Redis

class ConfirmationStore:
    """Permission 的确认记录存储：gate 确认 token 的签发、校验、废除。"""

    def __init__(self, redis: Redis, token_ttl: int = 1800):  # 默认 30 分钟过期
        self._redis = redis
        self._ttl = token_ttl

    async def issue(self, session_id: str, step: str,
                    approved: bool, user_id: str) -> "ConfirmationToken":
        token_id = secrets.token_hex(16)  # 32 字符随机 hex
        action = f"{session_id}:{step}"
        payload = {
            "token_id": token_id,
            "session_id": session_id,
            "step": step,
            "action": action,
            "approved": approved,
            "user_id": user_id,
            "issued_at": int(time.time()),
        }
        # 存 Redis: key = "confirm:{token_id}", value = JSON, TTL = 30min
        await self._redis.setex(
            f"confirm:{token_id}",
            self._ttl,
            json.dumps(payload),
        )
        # 同时记录该 session+step 的 token，防止重复确认
        await self._redis.setex(
            f"confirm:session:{session_id}:{step}",
            self._ttl,
            token_id,
        )
        return ConfirmationToken(
            id=token_id,
            action=action,
            approved=approved,
            user_id=user_id,
            issued_at=payload["issued_at"],
        )

    async def validate(self, token_id: str, expected_action: str) -> bool:
        raw = await self._redis.get(f"confirm:{token_id}")
        if raw is None:
            return False  # token 不存在或已过期
        payload = json.loads(raw)
        if payload["action"] != expected_action:
            return False  # action 不匹配（防篡改）
        return True
```

### 5.2 Token 的结构

```python
@dataclass(frozen=True)
class ConfirmationToken:
    id: str              # 32 字符 hex，如 "a1b2c3d4e5f6..."
    action: str          # "S-001:PROCESS_SWITCH" —— 绑定 session+step
    approved: bool       # 人点了确认还是拒绝
    user_id: str         # 谁确认的
    issued_at: int       # Unix 时间戳

    def valid_for(self, expected: str) -> bool:
        """校验 token 是否适用于指定的写动作。"""
        return self.action == expected
```

### 5.3 Token 校验流程

```
人确认
  -> POST /confirm {session_id, step, approved, user_id}
  -> ConfirmationStore.issue() -> 生成 token_id，存 Redis（Permission 确认记录）
  -> 确认事件进入事件系统 -> 按 session_id 触发续跑，取回 AgentState
  -> gate 节点继续执行:
      if not confirmation.valid_for(card.writes_via_action()):
          return "REJECT"        ← token 与写动作不匹配，拒绝
      return "PASS" if confirmation.approved else "REJECT"
  -> 若 PASS，Permission 才放行写工具执行，gate 节点返回
  -> write_via_appservice 节点拿 token 调 ACL 写客户端（Toolkit 工具）
  -> ACL 写客户端调 REST 时，header 带 X-Confirmation-Token: {token_id}
  -> 应用服务收到后，可选地回调 ConfirmationStore.validate() 二次校验
```

---

## 6. ActionCardDispatcher：动作卡推送

### 6.1 双通道推送（事件流 + Kafka 兜底）

```python
# app/application/action_card_dispatcher.py
class ActionCardDispatcher:
    """推送动作卡到责任人：事件系统（实时，经 AGUI 事件流渲染）+ Kafka（持久兜底）。"""

    def __init__(self, events: EventBus,                 # 事件系统（AgentScope 事件总线）
                 kafka_producer: ActionCardProducer):
        self._events = events
        self._kafka = kafka_producer

    async def push(self, card: ActionCard) -> None:
        # 1. 查该 session 的当前责任人（从 session context 或工位映射表）
        assignee = await self._resolve_assignee(card)

        # 2. 事件系统实时推：动作卡作为事件流出，经 AGUIProtocolMiddleware 渲染到责任人界面
        await self._events.publish(
            ActionCardIssued(card=card),
            audience=assignee.user_id,
        )

        # 3. Kafka 持久化（离线也能收到、可回溯）
        await self._kafka.send(
            topic="agent.action_cards",
            key=card.session_id,
            value=card.model_dump_json(),
            headers={
                "card_id": card.card_id,
                "assignee": assignee.user_id,
                "deadline": str(card.deadline) if card.deadline else "",
                "traceparent": get_current_traceparent(),
            },
        )
```

- **在线责任人**：走事件流（`reply_stream` 产出的 agent events 经 `AGUIProtocolMiddleware` 流式到前端），动作卡与 HITL 确认同源，不需要自研 WS 连接管理。
- **离线/审计**：Kafka 保留为跨服务事件总线与动作卡的离线兜底通道，重复推送由 `card_id` 去重。

### 6.2 动作卡的数据结构

```python
class ActionCard(BaseModel):
    card_id: str                    # UUID
    session_id: str                 # 所属会话
    step: str                       # "PROCESS_SWITCH" / "RELEASE" / "DISPOSITION"
    capability: str | None          # "root_cause" / "fault_impact" / None
    intent: str                     # "激活工艺路线 RR-100 v4"
    draft_payload: dict             # 草稿内容
    writes_via: str                 # "工艺管理上下文.application.activate_route"
    requires_confirmation: bool = True
    evidence: list[str]             # ["trace_id=T-101", "trace_id=T-102"]
    agent_hypothesis: dict | None   # {"root_cause": "上工单未还库", "confidence": "high"}
    confidence: str | None          # "high" / "medium" / "low"
    risk_note: str                  # "激活后将影响当前产线所有在制品"
    deadline: datetime | None       # 超时时间
```

### 6.3 deadline 的计时与超时处理

```python
# Permission gate 的 deadline 计时（gate 侧内部封装）
class GateConfirmationFlow:
    def __init__(self, ..., scheduler: SchedulerManager):
        self._scheduler = scheduler
        self._deadlines: dict[str, TaskHandle] = {}   # card_id -> 一次性延时任务句柄

    async def await_confirmation(self, session_id, step, card):
        await self._dispatcher.push(card)

        if card.deadline:
            # 启动 deadline 计时器：由 SchedulerManager 托管的一次性延时任务（示意名）
            handle = await self._scheduler.schedule_once(
                at=card.deadline, fn=lambda: self._deadline_watch(session_id, step, card))
            self._deadlines[card.card_id] = handle

        confirmation = await self._permission.await_hitl(session_id, step, card)

        # 人确认了，取消 deadline 计时器
        if card.card_id in self._deadlines:
            self._deadlines[card.card_id].cancel()
            del self._deadlines[card.card_id]

        if not confirmation.valid_for(card.writes_via_action()):
            return "REJECT"
        return "PASS" if confirmation.approved else "REJECT"

    async def _deadline_watch(self, session_id, step, card):
        """deadline 计时器：超时则挂起 session。"""
        delay = (card.deadline - datetime.now()).total_seconds()
        if delay <= 0:
            await self._suspend_session(session_id, step, "deadline 已过")
            return
        # 超时了，人还没确认
        await self._suspend_session(session_id, step, "deadline 超时")
        # 注意：待确认记录仍在 Redis（未过期），需要显式收口
        # 实际实现中，由事件系统投入一条 rejected 确认事件，让该步骤以 REJECT 落定
        await self._events.publish(GateRejected(session_id=session_id, step=step,
                                                reason="deadline 超时"))

    async def _suspend_session(self, session_id, step, reason):
        await self._repo.update_status(session_id, "SUSPENDED")
        await self._dispatcher.push_exception_card(session_id, step, reason)
```

**为什么 deadline 用 SchedulerManager 而不是 gate 协程里的 `asyncio.sleep`**：gate 挂起后本次驱动已经结束（不占执行槽），计时职责必须独立于 gate 协程存活——这正是 `SchedulerManager` 托管定时任务的用途；副本重启后可按待确认记录里的 `deadline` 重新登记。

---

## 7. 写路径：从 Agent 草拟到应用服务落库

### 7.1 完整写路径（以"批次隔离下达"为例）

```
Step 1: FaultImpactAgent (B) 推理
  -> 调设备遥测 ACL -> 故障模式 = "软漂移"
  -> 调 FMEA ACL -> 漂移参数 × 产品敏感度
  -> 调批次 ACL -> 窗口内批次 = [B1, B2, B3]
  -> 草拟隔离卡: draft_isolation_card(batches=[B1,B2,B3], reason="...")

Step 2: gate: ISOLATION
  -> 动作卡推给质量工程师
  -> 工程师点开 evidence trace_id，验证 agent 推理
  -> 工程师确认 -> POST /confirm

Step 3: gate 返回 PASS
  -> Permission 放行该写工具执行，write_via_appservice 节点运行
  -> 调 ReworkWriteAclClient.issue_isolation(batches, reason, confirmation)

Step 4: ReworkWriteAclClient
  -> 校验 confirmation token 有效性
  -> POST /api/isolation-orders {batches, reason, confirmation_id}
  -> header: X-Confirmation-Token: {token_id}

Step 5: 返工上下文应用服务 (Java)
  -> IsolationOrderApplicationService.issue()
  -> IsolationAggregate.issue(batch_set, reason)
    -> 聚合根不变式校验：
      - 批次状态必须允许隔离
      - 隔离原因不能为空
      - ...
  -> 事务提交：isolation_order 行 + outbox_event(BatchIsolated) 同一事务
  -> 返回 201 Created

Step 6: Agent 收到 201
  -> gate_decision = PASS 落 agent_step_record
  -> 编排继续执行下一步
```

### 7.2 ACL 写客户端的实现细节（封装为 Toolkit 工具）

```python
class ReworkWriteAclClient:
    """返工上下文写 ACL——只接受带 confirmation token 的请求。"""

    def __init__(self, http_client: httpx.AsyncClient,
                 confirmation_store: ConfirmationStore,
                 base_url: str = "http://rework-service"):
        self._http = http_client
        self._store = confirmation_store
        self._base_url = base_url

    async def issue_isolation(
        self, batch_set: list[str], reason: str,
        confirmation: ConfirmationToken, tenant: TenantContext,
    ) -> IsolationResult:
        # 1. 校验 token
        expected_action = f"isolation.issue:{tenant.tenant_id}"
        if not confirmation.valid_for(expected_action):
            raise PermissionError(f"token action 不匹配")
        if not await self._store.validate(confirmation.id, expected_action):
            raise PermissionError("token 无效或已过期")

        # 2. 调应用服务 REST
        resp = await self._http.post(
            f"{self._base_url}/api/isolation-orders",
            json={
                "batches": batch_set,
                "reason": reason,
                "confirmation_id": confirmation.id,
                "confirmed_by": confirmation.user_id,
            },
            headers={
                **tenant.headers(),
                "X-Confirmation-Token": confirmation.id,
                "X-Confirmed-By": confirmation.user_id,
            },
            timeout=3.0,
        )
        resp.raise_for_status()
        return IsolationResult.model_validate(resp.json())
```

### 7.3 写的幂等性

rework-service 的 `/api/isolation-orders` 需要做幂等：
- 用 `confirmation_id` 作为幂等键。
- 如果同一个 `confirmation_id` 已经处理过，返回 200 OK（不重复创建隔离单）。
- 这样 Agent 侧重试不会导致重复写。

---

## 8. AgentState 的完整定义（OrchestrationState）

```python
# app/domain/orchestration.py
from pydantic import BaseModel
from typing import Any
from datetime import datetime

class TenantContext(BaseModel):
    tenant_id: str
    workshop: str
    line: str
    scopes: list[str]

class OrchestrationState(BaseModel):
    """流程编排的显式状态（AgentState）：类型化字段即续跑与审计的唯一事实。"""

    # 会话标识
    session_id: str = ""
    scenario: str = ""                  # CHANGEOVER / FAULT_RESPONSE / COMPLAINT_8D / PROCESS_CHANGE

    # 租户上下文
    tenant: TenantContext | None = None

    # 业务上下文
    work_order_id: str | None = None
    batch_id: str | None = None
    asset_id: str | None = None
    target_route_id: str | None = None
    target_route_version: str | None = None

    # 会话状态
    status: str = "PLANNING"            # PLANNING / RUNNING / SUSPENDED / DONE / FAILED
    current_step: str = ""

    # 各步骤结果
    first_article_result: dict | None = None
    gate_first_article: str | None = None       # PASS / REJECT
    process_switch_result: dict | None = None
    gate_process_switch: str | None = None
    tooling_result: dict | None = None          # {"status": "PASS/FAIL", "code": "...", "expected": ..., "actual": ...}
    kitting_result: dict | None = None
    barrier_route: str | None = None             # draft_release / root_cause / suspend
    agent_hypothesis: dict | None = None         # agent 产出的根因假设
    agent_confidence: str | None = None          # high / medium / low
    action_card: dict | None = None
    gate_disposition: str | None = None
    gate_release: str | None = None
    retry_tooling: bool = False

    # agent 调用相关
    pending_tool_calls: list[dict] = []
    tool_results: list[dict] = []

    # 时间戳
    created_at: str = ""
    updated_at: str = ""
```

---

## 9. 并发隔离：多个 session 同时跑

### 9.1 隔离机制

```
session S-001 (session_id=S-001)  ->  编排执行实例 A（同一份 `flows/` 编排定义）
session S-002 (session_id=S-002)  ->  编排执行实例 B
session S-003 (session_id=S-003)  ->  编排执行实例 C
```

- 同一份 `flows/` 编排定义（换线）可以被多个 session 并发执行——Agent 是无状态生产者，状态在 Session store。
- 每次驱动互相独立——`AgentState` 按 `session_id` 隔离。
- `SessionService` 以 `session_id` 为键写 Redis-backed storage，同一会话的状态更新串行落盘（单写者），不同会话互不影响；跨副本并发确认由待确认记录的 `confirm:session:{session_id}:{step}` 幂等键挡住重复。

### 9.2 能力节点的会话隔离

```python
# 编排调用某个能力 Agent 时，使用独立的子会话 id
def _run_agent(self, capability: str):
    async def fn(state: OrchestrationState) -> OrchestrationState:
        agent = self._agents.get(capability)   # 该能力的 Agent 实例（共享定义，无状态）
        # 每次调用都是独立的——状态由入参与子会话 id 决定，不在实例里攒
        result = await agent.reply(
            state,
            session_id=f"{state.session_id}:{capability}",
            #                              ↑ 注意：能力节点用独立的子会话 id
        )
        ...
    return fn
```

**关键**：能力节点的子会话 id 是 `{session_id}:{capability}`，不是 `{session_id}`——主编排状态与能力节点的推理状态在 Session store 里分开存放，不会互相覆盖。这是"多 session 并发 + 能力节点独立"的隔离手段（`flows/` 里的编排形状——应用层顺序 `await` 还是 `asyncio.gather` 并发——只决定谁在什么时机被调用，状态隔离始终靠会话键）。

### 9.3 工具集隔离（安全边界在工具网关层）

```python
class ToolRegistry:                        # 工具描述符注册在 AgentScope Toolkit 内
    def tools_for(self, capability: str, tenant: TenantContext) -> list[ToolDescriptor]:
        return [
            d for d in self._descriptors.values()
            if d.capability == capability
            and tenant.can_access(d.required_tenant_scopes)
        ]
```

- 按能力收敛工具可见集合（A/B/C/D 各自的 `Toolkit` 子集），只是**降低误用概率的提示层收敛**，不是安全边界。
- **真正的边界建在工具网关**（ADR-04，顺序不可颠倒）：**主闸**是网关侧的服务端二次鉴权 + 参数 schema 强校验/白名单 + 幂等键两把 + **绑定"动作 + target + 用户"的一次性令牌**（用后即废、过期 fail closed）。框架 `Permission` 是**叠加层**：把闸门下钻到 Agent 循环内的每一次工具执行，按本仓库自定义的三套规则集 `readonly_policy` / `nowrite_policy` / `gated_write_policy`（在 `EXPLORE` 只读档位与自定义 deny 规则之上组合，**不是框架类名**）+ `tenant` scope 校验做拦截。绕开 Agent 循环直接调函数时 Permission 不生效，因此**内层失效不得导致外层放行**。
- 不同 session 的同一能力调用共享同一套工具描述符（只读，线程安全），但执行时 tenant 过滤确保数据隔离。
- 成本项提醒：能力节点之间一旦传递彼此的中间结论，**新增的 Agent 间通道本身是攻击面**——转入的内容必须当作"不可信输入"过同一套执行层校验，不能因为"来自另一个 Agent"就免检。（注：AgentScope 2.0 已删除 `MsgHub` / `sequential_pipeline` / `fanout_pipeline`，也没有 Handoffs/Routing 移交原语；本设计的"多 Agent 共享上下文"只能是**各分支只读同一份 `evidence_set_id`、彼此不看对方中间结论**，靠 `fetch_evidence_set` 显式回查，不做成员间广播。）

---

## 10. 失败恢复的完整状态机

### 10.1 session 的状态流转

```
PLANNING -> RUNNING -> DONE
                   -> SUSPENDED -> (手动恢复) -> RUNNING -> DONE
                   -> FAILED
```

### 10.2 各状态的含义与触发条件

| 状态 | 含义 | 触发条件 |
|------|------|---------|
| PLANNING | 会话刚创建，编排尚未开始执行 | `OrchestrationService.start()` 创建 session 时 |
| RUNNING | 编排正在执行，或 gate 等待人确认 | `_drive` 开始后 |
| SUSPENDED | 流程挂起，等待人工干预 | gate deadline 超时 / agent 连续失败 2 次 / barrier 检测到缺料 |
| DONE | 正常完成 | `done_node` 执行完毕 |
| FAILED | 异常终止 | Agent 步数与工具调用预算超限 / 整体 timeout / 未捕获异常 |

### 10.3 SUSPENDED 后的恢复

```python
class OrchestrationService:
    async def resume_suspended(self, session_id: str,
                               user_decision: str,  # "RETRY" / "ABORT" / "SKIP"
                               tenant: TenantContext) -> Session:
        session = await self._sessions.get(session_id)
        if session.status != "SUSPENDED":
            raise InvalidStateError(f"session {session_id} 不是 SUSPENDED 状态")

        if user_decision == "RETRY":
            # 重新驱动：按 session_id 取回 AgentState，从断点续跑
            session.status = "RUNNING"
            await self._sessions.save(session)
            await self._background.submit(self._drive(session, tenant))
        elif user_decision == "ABORT":
            session.status = "FAILED"
            await self._sessions.save(session)
        elif user_decision == "SKIP":
            # 跳过当前步骤：先写一条人工状态更新，再由事件系统触发续跑
            session.status = "RUNNING"
            await self._sessions.save(session)
            await self._sessions.update_state(
                session_id, {"skip_current_step": True})   # 直接改 AgentState
            await self._events.publish(StepSkipped(session_id=session_id))
        return session
```

---

## 11. agent 连续失败的计数与隔离

```python
# flows/barrier.py（本仓库编排代码目录；`flows/` 只做请求内协同）
class FailureTracker:
    """追踪同一个 session 内 agent 调用的连续失败次数。"""

    def __init__(self, repo: Repo):
        self._repo = repo

    async def record_agent_result(self, session_id: str, capability: str,
                                  result: dict) -> bool:
        """
        记录 agent 调用结果，返回 True 表示应该继续，False 表示应该挂起。
        """
        if result.get("status") == "SUCCESS":
            await self._repo.reset_failure_count(session_id, capability)
            return True

        count = await self._repo.increment_failure_count(session_id, capability)
        if count >= 2:
            await self._repo.update_status(session_id, "SUSPENDED")
            await self._repo.log_suspend_reason(
                session_id, capability,
                f"agent {capability} 连续失败 {count} 次，已挂起"
            )
            return False
        return True
```

```python
# 在 _run_agent 中使用
def _run_agent(self, capability: str):
    async def fn(state: OrchestrationState) -> OrchestrationState:
        agent = self._agents.get(capability)     # 该能力的 Agent（内置 ReAct）
        try:
            result = await agent.reply(
                state, session_id=f"{state['session_id']}:{capability}")
            # 记录成功
            await self._failure_tracker.record_agent_result(
                state["session_id"], capability, {"status": "SUCCESS"})
            state["agent_hypothesis"] = result["hypothesis"]
            state["agent_confidence"] = result["confidence"]
        except Exception as e:
            should_continue = await self._failure_tracker.record_agent_result(
                state["session_id"], capability, {"status": "FAILED", "error": str(e)})
            if not should_continue:
                state["status"] = "SUSPENDED"
                state["agent_hypothesis"] = {"error": str(e)}
                state["agent_confidence"] = "low"
            else:
                state["agent_hypothesis"] = {"error": str(e)}
                state["agent_confidence"] = "low"
        return state
    return fn
```

---

## 12. Permission 策略装配与启动校验：红线的完整实现

写闸门的**主闸在工具网关**——出站写调用必须携带绑定"具体动作 + 具体 target + 具体用户"的一次性令牌，网关校验并消费后即失效，过期或校验失败一律 **fail closed**（ADR-04）。框架 **Permission 系统是叠加层**，把闸门下钻到 Agent 循环内的每一次工具执行；`readonly_policy` / `nowrite_policy` / `gated_write_policy` 是**本仓库在 `EXPLORE` 档位与自定义 deny 规则之上组合出的三套规则集名，不是框架类名**。策略即配置；但**配置本身要被校验**——装配期断言不通过，进程起不来，不靠文档和 code review。

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    reg = app.state.toolkit_registry        # Toolkit 内的工具描述符注册表

    # 断言 1: 所有写工具必须声明 requires_confirmation + writes_via
    #         （否则 gated_write_policy 无从拦起，Permission 形同虚设）
    for d in reg.all():
        if not d.read_only:
            assert d.requires_confirmation, (
                f"写工具 {d.name} 必须 requires_confirmation=True，否则进程无法启动"
            )
            assert d.writes_via, (
                f"写工具 {d.name} 必须声明 writes_via（指向落库应用服务），否则进程无法启动"
            )

    # 断言 2: flows/ 编排的入口节点（纯路由/编排角色）不持有任何工具
    root_tools = reg.tools_for("orchestration_root", ANY_TENANT)
    assert len(root_tools) == 0, (
        f"编排根节点不应持有任何工具，当前注册了 {[t.name for t in root_tools]}"
    )

    # 断言 3: 任何 capability 下均无放行/拦截类工具
    FORBIDDEN_PREFIXES = ("pass_judge", "force_release", "release_", "block_", "intercept_")
    for cap in reg.capabilities():
        tools = reg.tools_for(cap, ANY_TENANT)
        for t in tools:
            for prefix in FORBIDDEN_PREFIXES:
                assert not t.name.startswith(prefix), (
                    f"capability={cap} 下禁止注册放行/拦截类工具: {t.name}"
                )

    # 断言 4: 各 capability 工具集互斥（一个工具不能同时属于两个 capability）
    reg.assert_capability_partition()

    # 断言 5: 所有 ACL 写 client 的方法名不含直接写动词（额外保护）
    # （由 NoWriteClientGate 扫描，同 草稿能力的机制；草稿能力的 nowrite_policy
    #   进一步禁止写工具注册，requires_confirmation 恒 True）

    # 装配 Permission 策略：诊断=readonly_policy / 草稿=nowrite_policy / 编排=gated_write_policy
    app.state.permissions = build_permission_policies(reg)

    yield
```

> 这五条断言与原方案等价，只是承载位置从"自研三层启动断言"移到 **Permission 策略装配 + Toolkit 注册表校验**：叠加层闸门在每一次工具执行时生效，装配期再把"策略拦不住的工具"挡在进程之外；**主闸仍在工具网关的一次性动作令牌**，内层失效不得导致外层放行。

---

## 13. 面试时可引用的实现细节清单

被问"这具体怎么实现"时，可以按以下顺序引用：

| 追问 | 回答要点 | 关键代码位置 |
|------|---------|------------|
| "状态怎么存的" | `AgentState` 显式类型化状态 + `SessionService`（Redis-backed），以 `session_id` 为键；MySQL 只留业务可观测表与可选归档，不再承担会话 checkpoint | §2 |
| "gate 怎么暂停的" | Permission 拦截写工具执行，`AgentState` 先落 Session store，会话进入待确认态（HITL confirmation），协程让出不销毁 | §3.1 |
| "怎么续跑的" | 人确认 → 签发确认记录 → 确认事件进事件总线 → 按 `session_id` 取回 AgentState，交回 gate 判定后继续 | §3.2 |
| "进程重启了怎么办" | 状态在 Redis-backed Session store 不在进程内存，任意副本按 `session_id` 恢复即可从断点续跑 | §3.4 |
| "token 怎么防伪造" | Redis 存储，token_id 随机 32 字符 hex，绑定 action:target，带 30 分钟 TTL，校验时查 Redis 验证存在性 + action 匹配 | §5 |
| "写怎么不走旁路" | ACL 写客户端调应用服务 REST，写路径过聚合根不变式 + 事务发件箱；confirmation_id 做幂等键 | §7 |
| "多个 session 怎么隔离" | `session_id` 不同 -> AgentState 记录不同 -> 状态隔离；能力节点用子会话 id `{session_id}:{capability}`；安全边界在工具执行层（Permission 拦每次执行） | §9 |
| "超时怎么计时" | asyncio.wait_for 包住整次编排驱动，从 _drive 开始计时，包含 gate 等待时间；gate 的 deadline 由 SchedulerManager 托管一次性延时任务 | §4.3 / §6.3 |
| "agent 失败怎么隔离" | FailureTracker 按 session+capability 计数，连续 2 次失败 -> status=SUSPENDED，不自动重试 | §11 |
| "红线怎么保证" | Permission 三条策略（readonly / nowrite / gated_write）+ 装配期 5 条断言，进程起不来 = 配置有问题，不靠文档或代码审查 | §12 |
| "走一遍具体场景" | 4 类典型长程任务（换线 / 故障复产 / 客诉 8D / 工艺变更）的逐步运行细节：每步性质（CODE/AGENT）、state 变化、状态快照序列、gate 挂起-续跑、ACL/应用服务落库、agent_step_record | §14 |

---

## 14. 使用场景全流程演练：4 类典型长程任务的逐步运行细节

> 前面 §1 用换线给了全景骨架，§2–§13 逐层拆了机制。本节把机制串起来，用 **4 个真实业务场景**演练"一次长程任务在程序里每一步到底发生了什么"——每个场景先给一个具体业务故事，再按时间线落到**步骤执行 / state 变化 / 状态快照写入 / gate 挂起-续跑 / ACL 调用 / 应用服务落库 / agent_step_record 记录**，被追问"走一遍具体场景"时可直接引用。
>
> 4 个场景对应 `OrchestrationState.scenario` 的 4 个枚举（见 §8、[实现方案 §1.4](FactoryAgent/FactoryAgent-流程编排能力.md)）：`CHANGEOVER` / `FAULT_RESPONSE` / `COMPLAINT_8D` / `PROCESS_CHANGE`。
>
> **口径**：仍是设计级骨架演练，时间戳为示意（T+0、T+2min 形式），非线上实绩；token 的 `action` 取 `card.writes_via_action()`，形如 `写动作:目标`（见 §5.3、架构图 §5 时序），表中为示意值；`AgentState` 快照序号简写为 `st-N`（写入 Redis-backed Session store，非 MySQL）；gate 确认后由 `card.writes_via` 触发写落库（见 §5.5、架构图 §5），不逐场景重复该机制，仅在表中标注"写落库"行。

### 14.1 场景① 换线（CHANGEOVER）：快路径 + mismatch 慢路径

换线是 编排能力的高频场景，也是"懂什么时候不用 AI"的典型——全程 PASS 时纯代码骨架跑完，LLM 调用为 0；只有钢网/程序 mismatch 时才触发 RootCauseAgent (A)。本节演练两条路径。

#### 14.1.1 业务背景

SMT 车间 1 线 L-01，白班 08:00 开始把工单 `WO-2026-0701` 从产品 A 换到产品 B：
- 目标工艺路线 `RR-B v4`（钢网 ST-B、贴片程序 PB-B-v4）
- 产线设备 `ASSET-01`（贴片机）
- 线长张工（user_id=u_zhang）在换线看板点"开始换线"

系统侧：换线看板调 `POST /agent/orchestration/changeover/start`，body 带 work_order_id / target_route_id / target_route_version / asset_id / tenant。`OrchestrationService.start()` 创建 session（id=`S-CO-20260712-001`，scenario=CHANGEOVER，status=PLANNING），由 `BackgroundTaskManager` 提交 `_drive`，HTTP 立即返回 session_id。

#### 14.1.2 快路径：全程 PASS（零 LLM 调用）

张工这次准备充分：首件已合格、工艺 v4 已就绪、钢网 ST-B 已扫码上机、物料齐套 100%。编排只走左侧快路径，**全程不触发 agent A，LLM 调用次数 = 0**（可由 `agent_step_total{node_type=CODE}` 与 `agent_llm_invocation_total=0` 验证，见实现方案 §8）。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（状态快照 / agent_step_record） |
|------|------|------|------|------|
| T+0 | `start` HTTP | OrchestrationService.start：session 写 MySQL（status=PLANNING），BackgroundTaskManager 托管 _drive 点火，返回 session_id | session.status=PLANNING | agent_session 行写入 |
| T+1s | `plan`（CODE） | 代码节点：按 scenario=CHANGEOVER 决定步骤序列 | current_step=PLAN | st-1(parent=null)；step=PLAN, node_type=CODE |
| T+2s | `first_article`（CODE） | query_first_article：调过点 ACL 查首件状态，返回 PASS | first_article_result={status:PASS, article_id:FA-001} | st-2(parent=st-1) |
| T+3s | `gate_first_article`（CODE） | _gate：build_action_card（intent="首件 FA-001 已合格，确认进入工艺激活"）-> save_step(GATE_WAITING) -> dispatcher.push（事件流推张工 + Kafka 持久）-> **Permission gate 拦截，会话挂起待人确认** | current_step=GATE_FIRST_ARTICLE | st-3(parent=st-2) 写入 Session store，本次驱动结束待人确认；step=FIRST_ARTICLE, status=GATE_WAITING |
| T+3s~T+5min | （等待人确认） | 张工看板点确认 -> `POST /confirm` {step:FIRST_ARTICLE, approved:true, user_id:u_zhang} -> ConfirmationStore.issue token（确认动作，无写落库）-> 确认事件按 `session_id=S-CO-...001` 触发续跑 | （gate 拿到确认记录） | SessionService 取回 st-3，续跑；step=FIRST_ARTICLE, status=CONFIRMED, gate_decision=PASS, gate_decided_by=u_zhang |
| T+5min | `process_switch`（CODE） | query_active_route：调工艺管理 ACL（强制 route_version=v4）查路线，返回 ACTIVE | process_switch_result={route_id:RR-B, version:v4, status:ACTIVE} | st-4(parent=st-3)；step=PROCESS_SWITCH, node_type=CODE |
| T+6min | `gate_process_switch`（CODE） | _gate：card intent="激活工艺路线 RR-B v4"，writes_via="工艺管理上下文.application.activate_route" -> push -> gate 挂起待人确认 | current_step=GATE_PROCESS_SWITCH | st-5(parent=st-4) 写入 Session store，挂起待人确认；step=PROCESS_SWITCH, status=GATE_WAITING |
| T+6min~T+9min | （等待人确认） | 工艺工程师李工确认激活 -> POST /confirm -> issue token（action=`process_route.activate:RR-B:v4`）-> 确认事件触发续跑 | （续跑） | 取回 st-5，续跑；step=PROCESS_SWITCH, status=CONFIRMED, gate_decision=PASS |
| T+9min | **写落库**（gate PASS 触发） | write_via_appservice 拿 token 调工艺管理上下文 ACL：POST /api/process-routes/RR-B/activate，header `X-Confirmation-Token`。应用服务过聚合根不变式 + 事务发件箱落 ProcessRouteActivated | gate_process_switch=PASS | st-6(parent=st-5)；step=PROCESS_SWITCH_WRITE, node_type=CODE |
| T+9min | `tooling_check` ‖ `kitting_check`（CODE，并行） | `asyncio.gather` 并行派发 ["tooling_check","kitting_check"]。tooling：expected=ST-B vs 扫码 actual=ST-B -> PASS；expected=PB-B-v4 vs 本地 PB-B-v4 -> PASS。kitting：齐套率=100% -> PASS | tooling_result={status:PASS}, kitting_result={status:PASS} | st-7(parent=st-6) 两分支汇合；step=TOOLING_CHECK/KITTING_CHECK, node_type=CODE |
| T+10min | `barrier`（CODE） | _barrier_node：t=PASS ∧ k=PASS -> barrier_route=draft_release（确定性分流，非 agent） | barrier_route=draft_release | st-8(parent=st-7)；step=BARRIER, node_type=CODE |
| T+10min | `draft_release`（CODE） | draft_release_card：结构化拼装放行卡（非 LLM），intent="WO-2026-0701 换线核对完成，放行生产" | action_card=放行卡 | st-9(parent=st-8) |
| T+10min | `gate_release`（CODE） | _gate：push -> gate 挂起待人确认 | current_step=GATE_RELEASE | st-10(parent=st-9) 写入 Session store，挂起待人确认；step=RELEASE, status=GATE_WAITING |
| T+10min~T+12min | （等待人确认） | 张工确认放行 -> POST /confirm -> issue token（action=`pass_execution.release:WO-2026-0701`）-> 确认事件触发续跑 | （续跑） | 取回 st-10，续跑；step=RELEASE, status=CONFIRMED, gate_decision=PASS |
| T+12min | **写落库**（放行） | write_via_appservice 调**过点上下文应用服务**放行（**过点主事务 + 规则引擎判定 P99≤200ms，agent 不进主事务**），发件箱落 PassReleased | gate_release=PASS | st-11(parent=st-10)；step=RELEASE_WRITE, node_type=CODE |
| T+12min | `done`（CODE） | session.status=DONE | status=DONE | st-12(parent=st-11)；step=DONE |

**快路径要点**：3 个 gate（FIRST_ARTICLE / PROCESS_SWITCH / RELEASE）全是代码节点，agent A 全程未触发，`agent_llm_invocation_total=0`。这正是实现方案 §0 的判断标准落地——三问皆否走代码节点。

#### 14.1.3 慢路径：钢网 mismatch 触发 RootCauseAgent (A)

同样的换线，但 T+9min 工艺激活后，tooling_check 扫码读到 `actual=ST-A`（不是 ST-B）。barrier 分流到 RootCauseAgent——这才是 agent 赚回成本的地方（痛点 A，见[痛点文档 §A](FactoryAgent/FactoryAgent-现场痛点与能力嵌入点.md)）。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（状态快照 / agent_step_record） |
|------|------|------|------|------|
| … | …（plan/first_article/process_switch 同快路径，略） | | gate_process_switch=PASS | st-6 |
| T+9min | `tooling_check` ‖ `kitting_check`（CODE，并行） | tooling：expected=ST-B vs actual=ST-A -> FAIL(code=TOOLING_STENCIL_MISMATCH)。kitting：齐套=100% -> PASS | tooling_result={status:FAIL, code:TOOLING_STENCIL_MISMATCH, expected:ST-B, actual:ST-A}, kitting_result={status:PASS} | st-7(parent=st-6)；step=TOOLING_CHECK, node_type=CODE, tooling_result 落库 |
| T+10min | `barrier`（CODE） | _barrier_node：t=FAIL -> barrier_route=root_cause，把 expected/actual/code 注入 state 给 agent | barrier_route=root_cause, expected=ST-B, actual=ST-A, mismatch_code=TOOLING_STENCIL_MISMATCH | st-8(parent=st-7)；step=BARRIER, node_type=CODE |
| T+10min | `root_cause`（**AGENT A**） | _run_agent("root_cause")：调用 A 的 Agent 执行（内置 ReAct），**子会话 id=`S-CO-...001:root_cause`**（见 §9.2，与主编排状态分开存放）。RootCauseAgent 自适应取证（LLM + 只读 toolset）：<br>① query_stencil_lending -> ST-A 借出未还<br>② query_last_changeover_close -> 上工单 WO-2026-0630 收线未触发归还<br>③ query_route_audit -> v4 录入 ST-B 无误<br>④ 输出根因假设"ST-A 未还库 + ST-B 未领"，confidence=high，草拟处置卡（suggested_actions: 归还 ST-A / 领用 ST-B，route_to: 库管+线长） | agent_hypothesis={root_cause:"ST-A 未还库 + ST-B 未领",...}, agent_confidence=high, action_card=处置卡 | 主编排 st-9(parent=st-8)；A 的推理状态落子会话 `...001:root_cause`；step=ROOT_CAUSE, **node_type=AGENT**, capability=A, tool_call_traces=[trace_id 列表] |
| T+11min | `gate_disposition`（CODE） | _gate：build_action_card（含 agent_hypothesis + evidence trace_id + confidence=high + risk_note）-> push -> gate 挂起待人确认。**确认人可点开 evidence 回溯 agent 推理，基于证据确认而非盲批**。同时启动 deadline 计时器（card.deadline，见 §6.3） | current_step=GATE_DISPOSITION | st-10(parent=st-9) 写入 Session store，挂起待人确认；step=DISPOSITION, status=GATE_WAITING, agent_hypothesis 落库 |
| T+11min~T+18min | （等待人确认） | 库管确认处置 -> POST /confirm {step:DISPOSITION, approved:true} -> issue token（action=`tooling.swap:ASSET-01`）-> 确认事件触发续跑。**期间 deadline 计时器在走，超时则 _suspend_session 推异常卡** | （续跑；取消 deadline 计时器） | 取回 st-10，续跑；step=DISPOSITION, status=CONFIRMED, gate_decision=PASS |
| T+18min | **写落库**（处置） | write_via_appservice 调钢网上下文应用服务：POST /api/tooling/returns（归还 ST-A）+ POST /api/tooling/issues（领用 ST-B），header 带 X-Confirmation-Token，**confirmation_id 做幂等键**（见 §7.3）。过聚合根不变式 + 发件箱 | gate_disposition=PASS, retry_tooling=true | st-11(parent=st-10)；step=DISPOSITION_WRITE, node_type=CODE |
| T+18min | `gate_disposition` 条件边 -> `tooling_check`（重检） | `flows/` 编排条件分流（应用层代码分支）：retry_tooling=true -> 回 tooling_check（**代码节点重检，agent 不参与"重检通过没"的判定**） | — | st-12(parent=st-11) |
| T+19min | `tooling_check`（CODE，重检） | 产线已换上 ST-B：expected=ST-B vs actual=ST-B -> PASS | tooling_result={status:PASS} | st-13(parent=st-12)；step=TOOLING_CHECK, node_type=CODE |
| T+19min~T+21min | `barrier` -> `draft_release` -> `gate_release` -> 放行写落库 -> `done` | 同快路径后半段 | status=DONE | st-14… |

**慢路径要点**：
- **agent A 只在 mismatch 分支触发**：barrier 的分流是确定性代码（PASS->放行 / FAIL->A / 缺料->挂起），agent 不参与"要不要放行"的判定。
- **能力节点用子会话 id**（§9.2）：`S-CO-...001:root_cause`，与主编排状态分开存放，互不覆盖。
- **重检回路**：A 草拟处置 -> 人确认 -> 处置落库 -> 回 tooling_check 重检（代码），agent 不参与重检判定——这是实现方案 §5.1 的硬边界。
- **取证路径由中间结果驱动**：A 查到"ST-A 借出未还"后自适应去查上工单收线记录，不是固定 JOIN——这是痛点 A 降复杂度的核心（见痛点文档 §A.3）。

---

### 14.2 场景② 设备故障复产（FAULT_RESPONSE）：嵌入 FaultImpactAgent (B)

#### 14.2.1 业务背景

SMT-1 线 L-01，14:30 贴片机 ASSET-01 报警停机（温控阀故障）。维修工李工到场维修。痛点不在维修本身（那是维修单的活），而在**ASSET-01 从什么时候开始参数漂移、漂移期间生产了哪些批次、这些批次要不要隔离**——这步现场几乎没人做好（痛点 B，见痛点文档 §B.1）：
- 若是硬停（故障前参数正常），只隔离报警时刻前后批次；
- 若是软漂移（参数早就偏了只是今天才报警），要往前回溯——漂移起始时间未知，夜班批次可能漏隔离。

设备故障事件（Kafka topic `equipment.fault` 或维修看板手动触发）调 `POST /agent/orchestration/fault_response/start`，body 带 asset_id / fault_time / tenant。session id=`S-FR-20260712-001`，scenario=FAULT_RESPONSE。

#### 14.2.2 逐步运行细节

编排结构见架构图 §4.1：`draft_repair_order ‖ FaultImpactAgent(B)` 并行 -> `gate:REPAIR ‖ gate:ISOLATION` 并行 -> 计量复校 gate -> 复产首件 gate -> done。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（状态快照 / agent_step_record） |
|------|------|------|------|------|
| T+0 | `start` HTTP | 设备故障事件触发，session 写 MySQL（status=PLANNING），后台任务托管点火 | session.status=PLANNING | agent_session 行 |
| T+1s | `plan`（CODE） | 按 scenario=FAULT_RESPONSE 决定步骤序列 | current_step=PLAN | st-1 |
| T+2s | `draft_repair_order` ‖ `fault_impact`（并行派发） | `asyncio.gather` 并发派发两个目标（分支各自独立执行，应用层显式合并）：<br>左 `draft_repair_order`（CODE）：结构化拼装维修单（设备 ASSET-01、故障现象、报修人李工）<br>右 `fault_impact`（**AGENT B**）：能力节点子会话 id=`...001:fault_impact`，LLM + 只读 toolset 自适应取证 | （并行执行） | st-2(parent=st-1) |
| T+2s~T+30s | `fault_impact`（**AGENT B**，子步骤） | B 推理故障模式 × 漂移窗口 × 产品敏感度：<br>① query_equipment_telemetry -> 温控阀温度曲线长期偏移，**推理故障模式=软漂移**，估漂移起始窗口=[昨晚 22:00, 14:30]（时序形态判断，规则引擎做不了）<br>② query_batches_in_window -> 窗口内 ASSET-01 生产批次=[B-501(夜班), B-502(夜班), B-503(白班)]<br>③ query_process_fmea + query_product_sensitivity -> 漂移参数=贴装压力，敏感产品=0201 细间距（B-501、B-502），0603 粗间距（B-503）不受影响<br>④ draft_isolation_card -> 隔离集={B-501,B-502}，放行 B-503，含每批次隔离理由 + 证据 trace_id | agent_hypothesis={fault_mode:"软漂移", drift_window:["22:00","14:30"], isolation_set:["B-501","B-502"], release:["B-503"], sensitivity_reason:...}, agent_confidence=high, action_card=隔离卡 | 主编排 st-3；能力节点状态落子会话；step=FAULT_IMPACT, **node_type=AGENT**, capability=B, tool_call_traces 落库 |
| T+30s | `gate_repair` ‖ `gate_isolation`（CODE，并行） | 两 gate 并行拦截：gate_repair 推维修单卡给李工；gate_isolation 推隔离卡给质量工程师王工（含 agent_hypothesis + evidence + confidence）-> gate 挂起待人确认。各启 deadline 计时器 | current_step=GATE_REPAIR / GATE_ISOLATION | st-4(parent=st-3) 落 Redis-backed Session store（步骤台账 `agent_step_record` 写 MySQL），两 gate 各自独立待确认；step=REPAIR/ISOLATION, status=GATE_WAITING |
| T+30s~T+20min | （并行等待人确认） | 李工确认维修单（边修边确认）；王工点开隔离卡 evidence 验证 B 的推理（漂移窗口 + 敏感度理由），确认隔离集。各 POST /confirm -> 各 issue token（维修单 action=`repair_order.issue:ASSET-01`；隔离 action=`isolation.issue:WS-A`）-> 各自由确认事件触发续跑 | （两 gate 各自续跑） | 取回 st-4，分别续跑；step=REPAIR/ISOLATION, status=CONFIRMED, gate_decision=PASS |
| T+20min | **写落库**（隔离） | write_via_appservice 调返工上下文 ACL（ReworkWriteAclClient，见 §7.2）：校验 token -> POST /api/isolation-orders {batches:[B-501,B-502], reason, confirmation_id}，header X-Confirmation-Token。返工上下文 IsolationAggregate.issue 过不变式 + 事务发件箱落 BatchIsolated。**confirmation_id 做幂等键** | gate_isolation=PASS | st-5(parent=st-4)；step=ISOLATION_WRITE, node_type=CODE |
| T+20min | **写落库**（维修单） | 调设备/维修上下文应用服务落维修单 | gate_repair=PASS | （同状态快照序列）；step=REPAIR_WRITE, node_type=CODE |
| T+20min~T+45min | （维修进行 + 计量复校） | 李工完成维修，设备计量复校，复校结果回填 | — | — |
| T+45min | `gate_recalibration`（CODE） | 计量复校 gate：推复校确认卡 -> gate 挂起待人确认（**确定性 gate，不嵌 agent，agent 不碰复校红线**）。issue token（action=`calibration.confirm:ASSET-01`，确认动作） | gate_recalibration=PASS | st-6；step=RECALIBRATION, status=CONFIRMED |
| T+46min | `gate_restart_first_article`（CODE） | barrier 等复校+点检 PASS -> 推复产首件放行卡 -> gate 挂起待人确认 -> 过点上下文放行复产（过点主事务 + 规则引擎） | gate_restart_first_article=PASS | st-7；step=RESTART_FA, status=CONFIRMED |
| T+47min | `done`（CODE） | session.status=DONE | status=DONE | st-8；step=DONE |

**场景②要点**：
- **B 的隔离范围判定是三维动态**（故障模式 × 漂移窗口 × 产品敏感度），规则引擎会随产品族/设备类型爆炸，agent 用"遥测形态推理 + FMEA 关联"降复杂度（痛点 B）。
- **隔离集命中夜班批次**：B 推理出软漂移窗口覆盖昨晚 22:00，隔离了夜班 B-501/B-502——这是人凭记忆"10 点报警才坏"会漏掉的（痛点 B.1 步骤 2 的真实风险）。
- **复校 / 复产首件两道红线是代码 gate**，agent 不碰（实现方案 §5.1 硬边界）。
- **隔离下达仍是代码**：B 只草拟隔离集，人确认后走返工上下文应用服务，过聚合根不变式 + 发件箱，agent 不直写。

---

### 14.3 场景③ 客诉 8D（COMPLAINT_8D）：嵌入 TraceabilityAgent (C) + DraftAgents (D)，含跨进程恢复

#### 14.3.1 业务背景

客户反馈批次 `P-2026-0605` 焊接不良退货。质量工程师王工触发客诉追溯。痛点在**跨 5 上下文手工串证据 + 5M1E 假设靠人脑排 + 版本串错**（痛点 C，见痛点文档 §C.1）——最隐蔽的坑是：工艺界面默认显示当前 v5，工程师若用 v5 分析批次 P（其实 P 过点时用的是 v3），根因分析建立在错误工艺基础上。

触发：`POST /agent/orchestration/complaint_8d/start`，body 带 batch_id=P-2026-0605 / tenant。session id=`S-CP-20260712-001`，scenario=COMPLAINT_8D。本场景同时演示**跨进程恢复**（§3.4）——在 gate 等人确认时 agent-service pod 被 OOM Kill，新 pod 接管续跑。

#### 14.3.2 逐步运行细节

编排结构见架构图 §4.2：`TraceabilityAgent(C)` -> `供应商批次追溯 ‖ 隔离范围判定` 并行 -> `gate:ISOLATION` -> `DraftAgents.draft_8d(D)` -> `gate:8D_PUBLISH` -> done。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（状态快照 / agent_step_record） |
|------|------|------|------|------|
| T+0 | `start` HTTP | session 写 MySQL（status=PLANNING），**点火在 pod-1**，BackgroundTaskManager 托管 _drive | session.status=PLANNING | agent_session 行 |
| T+1s | `plan`（CODE） | 按 scenario=COMPLAINT_8D 决定步骤序列 | current_step=PLAN | st-1 |
| T+2s | `traceability`（**AGENT C**，嵌入 诊断能力） | _run_agent("traceability")：能力节点子会话 id=`...001:traceability`，C 复用诊断能力（跨能力委托=**显式服务调用**，传 `session_id` + `evidence_set_id` + `traceparent`；AgentScope 2.0 **无 Handoffs/Routing 移交原语**，共享的是证据集与 trace 上下文，不是消息流）：<br>① **版本钉死（ACL 代码做，非 agent）**：从过点记录强取批次 P 用的 route_version=v3（不是当前 v5），后续工艺查询强制 v3——agent 只消费钉死后的版本<br>② 汇聚跨上下文证据：过点 TestResult、设备遥测、锡膏批次 B-77、同批次不良率<br>③ **5M1E 假设排序（LLM 加权推理，规则引擎做不了）**：料因（B-77 在别处也有不良）置信度高、机因（设备参数漂移幅度小）置信度中<br>④ 输出排序假设 + 证据链 + 置信度 | agent_hypothesis={hypotheses:[{cause:"料-锡膏B-77",confidence:high},{cause:"机-设备漂移",confidence:medium}], evidence_chain:[...]}, agent_confidence=high | 主编排 st-2；能力节点状态落子会话；step=TRACEABILITY, **node_type=AGENT**, capability=C, tool_call_traces 落库 |
| T+20s | `supplier_trace` ‖ `isolation_scope`（并行） | 左 `supplier_trace`（CODE，版本钉死后查）：查 B-77 供应商批次、在库品。右 `isolation_scope`（代码或复用 B 逻辑）：判同批次在库品隔离范围 | （并行） | st-3(parent=st-2)；step=SUPPLIER_TRACE/ISOLATION_SCOPE, node_type=CODE |
| T+25s | `gate_isolation`（CODE） | _gate：build_action_card（隔离集 + 每批次理由 + agent_hypothesis + evidence）-> push 给王工 -> **Permission gate 拦截，会话挂起待人确认**。AgentState 落 Session store st-4，本次驱动结束等待确认事件 | current_step=GATE_ISOLATION | **st-4(parent=st-3) 写入 Session store**，pod-1 的本次驱动结束、协程让出；step=ISOLATION, status=GATE_WAITING |
| T+25s~T+10min | ⚠️ **pod-1 被 OOM Kill**（跨进程恢复演示） | 14:35 pod-1 因内存压力被 K8s OOM Kill，_drive 协程销毁。**但 AgentState 已在 Redis-backed Session store（st-4），不在进程内存**——session 在 DB 仍 RUNNING，gate 步骤仍 GATE_WAITING。期间王工在另一终端确认隔离 | （pod-1 进程已死） | Session store 的 st-4 仍在 |
| T+10min | 王工确认 -> POST /confirm（**打到 pod-2**） | 王工点开隔离卡 evidence（trace_id 回溯 C 的 5M1E 推理），确认隔离 -> POST /confirm {step:ISOLATION, approved:true} -> **请求被负载均衡到 pod-2** -> ConfirmationStore.issue token（action=`isolation.issue:WS-A`）-> 确认事件按 `session_id=S-CP-...001` 触发续跑（pod-2 接管） | （pod-2 接管） | **SessionService 从 Session store 取回 st-4**，反序列化 state（GATE_ISOLATION 处），确认记录交回 gate 判定，继续执行；step=ISOLATION, status=CONFIRMED, gate_decision=PASS, gate_decided_by=u_wang |
| T+10min | **写落库**（隔离） | write_via_appservice 调返工上下文 ACL：POST /api/isolation-orders，header X-Confirmation-Token，confirmation_id 幂等。过聚合根不变式 + 发件箱 | gate_isolation=PASS | **st-5(parent=st-4)**（pod-2 写入，链续上）；step=ISOLATION_WRITE, node_type=CODE |
| T+11min | `draft_8d`（**AGENT D**） | _run_agent("draft_8d")：能力节点子会话 id=`...001:draft_8d`，D 拉追溯链（C 已汇聚）+ 历史 8D 案例库（query_history_8d）-> **草拟 8D 报告**（根因/纠正/预防/案例对照，开放生成，代码写不出） | agent_hypothesis={draft_8d:{...}}, action_card=8D 发布卡 | 主编排 st-6(parent=st-5)；能力节点状态落子会话；step=DRAFT_8D, **node_type=AGENT**, capability=D |
| T+12min | `gate_8d_publish`（CODE） | _gate：推 8D 发布卡给王工 -> gate 挂起待人确认 | current_step=GATE_8D_PUBLISH | st-7(parent=st-6) 写入 Session store，挂起待人确认；step=8D_PUBLISH, status=GATE_WAITING |
| T+12min~T+30min | （等待人确认） | 王工审改 8D 草稿，确认发布 -> POST /confirm -> issue token（action=`report.publish_8d:P-2026-0605`）-> 确认事件触发续跑 | （续跑） | 取回 st-7，续跑；step=8D_PUBLISH, status=CONFIRMED, gate_decision=PASS |
| T+30min | **写落库**（8D 发布） | write_via_appservice 调文档/质量上下文应用服务发布 8D，过不变式 + 发件箱 | gate_8d_publish=PASS | st-8(parent=st-7)；step=8D_PUBLISH_WRITE, node_type=CODE |
| T+30min | `done`（CODE） | session.status=DONE（done 节点标 DONE，不依赖已死的 pod-1 _drive） | status=DONE | st-9；step=DONE |

**场景③要点**：
- **跨进程恢复真实演练**（§3.4，设计目标演练）：pod-1 OOM Kill 后，state 不丢（在 **Redis-backed Session store**，不在进程内存），pod-2 按同一 session_id 取回 st-4 续跑，快照序列 st-4->st-5 跨 pod 续上——这是 `SessionService`（Redis-backed）+ `session_id` 恢复键（§2.3）的价值。done 节点是代码节点，自行标 DONE，不依赖原 _drive 协程。
- **版本钉死是 ACL 代码做**（非 agent）：C 只消费钉死后的 v3，不会用当前 v5 套历史批次 P——这层是代码红线，agent 不碰版本校验（痛点 C.4）。
- **5M1E 假设排序是 LLM 加权推理**：同样证据在不同产品族/历史背景下排序不同，规则引擎做不了（痛点 C.2）。
- **8D 草拟是 D 的开放生成**：代码写不出自然语言 8D，agent 草拟 + 人审改兜底幻觉（痛点 D）。

---

### 14.4 场景④ 工艺变更落地（PROCESS_CHANGE）：嵌入 DraftAgents.draft_sop (D)

#### 14.4.1 业务背景

工艺路线 RR-B 从 v4 升 v5，回流焊温区 3 从 240℃ 调到 245℃（为改善 0201 元件立碑）。工艺变更发布后，订阅 `ProcessRouteActivated` 事件触发 编排能力：要草拟新 SOP + 核对操作工资质 + 新工艺首件验证（痛点 D，见痛点文档 §D.1）。

触发：Kafka 监听器收 `ProcessRouteActivated{route_id:RR-B, version:v5}` -> 调 `POST /agent/orchestration/process_change/start`。session id=`S-PC-20260712-001`，scenario=PROCESS_CHANGE。

#### 14.4.2 逐步运行细节

编排结构见架构图 §4.3：`draft_sop(D) ‖ 资质核对(CODE)` 并行 -> `barrier` -> `gate:SOP_PUBLISH` -> 新工艺首件验证 gate -> done。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（状态快照 / agent_step_record） |
|------|------|------|------|------|
| T+0 | 事件触发 `start` | Kafka 监听器收 ProcessRouteActivated -> POST /agent/orchestration/process_change/start。session 写 MySQL，后台任务托管点火 | session.status=PLANNING | agent_session 行 |
| T+1s | `plan`（CODE） | 按 scenario=PROCESS_CHANGE 决定步骤序列 | current_step=PLAN | st-1 |
| T+2s | `draft_sop` ‖ `qualification_check`（并行派发） | `asyncio.gather` 并发派发两个目标（分支各自独立执行，应用层显式合并）：<br>左 `draft_sop`（**AGENT D**）：能力节点子会话 id=`...001:draft_sop`，D 查 query_route_diff（v4->v5：温区3 +5℃）+ query_prior_sop（v4 旧 SOP）+ query_fmea（0201 立碑风险）-> **草拟新 SOP**（含差异点提示："温区3 245℃，关注 0201 立碑，首件重点检"）<br>右 `qualification_check`（**CODE，确定性，不嵌 agent**）：query 操作工资质 ∈ 工艺要求资质集？张工=回流焊资质√，李工=无资质✗（需培训） | 左 agent_hypothesis={draft_sop:{...}}, action_card=SOP 发布卡；右 qualification_result={qualified:[张工], unqualified:[李工]} | 主编排 st-2；D 的 能力节点状态落子会话；step=DRAFT_SOP, **node_type=AGENT**, capability=D；step=QUALIFICATION, node_type=CODE |
| T+15s | `barrier`（CODE） | 等双分支汇合：SOP 草拟完成 ∧ 资质核对完成 -> barrier_route=draft_release（资质不全则分流挂起推培训） | barrier_route=draft_release | st-3(parent=st-2)；step=BARRIER, node_type=CODE |
| T+15s | `gate_sop_publish`（CODE） | _gate：推 SOP 发布卡给工艺工程师（含 D 草拟的 SOP + 差异点提示 + evidence）-> gate 挂起待人确认 | current_step=GATE_SOP_PUBLISH | st-4(parent=st-3) 写入 Session store，挂起待人确认；step=SOP_PUBLISH, status=GATE_WAITING |
| T+15s~T+25min | （等待人确认） | 工艺工程师审改 SOP 草稿，确认发布 -> POST /confirm -> issue token（action=`sop.publish:RR-B:v5`）-> 确认事件触发续跑 | （续跑） | 取回 st-4，续跑；step=SOP_PUBLISH, status=CONFIRMED, gate_decision=PASS |
| T+25min | **写落库**（SOP 发布） | write_via_appservice 调工艺/SOP 上下文应用服务发布 SOP v5，过不变式 + 发件箱落 SOPPublished | gate_sop_publish=PASS | st-5(parent=st-4)；step=SOP_PUBLISH_WRITE, node_type=CODE |
| T+25min | `gate_new_route_first_article`（CODE） | 新工艺首件验证 gate：推首件放行卡 -> gate 挂起待人确认 -> 过点上下文放行首件（过点主事务 + 规则引擎）。issue token（action=`pass_execution.release:RR-B:v5`） | gate_new_route_first_article=PASS | st-6(parent=st-5)；step=NEW_ROUTE_FA, status=CONFIRMED |
| T+26min | `done`（CODE） | session.status=DONE | status=DONE | st-7；step=DONE |

**场景④要点**：
- **SOP 草拟嵌 D（开放生成），资质核对是代码**——这是"该用代码的没用 AI"的典型体现（架构图 §4.3）：资质核对是"操作工资质 ∈ 工艺要求资质集"的确定性查询，代码做，不嵌 agent；只有 SOP 草拟（开放生成，代码写不出）嵌 D。
- **生成不碰红线**：D 只草拟 SOP，不触放过点放行、不碰隔离下达，生成能力严格限制在"草拟"（痛点 D.4）。
- **人审改兜底幻觉**：SOP 草稿必须人确认才发布，LLM 幻觉成本留在审改环节，不出厂。

---

### 14.5 4 场景对照表

| 场景 | scenario | 业务触发 | 嵌入 agent | gate 数 | 突出体现的机制 | 全程 PASS 时 LLM 调用 |
|------|----------|---------|-----------|--------|--------------|-------------------|
| ① 换线 | CHANGEOVER | 线长点"开始换线" | A（仅 mismatch 分支） | 3~4（首件/工艺/放行/可选处置） | 代码节点 vs agent 节点边界、barrier 确定性分流、重检回路、子会话隔离 | **0**（快路径纯代码） |
| ② 设备故障复产 | FAULT_RESPONSE | 设备故障事件 | B（隔离范围判定） | 4（维修/隔离/复校/复产首件） | 并行 gate、B 的三维动态判定、复校/复产红线是代码 | 必调（B 是核心） |
| ③ 客诉 8D | COMPLAINT_8D | 客诉触发 | C（5M1E 排序）+ D（8D 草拟） | 2（隔离/8D 发布） | 跨进程恢复、版本钉死（代码）+ 5M1E 排序（agent）、开放生成 | 必调（C+D） |
| ④ 工艺变更落地 | PROCESS_CHANGE | ProcessRouteActivated 事件 | D（SOP 草拟） | 2（SOP 发布/新工艺首件） | SOP 草拟（agent）‖ 资质核对（代码）的边界、生成不碰红线 | 必调（D） |

**一句话**：4 个场景共享同一套机制（SessionService + AgentState 持久化 / gate 挂起-事件续跑 / Permission 确认记录 / ActionCardDispatcher / barrier / ACL 写客户端），区别只在**编排装配**（代码节点 + agent 能力的组合）与**触发源**（人 / 设备事件 / 客诉 / 工艺事件）。换线快路径全程零 LLM 是"懂什么时候不用 AI"；场景②③④的 agent 调用分别赚回 B/C/D 三类代码做不了的非确定段——但写闸门（confirmation gate + 应用服务 + 发件箱）在所有场景里始终一致，agent 只草拟、人确认、应用服务落库。