# Agent 长程任务 · 实现细节深度剖析

> **定位**：本文从零开始，逐层拆解 L3 编排型 Agent 长程任务的**全部实现细节**——从 LangGraph 的 SqlSaver 存什么、interrupt 怎么暂停、Command(resume) 怎么恢复，到 confirmation token 的 Redis 存储结构、gate deadline 怎么计时、进程重启后怎么从断点续跑、多个 session 并发怎么隔离。每一层都落到代码级细节。
>
> **阅读前提**：已读过 [L3编排型Agent-实现方案.md](../AGENT服务/L3编排型Agent/L3编排型Agent-实现方案.md) 和 [L3编排型Agent-LangGraph架构与流程图.md](../AGENT服务/L3编排型Agent/L3编排型Agent-LangGraph架构与流程图.md)，本文是它们的**实现细节补全**——面试时被追问"怎么实现的"时，本文提供可直接引用的技术细节。
>
> **口径纪律**：这是设计规划阶段的实现方案，所有代码为设计级骨架（非生产可运行），但足够回答"怎么实现"级别的追问。

---

## 1. 长程任务的全生命周期（先看全景）

一次换线长程任务从启动到结束，经历以下阶段：

```
HTTP POST /agent/l3/changeover/start
  → L3Orchestrator.start()
    → l3_session 写入 MySQL (status=PLANNING)
    → asyncio.create_task(_drive(session))    ← 异步点火，HTTP 立即返回 session_id
    → 返回 session_id 给调用方

后台 asyncio Task:
  _drive(session)
    → supervisor.ainvoke(initial_state)       ← LangGraph 图开始执行
      → plan_node (代码)                      ← 每一步都推进 state
      → first_article (代码)
      → gate_first_article                    ← interrupt! state 落 MySQL，Task 暂停
        ... 等人确认 ...
        POST /agent/l3/{session_id}/confirm   ← 人确认
          → Command(resume=token)             ← 唤醒 Task
      → process_switch (代码)
      → gate_process_switch                   ← interrupt again...
        ... 等人确认 ...
      → tooling_check ‖ kitting_check (并行)  ← 两条分支并发
      → barrier_node (代码)
      → ... 继续直到 done_node
    → supervisor.ainvoke 返回
  → session.status = DONE
```

**关键认知**：整个长程任务的生命周期由三个组件协作完成：
1. **asyncio Task**：承载图执行的协程，`interrupt` 时让出 CPU 但不销毁
2. **MySQL (SqlSaver)**：持久化 state，让 Task 可以跨进程存活
3. **Redis (ConfirmationStore)**：管理 confirmation token 的签发与校验

---

## 2. 状态持久化：SqlSaver 详解

### 2.1 LangGraph 的 checkpoint 机制

LangGraph 每执行一个节点（node），就会自动创建一个 checkpoint。checkpoint 包含：
- 当前 state（所有 channel 的值）
- 当前节点 ID
- 父 checkpoint ID（形成链）
- 元数据（step、source、write 操作）

`SqlSaver` 是 LangGraph 提供的 MySQL checkpointer 实现，源码在 `langgraph.checkpoint.mysql`。

### 2.2 SqlSaver 的 MySQL 表结构

LangGraph 的 `SqlSaver` 在 MySQL 中创建三张表：

```sql
-- 表1: checkpoints —— 每个 checkpoint 一行
CREATE TABLE checkpoints (
    thread_id       VARCHAR(255) NOT NULL,   -- 我们映射为 session_id
    checkpoint_ns   VARCHAR(255) NOT NULL DEFAULT '',  -- namespace（多图隔离）
    checkpoint_id   VARCHAR(255) NOT NULL,   -- checkpoint UUID
    parent_checkpoint_id VARCHAR(255),       -- 父 checkpoint（形成链）
    type            VARCHAR(255),            -- 类型标记
    checkpoint      LONGBLOB NOT NULL,       -- 序列化后的完整 state（JSON/MsgPack）
    metadata        LONGBLOB,                -- 元数据（step、source 等）
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

-- 表2: checkpoint_writes —— 每个 checkpoint 内的写操作
CREATE TABLE checkpoint_writes (
    thread_id       VARCHAR(255) NOT NULL,
    checkpoint_ns   VARCHAR(255) NOT NULL DEFAULT '',
    checkpoint_id   VARCHAR(255) NOT NULL,
    task_id         VARCHAR(255) NOT NULL,   -- 节点 ID
    idx             INT NOT NULL,            -- 写操作序号
    channel         VARCHAR(255) NOT NULL,   -- 写到哪个 channel
    type            VARCHAR(255),
    value           LONGBLOB,                -- 写入的值
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- 表3: checkpoint_blobs —— 大对象存储（可选，按 channel 分片）
CREATE TABLE checkpoint_blobs (
    thread_id       VARCHAR(255) NOT NULL,
    checkpoint_ns   VARCHAR(255) NOT NULL,
    channel         VARCHAR(255) NOT NULL,
    version          VARCHAR(255) NOT NULL,
    type             VARCHAR(255),
    blob             LONGBLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
```

### 2.3 thread_id = session_id 的映射

```python
# app/application/l3_orchestrator.py
config = {
    "recursion_limit": 40,
    "configurable": {
        "thread_id": session.id,   # ← session_id 直接作为 thread_id
    }
}
```

**为什么这样映射**：
- `thread_id` 是 LangGraph 的会话隔离键——不同 thread_id 的 state 完全隔离。
- 一个 L3 会话 = 一个 LangGraph thread，天然一一对应。
- 恢复时只需知道 `session_id`，就能用同一个 `thread_id` 找回 state。

### 2.4 checkpoint 里到底存了什么

以换线场景为例，state 在 gate 处 interrupt 时，checkpoint 的 `checkpoint` BLOB 展开后大致是：

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
- state 是完整快照，包含所有已执行步骤的中间结果。
- `interrupt(value=card)` 时，`value`（动作卡）**不进入 checkpoint**——它只是传给外部的值。checkpoint 存的是 state 本身。
- 恢复时，LangGraph 从最新 checkpoint 重建 state，然后从 `interrupt` 的下一行继续执行。

### 2.5 checkpoint 链

```
checkpoint_1 (parent=null)  ← plan_node 执行完
  ↑
checkpoint_2 (parent=cp_1)  ← first_article 执行完
  ↑
checkpoint_3 (parent=cp_2)  ← gate_first_article interrupt 处
  ↑
checkpoint_4 (parent=cp_3)  ← process_switch 执行完（resume 后）
  ↑
...
```

- 链式结构让 LangGraph 可以**回退**到任意历史 checkpoint（类似 git 的 commit 链）。
- L3 不用回退（gate 确认后不撤销），但链式结构是 LangGraph 的内建能力。

---

## 3. interrupt / resume 的底层机制

### 3.1 interrupt 到底做了什么

```python
# GateManager.await_confirmation
async def await_confirmation(self, session_id: str, step: str,
                             card: ActionCard) -> str:
    await self._dispatcher.push(card)        # ① 推送动作卡
    confirmation = await interrupt(value=card)  # ② 暂停
    # ⬆ 代码执行到这里暂停，直到有人调 Command(resume=...)
    # ⬇ 人确认后，从下面继续执行
    if not confirmation.valid_for(card.writes_via_action()):
        return "REJECT"
    return "PASS" if confirmation.approved else "REJECT"
```

`interrupt(value=card)` 的内部行为：

1. **保存当前 checkpoint**：LangGraph 把当前 state 序列化，写入 MySQL `checkpoints` 表。
2. **抛出 `GraphInterrupt` 异常**：这不是错误异常，是 LangGraph 的控制流信号。异常被 LangGraph 的 runtime 捕获。
3. **返回 `value` 给调用方**：`interrupt` 的返回值会通过 `stream()` 或 `astream_events()` 抛出给外部，但对于 `ainvoke()` 模式，LangGraph 把这个 `value` 存在内部，等待 `Command(resume=...)` 来唤醒。
4. **协程挂起**：`interrupt` 返回一个特殊的 awaitable，它不会 resolve，直到 `Command(resume=...)` 被投入。

### 3.2 Command(resume=...) 的恢复过程

```python
# POST /agent/l3/{session_id}/confirm
@router.post("/agent/l3/{session_id}/confirm")
async def confirm_gate(session_id: str, req: ConfirmRequest, ...):
    token = store.issue(session_id, req.step, req.approved, req.user_id)
    await graph.ainvoke(
        Command(resume=token),                       # ← 关键
        config={"configurable": {"thread_id": session_id}},
    )
```

`Command(resume=token)` 的内部行为：

1. **从 MySQL 加载最新 checkpoint**：`SqlSaver` 按 `(thread_id, checkpoint_ns)` 找到最新的 checkpoint，反序列化 state。
2. **找到 interrupt 点**：LangGraph 定位到 `interrupt` 被调用的那个节点。
3. **让 `interrupt` 返回 `token`**：此时 `interrupt` 的 awaitable resolve，返回值就是 `token`（即 `Command(resume=...)` 的参数）。
4. **继续执行**：从 `interrupt` 的下一行开始执行（`if not confirmation.valid_for(...)`）。

### 3.3 为什么 interrupt 不阻塞进程

`interrupt` 抛出的是 `GraphInterrupt`，不是 `asyncio.CancelledError`。LangGraph 的图执行引擎捕获这个异常后：

- **ainvoke 模式**：`ainvoke` 返回一个包含 `__interrupt__` 标记的特殊结果，协程本身不阻塞——但 `_drive` 里 `await supervisor.ainvoke(...)` 会等待图完成。这里的关键是：`ainvoke` 在 `interrupt` 时**不会**让 `await` 完成——它内部进入等待状态，等待 `Command(resume=...)` 重新投入。

实际上，LangGraph 的 `ainvoke` 在遇到 `interrupt` 时的行为取决于版本：
- 较新版本（LangGraph >= 0.2.x）：`ainvoke` 会持续等待，直到 `Command(resume=...)` 通过 `graph.ainvoke(Command(resume=...), config)` 重新投入。两次 `ainvoke` 调用共享同一个 `thread_id`，第二次调用不会重新创建 state，而是从 checkpoint 恢复并继续。

**更准确的理解**：
```python
# 第一次调用：启动图
await graph.ainvoke(initial_state, config={"configurable": {"thread_id": session_id}})
# → 图跑到 interrupt，state 落 MySQL，ainvoke 阻塞等待

# 第二次调用（可以在不同进程）：
await graph.ainvoke(Command(resume=token), config={"configurable": {"thread_id": session_id}})
# → 从 MySQL 加载 state，让 interrupt 返回 token，继续执行
# → 如果又遇到 interrupt，再次阻塞等待
```

### 3.4 跨进程恢复的完整时序

```
进程 A (agent-service pod-1):
  asyncio.create_task(_drive(session))
    → graph.ainvoke(initial_state, thread_id=S-001)
      → plan_node ✓
      → first_article ✓
      → gate_first_article → interrupt! → state 落 MySQL checkpoint
      → ainvoke 阻塞等待...

  ──── pod-1 被 OOM Kill 或滚动更新重启 ────

进程 B (agent-service pod-2):
  # 人确认了，POST /confirm 到达
  → graph.ainvoke(Command(resume=token), thread_id=S-001)
    → SqlSaver 从 MySQL 加载 thread_id=S-001 的最新 checkpoint
    → 反序列化 state（gate_first_article 处）
    → interrupt 返回 token
    → 继续执行: process_switch → gate_process_switch → ...
```

**关键**：进程 A 和进程 B 可以是完全不同的进程，甚至不同的 Pod——只要 `thread_id` 相同、MySQL 可达，就能从断点续跑。

---

## 4. 异步驱动：asyncio.create_task 的细节

### 4.1 为什么不用 BackgroundTasks 或 Celery

```python
# L3Orchestrator.start()
async def start(self, req: L3Request, tenant: TenantContext) -> L3Session:
    session = await self._sessions.create(req, tenant)
    asyncio.create_task(self._drive(session, tenant))  # ← 直接创建 Task
    return session  # 立即返回
```

| 方案 | 特点 | 为什么不选 |
|------|------|-----------|
| FastAPI `BackgroundTasks` | 请求结束后执行，与请求生命周期绑定 | 无法跨请求恢复——gate 等人时 background task 虽然活着，但没法从另一个 HTTP 请求唤醒 |
| Celery / 消息队列 | 独立 worker 进程，任务持久化到 broker | 太重，且 L3 的 `interrupt` 是 LangGraph 原语，需要同进程内的协程语义 |
| `asyncio.create_task` | 同事件循环的协程 Task | 轻量，且 `interrupt`/`resume` 在同事件循环内自然衔接 |

**选择 `asyncio.create_task` 的原因**：L3 的"暂停恢复"是通过 LangGraph 的 `interrupt`/`Command(resume=...)` 实现的，不是通过消息队列的"暂停消费/恢复消费"。`asyncio.create_task` 创建的 Task 在 gate 处 `interrupt` 时，协程挂起但不销毁——它等待同一个 `thread_id` 的下一次 `ainvoke(Command(resume=...))` 来唤醒。

### 4.2 Task 的生命周期管理

```python
class L3Orchestrator:
    def __init__(self, ...):
        self._active_tasks: dict[str, asyncio.Task] = {}  # session_id → Task

    async def start(self, req, tenant):
        session = await self._sessions.create(req, tenant)
        task = asyncio.create_task(self._drive(session, tenant))
        self._active_tasks[session.id] = task
        task.add_done_callback(lambda t: self._on_task_done(session.id, t))
        return session

    def _on_task_done(self, session_id: str, task: asyncio.Task):
        self._active_tasks.pop(session_id, None)
        if task.exception():
            # 记录异常，session 状态由 _drive 内部的 try/except 管理
            pass
```

### 4.3 超时实现

```python
async def _drive(self, session, tenant):
    try:
        await asyncio.wait_for(
            self._supervisor.ainvoke(
                {"session_id": session.id, ...},
                config={"recursion_limit": 40, "configurable": {"thread_id": session.id}},
            ),
            timeout=3600.0,   # 1 小时
        )
    except asyncio.TimeoutError:
        await self._sessions.mark_failed(session, "整体超时")
    except GraphRecursionError:
        await self._sessions.mark_failed(session, "步数超限")
```

**`asyncio.wait_for` 的计时细节**：
- `timeout=3600.0` 是从 `_drive` 开始计时，**包含** gate 等人的时间。
- 当 `interrupt` 挂起时，`asyncio.wait_for` 的计时器**继续走**——因为 `interrupt` 是协程内的 await，不是释放 GIL 的 sleep。
- 如果人在 gate 处等了 1 小时零 1 秒才确认，`asyncio.wait_for` 会在 1 小时整时抛出 `TimeoutError`，`Command(resume=...)` 的 `ainvoke` 会失败。

**这意味着**：1 小时的 timeout 是"从启动到完成"的总时长，包括所有人确认等待时间。如果单个 gate 确认可能等很久，需要确保 timeout 足够大，或者后续优化为"只计非等待时间"。

---

## 5. ConfirmationStore：Token 的签发与校验

### 5.1 Redis 存储结构

```python
# app/infrastructure/redis_/confirmation_store.py
import hashlib, secrets, time
from redis.asyncio import Redis

class ConfirmationStore:
    """confirmation token 的签发、校验、废除。"""

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
  → POST /confirm {session_id, step, approved, user_id}
  → ConfirmationStore.issue() → 生成 token_id，存 Redis
  → graph.ainvoke(Command(resume=token))
  → interrupt 返回 token
  → GateManager.await_confirmation 继续执行:
      if not confirmation.valid_for(card.writes_via_action()):
          return "REJECT"        ← token 与写动作不匹配，拒绝
      return "PASS" if confirmation.approved else "REJECT"
  → 若 PASS，gate 节点返回
  → write_via_appservice 节点拿 token 调 ACL 写客户端
  → ACL 写客户端调 REST 时，header 带 X-Confirmation-Token: {token_id}
  → 应用服务收到后，可选地回调 ConfirmationStore.validate() 二次校验
```

---

## 6. ActionCardDispatcher：动作卡推送

### 6.1 双通道推送

```python
# app/application/action_card_dispatcher.py
class ActionCardDispatcher:
    """推送动作卡到责任人：WebSocket（实时）+ Kafka（持久兜底）。"""

    def __init__(self, ws_manager: WebSocketManager,
                 kafka_producer: ActionCardProducer):
        self._ws = ws_manager
        self._kafka = kafka_producer

    async def push(self, card: ActionCard) -> None:
        # 1. 查该 session 的当前责任人（从 session context 或工位映射表）
        assignee = await self._resolve_assignee(card)

        # 2. WebSocket 实时推（如果责任人在线）
        await self._ws.send_to_user(assignee.user_id, card.model_dump_json())

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
class GateManager:
    def __init__(self, ...):
        self._deadlines: dict[str, asyncio.Task] = {}  # card_id → deadline timer

    async def await_confirmation(self, session_id, step, card):
        await self._dispatcher.push(card)

        if card.deadline:
            # 启动 deadline 计时器
            deadline_task = asyncio.create_task(self._deadline_watch(
                session_id, step, card))
            self._deadlines[card.card_id] = deadline_task

        confirmation = await interrupt(value=card)

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
        await asyncio.sleep(delay)
        # 超时了，人还没确认
        await self._suspend_session(session_id, step, "deadline 超时")
        # 注意：此时 interrupt 还在等待，需要外部机制来取消它
        # 实际实现中，需要通过 Command(resume=...) 投入一个 rejected token

    async def _suspend_session(self, session_id, step, reason):
        await self._repo.update_status(session_id, "SUSPENDED")
        await self._dispatcher.push_exception_card(session_id, step, reason)
```

---

## 7. 写路径：从 Agent 草拟到应用服务落库

### 7.1 完整写路径（以"批次隔离下达"为例）

```
Step 1: FaultImpactAgent (B) 推理
  → 调设备遥测 ACL → 故障模式 = "软漂移"
  → 调 FMEA ACL → 漂移参数 × 产品敏感度
  → 调批次 ACL → 窗口内批次 = [B1, B2, B3]
  → 草拟隔离卡: draft_isolation_card(batches=[B1,B2,B3], reason="...")

Step 2: gate: ISOLATION
  → 动作卡推给质量工程师
  → 工程师点开 evidence trace_id，验证 agent 推理
  → 工程师确认 → POST /confirm

Step 3: gate 返回 PASS
  → write_via_appservice 节点执行
  → 调 ReworkWriteAclClient.issue_isolation(batches, reason, confirmation)

Step 4: ReworkWriteAclClient
  → 校验 confirmation token 有效性
  → POST /api/isolation-orders {batches, reason, confirmation_id}
  → header: X-Confirmation-Token: {token_id}

Step 5: 返工上下文应用服务 (Java)
  → IsolationOrderApplicationService.issue()
  → IsolationAggregate.issue(batch_set, reason)
    → 聚合根不变式校验：
      - 批次状态必须允许隔离
      - 隔离原因不能为空
      - ...
  → 事务提交：isolation_order 行 + outbox_event(BatchIsolated) 同一事务
  → 返回 201 Created

Step 6: Agent 收到 201
  → gate_decision = PASS 落 l3_step_record
  → 图继续执行下一步
```

### 7.2 ACL 写客户端的实现细节

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

## 8. State 的完整定义（L3State）

```python
# app/domain/l3_state.py
from pydantic import BaseModel
from typing import Any
from datetime import datetime

class TenantContext(BaseModel):
    tenant_id: str
    workshop: str
    line: str
    scopes: list[str]

class L3State(BaseModel):
    """L3 编排的 state，LangGraph 的 StateGraph 用它做 channel schema。"""

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
session S-001 (thread_id=S-001)  →  StateGraph 实例 A
session S-002 (thread_id=S-002)  →  StateGraph 实例 B (同一 CompiledGraph 的不同调用)
session S-003 (thread_id=S-003)  →  StateGraph 实例 C
```

- 同一 `CompiledGraph`（换线图）可以被多个 session 并发调用。
- 每个 `ainvoke` 调用是独立的——LangGraph 按 `thread_id` 隔离 state。
- `SqlSaver` 的 `thread_id` 是联合主键的一部分，MySQL 的行锁保证同一个 thread_id 的 checkpoint 写是串行的。

### 9.2 agent 能力实例的隔离

```python
# 每个 session 调用 agent 能力时，创建独立的 subgraph 调用
def _run_agent(self, capability: str):
    async def fn(state: L3State) -> L3State:
        sub = self._agents.get(capability)  # 获取共享的 subgraph 实例
        # sub.ainvoke 每次调用都是独立的——state 来自参数，不共享
        result = await sub.ainvoke(
            state,
            config={"configurable": {"thread_id": f"{state['session_id']}_{capability}"}}
            #                                              ↑ 注意：subgraph 用独立的 sub-thread_id
        )
        ...
    return fn
```

**关键**：subgraph 的 `thread_id` 是 `{session_id}_{capability}`，不是 `session_id`——这样 supervisor 的 checkpoint 和 subgraph 的 checkpoint 在 MySQL 中是分开的，不会互相覆盖。

### 9.3 工具集隔离

```python
class ToolRegistry:
    def tools_for(self, capability: str, tenant: TenantContext) -> list[ToolDescriptor]:
        return [
            d for d in self._descriptors.values()
            if d.capability == capability
            and tenant.can_access(d.required_tenant_scopes)
        ]
```

- 每个 agent 能力 (A/B/C/D) 有独立的工具集。
- 同一个 session 的不同能力调用不会互相看到对方的工具。
- 不同 session 的同一能力调用共享同一套工具描述符（只读，线程安全），但工具调用时 tenant 过滤确保数据隔离。

---

## 10. 失败恢复的完整状态机

### 10.1 session 的状态流转

```
PLANNING → RUNNING → DONE
                   → SUSPENDED → (手动恢复) → RUNNING → DONE
                   → FAILED
```

### 10.2 各状态的含义与触发条件

| 状态 | 含义 | 触发条件 |
|------|------|---------|
| PLANNING | 会话刚创建，图尚未开始执行 | `L3Orchestrator.start()` 创建 session 时 |
| RUNNING | 图正在执行或 gate 等待中 | `_drive` 开始后 |
| SUSPENDED | 流程挂起，等待人工干预 | gate deadline 超时 / agent 连续失败 2 次 / barrier 检测到缺料 |
| DONE | 正常完成 | `done_node` 执行完毕 |
| FAILED | 异常终止 | `recursion_limit` 超限 / 整体 timeout / 未捕获异常 |

### 10.3 SUSPENDED 后的恢复

```python
class L3Orchestrator:
    async def resume_suspended(self, session_id: str,
                               user_decision: str,  # "RETRY" / "ABORT" / "SKIP"
                               tenant: TenantContext) -> L3Session:
        session = await self._sessions.get(session_id)
        if session.status != "SUSPENDED":
            raise InvalidStateError(f"session {session_id} 不是 SUSPENDED 状态")

        if user_decision == "RETRY":
            # 重新驱动：从当前 checkpoint 续跑
            session.status = "RUNNING"
            await self._sessions.save(session)
            asyncio.create_task(self._drive(session, tenant))
        elif user_decision == "ABORT":
            session.status = "FAILED"
            await self._sessions.save(session)
        elif user_decision == "SKIP":
            # 跳过当前步骤，注入一个手工 state 后继续
            session.status = "RUNNING"
            await self._sessions.save(session)
            await self._supervisor.ainvoke(
                Command(update={"skip_current_step": True}),
                config={"configurable": {"thread_id": session_id}},
            )
        return session
```

---

## 11. agent 连续失败的计数与隔离

```python
# app/orchestration/code_nodes/barrier.py (或 supervisor_graph.py 内)
class FailureTracker:
    """追踪同一个 session 内 agent 调用的连续失败次数。"""

    def __init__(self, repo: L3Repo):
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
    async def fn(state: L3State) -> L3State:
        sub = self._agents.get(capability)
        try:
            result = await sub.ainvoke(state, config=...)
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

## 12. 启动断言：红线的完整实现

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    reg = app.state.tool_registry

    # 断言 1: 所有写工具必须声明 requires_confirmation + writes_via
    for d in reg.all():
        if not d.read_only:
            assert d.requires_confirmation, (
                f"写工具 {d.name} 必须 requires_confirmation=True，否则进程无法启动"
            )
            assert d.writes_via, (
                f"写工具 {d.name} 必须声明 writes_via（指向落库应用服务），否则进程无法启动"
            )

    # 断言 2: supervisor 能力下无任何工具
    supervisor_tools = reg.tools_for("supervisor", ANY_TENANT)
    assert len(supervisor_tools) == 0, (
        f"supervisor 不应持有任何工具，当前注册了 {[t.name for t in supervisor_tools]}"
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
    # （由 NoWriteClientGate 扫描，同 L2 的机制）

    yield
```

---

## 13. 面试时可引用的实现细节清单

被问"这具体怎么实现"时，可以按以下顺序引用：

| 追问 | 回答要点 | 关键代码位置 |
|------|---------|------------|
| "状态怎么存的" | SqlSaver 三张表（checkpoints / checkpoint_writes / checkpoint_blobs），thread_id=session_id，checkpoint 是完整 state 快照的序列化 BLOB | §2 |
| "interrupt 怎么暂停的" | LangGraph 抛出 GraphInterrupt 异常，checkpoint 落 MySQL，ainvoke 进入等待状态 | §3.1 |
| "resume 怎么恢复的" | Command(resume=token) 通过同一 thread_id 的 ainvoke 投入，从 MySQL 加载 checkpoint，interrupt 返回 token 后继续执行 | §3.2 |
| "进程重启了怎么办" | state 在 MySQL 不在进程内存，新进程用同一个 thread_id 调 ainvoke 即可从 checkpoint 恢复 | §3.4 |
| "token 怎么防伪造" | Redis 存储，token_id 随机 32 字符 hex，绑定 action:target，带 30 分钟 TTL，校验时查 Redis 验证存在性 + action 匹配 | §5 |
| "写怎么不走旁路" | ACL 写客户端调应用服务 REST，写路径过聚合根不变式 + 事务发件箱；confirmation_id 做幂等键 | §7 |
| "多个 session 怎么隔离" | thread_id 不同 → checkpoint 行不同 → state 物理隔离；agent 能力 subgraph 用 sub-thread_id | §9 |
| "超时怎么计时" | asyncio.wait_for 包住整个 ainvoke，从 _drive 开始计时，包含 gate 等待时间；gate 单独有 deadline 计时器 | §4.3 / §6.3 |
| "agent 失败怎么隔离" | FailureTracker 按 session+capability 计数，连续 2 次失败 → status=SUSPENDED，不自动重试 | §11 |
| "红线怎么保证" | 5 条启动断言，进程起不来 = 配置有问题，不靠文档或代码审查 | §12 |
| "走一遍具体场景" | 4 类典型长程任务（换线 / 故障复产 / 客诉 8D / 工艺变更）的逐步运行细节：每步节点性质（CODE/AGENT）、state 变化、checkpoint 链、interrupt-resume、ACL/应用服务落库、l3_step_record | §14 |

---

## 14. 使用场景全流程演练：4 类典型长程任务的逐步运行细节

> 前面 §1 用换线给了全景骨架，§2–§13 逐层拆了机制。本节把机制串起来，用 **4 个真实业务场景**演练"一次长程任务在程序里每一步到底发生了什么"——每个场景先给一个具体业务故事，再按时间线落到**节点执行 / state 变化 / checkpoint 写入 / interrupt-resume / ACL 调用 / 应用服务落库 / l3_step_record 记录**，被追问"走一遍具体场景"时可直接引用。
>
> 4 个场景对应 `L3State.scenario` 的 4 个枚举（见 §8、[实现方案 §1.4](../L3编排型Agent/L3编排型Agent-实现方案.md)）：`CHANGEOVER` / `FAULT_RESPONSE` / `COMPLAINT_8D` / `PROCESS_CHANGE`。
>
> **口径**：仍是设计级骨架演练，时间戳为示意（T+0、T+2min 形式），非线上实绩；token 的 `action` 取 `card.writes_via_action()`，形如 `写动作:目标`（见 §5.3、架构图 §5 时序），表中为示意值；checkpoint_id 简写为 `cp-N`；gate PASS 后由 `card.writes_via` 触发写落库（见 §5.5、架构图 §5），不逐场景重复该机制，仅在表中标注"写落库"行。

### 14.1 场景① 换线（CHANGEOVER）：快路径 + mismatch 慢路径

换线是 L3 的高频场景，也是"懂什么时候不用 AI"的典型——全程 PASS 时纯代码骨架跑完，LLM 调用为 0；只有钢网/程序 mismatch 时才触发 RootCauseAgent (A)。本节演练两条路径。

#### 14.1.1 业务背景

SMT 车间 1 线 L-01，白班 08:00 开始把工单 `WO-2026-0701` 从产品 A 换到产品 B：
- 目标工艺路线 `RR-B v4`（钢网 ST-B、贴片程序 PB-B-v4）
- 产线设备 `ASSET-01`（贴片机）
- 线长张工（user_id=u_zhang）在换线看板点"开始换线"

系统侧：换线看板调 `POST /agent/l3/changeover/start`，body 带 work_order_id / target_route_id / target_route_version / asset_id / tenant。`L3Orchestrator.start()` 创建 session（id=`S-CO-20260712-001`，scenario=CHANGEOVER，status=PLANNING），`asyncio.create_task` 点火 `_drive`，HTTP 立即返回 session_id。

#### 14.1.2 快路径：全程 PASS（零 LLM 调用）

张工这次准备充分：首件已合格、工艺 v4 已就绪、钢网 ST-B 已扫码上机、物料齐套 100%。图只走左侧快路径，**全程不触发 agent A，LLM 调用次数 = 0**（可由 `l3_node_total{node_type=CODE}` 与 `l3_llm_invocation_total=0` 验证，见实现方案 §8）。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（checkpoint / l3_step_record） |
|------|------|------|------|------|
| T+0 | `start` HTTP | L3Orchestrator.start：session 写 MySQL（status=PLANNING），create_task(_drive) 点火，返回 session_id | session.status=PLANNING | l3_session 行写入 |
| T+1s | `plan`（CODE） | 代码节点：按 scenario=CHANGEOVER 决定步骤序列 | current_step=PLAN | cp-1(parent=null)；step=PLAN, node_type=CODE |
| T+2s | `first_article`（CODE） | query_first_article：调过点 ACL 查首件状态，返回 PASS | first_article_result={status:PASS, article_id:FA-001} | cp-2(parent=cp-1) |
| T+3s | `gate_first_article`（CODE） | _gate：build_action_card（intent="首件 FA-001 已合格，确认进入工艺激活"）→ save_step(GATE_WAITING) → dispatcher.push（WebSocket 推张工 + Kafka 持久）→ **interrupt(value=card)** | current_step=GATE_FIRST_ARTICLE | cp-3(parent=cp-2) 落 MySQL，ainvoke 挂起；step=FIRST_ARTICLE, status=GATE_WAITING |
| T+3s~T+5min | （等待人确认） | 张工看板点确认 → `POST /confirm` {step:FIRST_ARTICLE, approved:true, user_id:u_zhang} → ConfirmationStore.issue token（确认动作，无写落库）→ `graph.ainvoke(Command(resume=token), thread_id=S-CO-...001)` | （interrupt 返回 token） | SqlSaver 加载 cp-3，resume；step=FIRST_ARTICLE, status=CONFIRMED, gate_decision=PASS, gate_decided_by=u_zhang |
| T+5min | `process_switch`（CODE） | query_active_route：调工艺管理 ACL（强制 route_version=v4）查路线，返回 ACTIVE | process_switch_result={route_id:RR-B, version:v4, status:ACTIVE} | cp-4(parent=cp-3)；step=PROCESS_SWITCH, node_type=CODE |
| T+6min | `gate_process_switch`（CODE） | _gate：card intent="激活工艺路线 RR-B v4"，writes_via="工艺管理上下文.application.activate_route" → push → interrupt | current_step=GATE_PROCESS_SWITCH | cp-5(parent=cp-4) 落 MySQL，挂起；step=PROCESS_SWITCH, status=GATE_WAITING |
| T+6min~T+9min | （等待人确认） | 工艺工程师李工确认激活 → POST /confirm → issue token（action=`process_route.activate:RR-B:v4`）→ Command(resume=token) | （resume） | 加载 cp-5，resume；step=PROCESS_SWITCH, status=CONFIRMED, gate_decision=PASS |
| T+9min | **写落库**（gate PASS 触发） | write_via_appservice 拿 token 调工艺管理上下文 ACL：POST /api/process-routes/RR-B/activate，header `X-Confirmation-Token`。应用服务过聚合根不变式 + 事务发件箱落 ProcessRouteActivated | gate_process_switch=PASS | cp-6(parent=cp-5)；step=PROCESS_SWITCH_WRITE, node_type=CODE |
| T+9min | `tooling_check` ‖ `kitting_check`（CODE，并行） | conditional_edges 返回 ["tooling_check","kitting_check"] 并行派发。tooling：expected=ST-B vs 扫码 actual=ST-B → PASS；expected=PB-B-v4 vs 本地 PB-B-v4 → PASS。kitting：齐套率=100% → PASS | tooling_result={status:PASS}, kitting_result={status:PASS} | cp-7(parent=cp-6) 两分支汇合；step=TOOLING_CHECK/KITTING_CHECK, node_type=CODE |
| T+10min | `barrier`（CODE） | _barrier_node：t=PASS ∧ k=PASS → barrier_route=draft_release（确定性分流，非 agent） | barrier_route=draft_release | cp-8(parent=cp-7)；step=BARRIER, node_type=CODE |
| T+10min | `draft_release`（CODE） | draft_release_card：结构化拼装放行卡（非 LLM），intent="WO-2026-0701 换线核对完成，放行生产" | action_card=放行卡 | cp-9(parent=cp-8) |
| T+10min | `gate_release`（CODE） | _gate：push → interrupt | current_step=GATE_RELEASE | cp-10(parent=cp-9) 落 MySQL，挂起；step=RELEASE, status=GATE_WAITING |
| T+10min~T+12min | （等待人确认） | 张工确认放行 → POST /confirm → issue token（action=`pass_execution.release:WO-2026-0701`）→ Command(resume=token) | （resume） | 加载 cp-10，resume；step=RELEASE, status=CONFIRMED, gate_decision=PASS |
| T+12min | **写落库**（放行） | write_via_appservice 调**过点上下文应用服务**放行（**过点主事务 + 规则引擎判定 P99≤200ms，agent 不进主事务**），发件箱落 PassReleased | gate_release=PASS | cp-11(parent=cp-10)；step=RELEASE_WRITE, node_type=CODE |
| T+12min | `done`（CODE） | session.status=DONE | status=DONE | cp-12(parent=cp-11)；step=DONE |

**快路径要点**：3 个 gate（FIRST_ARTICLE / PROCESS_SWITCH / RELEASE）全是代码节点，agent A 全程未触发，`l3_llm_invocation_total=0`。这正是实现方案 §0 的判断标准落地——三问皆否走代码节点。

#### 14.1.3 慢路径：钢网 mismatch 触发 RootCauseAgent (A)

同样的换线，但 T+9min 工艺激活后，tooling_check 扫码读到 `actual=ST-A`（不是 ST-B）。barrier 分流到 RootCauseAgent——这才是 agent 赚回成本的地方（痛点 A，见[痛点文档 §A](../L3编排型Agent/L3编排型Agent-痛点操作步骤与解决方案.md)）。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（checkpoint / l3_step_record） |
|------|------|------|------|------|
| … | …（plan/first_article/process_switch 同快路径，略） | | gate_process_switch=PASS | cp-6 |
| T+9min | `tooling_check` ‖ `kitting_check`（CODE，并行） | tooling：expected=ST-B vs actual=ST-A → FAIL(code=TOOLING_STENCIL_MISMATCH)。kitting：齐套=100% → PASS | tooling_result={status:FAIL, code:TOOLING_STENCIL_MISMATCH, expected:ST-B, actual:ST-A}, kitting_result={status:PASS} | cp-7(parent=cp-6)；step=TOOLING_CHECK, node_type=CODE, tooling_result 落库 |
| T+10min | `barrier`（CODE） | _barrier_node：t=FAIL → barrier_route=root_cause，把 expected/actual/code 注入 state 给 agent | barrier_route=root_cause, expected=ST-B, actual=ST-A, mismatch_code=TOOLING_STENCIL_MISMATCH | cp-8(parent=cp-7)；step=BARRIER, node_type=CODE |
| T+10min | `root_cause`（**AGENT A**） | _run_agent("root_cause")：subgraph.ainvoke，**thread_id=`S-CO-...001_root_cause`**（sub-thread，见 §9.2，与 supervisor checkpoint 分开）。RootCauseAgent 自适应取证（LLM + 只读 toolset）：<br>① query_stencil_lending → ST-A 借出未还<br>② query_last_changeover_close → 上工单 WO-2026-0630 收线未触发归还<br>③ query_route_audit → v4 录入 ST-B 无误<br>④ 输出根因假设"ST-A 未还库 + ST-B 未领"，confidence=high，草拟处置卡（suggested_actions: 归还 ST-A / 领用 ST-B，route_to: 库管+线长） | agent_hypothesis={root_cause:"ST-A 未还库 + ST-B 未领",...}, agent_confidence=high, action_card=处置卡 | supervisor cp-9(parent=cp-8)；subgraph 内部 checkpoint 落 sub-thread；step=ROOT_CAUSE, **node_type=AGENT**, capability=A, tool_call_traces=[trace_id 列表] |
| T+11min | `gate_disposition`（CODE） | _gate：build_action_card（含 agent_hypothesis + evidence trace_id + confidence=high + risk_note）→ push → interrupt。**确认人可点开 evidence 回溯 agent 推理，基于证据确认而非盲批**。同时启动 deadline 计时器（card.deadline，见 §6.3） | current_step=GATE_DISPOSITION | cp-10(parent=cp-9) 落 MySQL，挂起；step=DISPOSITION, status=GATE_WAITING, agent_hypothesis 落库 |
| T+11min~T+18min | （等待人确认） | 库管确认处置 → POST /confirm {step:DISPOSITION, approved:true} → issue token（action=`tooling.swap:ASSET-01`）→ Command(resume=token)。**期间 deadline 计时器在走，超时则 _suspend_session 推异常卡** | （resume；取消 deadline 计时器） | 加载 cp-10，resume；step=DISPOSITION, status=CONFIRMED, gate_decision=PASS |
| T+18min | **写落库**（处置） | write_via_appservice 调钢网上下文应用服务：POST /api/tooling/returns（归还 ST-A）+ POST /api/tooling/issues（领用 ST-B），header 带 X-Confirmation-Token，**confirmation_id 做幂等键**（见 §7.3）。过聚合根不变式 + 发件箱 | gate_disposition=PASS, retry_tooling=true | cp-11(parent=cp-10)；step=DISPOSITION_WRITE, node_type=CODE |
| T+18min | `gate_disposition` 条件边 → `tooling_check`（重检） | conditional_edges：retry_tooling=true → 回 tooling_check（**代码节点重检，agent 不参与"重检通过没"的判定**） | — | cp-12(parent=cp-11) |
| T+19min | `tooling_check`（CODE，重检） | 产线已换上 ST-B：expected=ST-B vs actual=ST-B → PASS | tooling_result={status:PASS} | cp-13(parent=cp-12)；step=TOOLING_CHECK, node_type=CODE |
| T+19min~T+21min | `barrier` → `draft_release` → `gate_release` → 放行写落库 → `done` | 同快路径后半段 | status=DONE | cp-14… |

**慢路径要点**：
- **agent A 只在 mismatch 分支触发**：barrier 的分流是确定性代码（PASS→放行 / FAIL→A / 缺料→挂起），agent 不参与"要不要放行"的判定。
- **subgraph 用 sub-thread_id**（§9.2）：`S-CO-...001_root_cause`，与 supervisor 的 checkpoint 物理隔离，不互相覆盖。
- **重检回路**：A 草拟处置 → 人确认 → 处置落库 → 回 tooling_check 重检（代码），agent 不参与重检判定——这是实现方案 §5.1 的硬边界。
- **取证路径由中间结果驱动**：A 查到"ST-A 借出未还"后自适应去查上工单收线记录，不是固定 JOIN——这是痛点 A 降复杂度的核心（见痛点文档 §A.3）。

---

### 14.2 场景② 设备故障复产（FAULT_RESPONSE）：嵌入 FaultImpactAgent (B)

#### 14.2.1 业务背景

SMT-1 线 L-01，14:30 贴片机 ASSET-01 报警停机（温控阀故障）。维修工李工到场维修。痛点不在维修本身（那是维修单的活），而在**ASSET-01 从什么时候开始参数漂移、漂移期间生产了哪些批次、这些批次要不要隔离**——这步现场几乎没人做好（痛点 B，见痛点文档 §B.1）：
- 若是硬停（故障前参数正常），只隔离报警时刻前后批次；
- 若是软漂移（参数早就偏了只是今天才报警），要往前回溯——漂移起始时间未知，夜班批次可能漏隔离。

设备故障事件（Kafka topic `equipment.fault` 或维修看板手动触发）调 `POST /agent/l3/fault_response/start`，body 带 asset_id / fault_time / tenant。session id=`S-FR-20260712-001`，scenario=FAULT_RESPONSE。

#### 14.2.2 逐步运行细节

图结构见架构图 §4.1：`draft_repair_order ‖ FaultImpactAgent(B)` 并行 → `gate:REPAIR ‖ gate:ISOLATION` 并行 → 计量复校 gate → 复产首件 gate → done。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（checkpoint / l3_step_record） |
|------|------|------|------|------|
| T+0 | `start` HTTP | 设备故障事件触发，session 写 MySQL（status=PLANNING），create_task 点火 | session.status=PLANNING | l3_session 行 |
| T+1s | `plan`（CODE） | 按 scenario=FAULT_RESPONSE 决定步骤序列 | current_step=PLAN | cp-1 |
| T+2s | `draft_repair_order` ‖ `fault_impact`（并行派发） | conditional_edges 返回两目标并行：<br>左 `draft_repair_order`（CODE）：结构化拼装维修单（设备 ASSET-01、故障现象、报修人李工）<br>右 `fault_impact`（**AGENT B**）：subgraph thread_id=`...001_fault_impact`，LLM + 只读 toolset 自适应取证 | （并行执行） | cp-2(parent=cp-1) |
| T+2s~T+30s | `fault_impact`（**AGENT B**，子步骤） | B 推理故障模式 × 漂移窗口 × 产品敏感度：<br>① query_equipment_telemetry → 温控阀温度曲线长期偏移，**推理故障模式=软漂移**，估漂移起始窗口=[昨晚 22:00, 14:30]（时序形态判断，规则引擎做不了）<br>② query_batches_in_window → 窗口内 ASSET-01 生产批次=[B-501(夜班), B-502(夜班), B-503(白班)]<br>③ query_process_fmea + query_product_sensitivity → 漂移参数=贴装压力，敏感产品=0201 细间距（B-501、B-502），0603 粗间距（B-503）不受影响<br>④ draft_isolation_card → 隔离集={B-501,B-502}，放行 B-503，含每批次隔离理由 + 证据 trace_id | agent_hypothesis={fault_mode:"软漂移", drift_window:["22:00","14:30"], isolation_set:["B-501","B-502"], release:["B-503"], sensitivity_reason:...}, agent_confidence=high, action_card=隔离卡 | supervisor cp-3；subgraph checkpoint 落 sub-thread；step=FAULT_IMPACT, **node_type=AGENT**, capability=B, tool_call_traces 落库 |
| T+30s | `gate_repair` ‖ `gate_isolation`（CODE，并行） | 两 gate 并行 interrupt：gate_repair 推维修单卡给李工；gate_isolation 推隔离卡给质量工程师王工（含 agent_hypothesis + evidence + confidence）→ interrupt。各启 deadline 计时器 | current_step=GATE_REPAIR / GATE_ISOLATION | cp-4(parent=cp-3) 落 MySQL，两 gate 各自挂起；step=REPAIR/ISOLATION, status=GATE_WAITING |
| T+30s~T+20min | （并行等待人确认） | 李工确认维修单（边修边确认）；王工点开隔离卡 evidence 验证 B 的推理（漂移窗口 + 敏感度理由），确认隔离集。各 POST /confirm → 各 issue token（维修单 action=`repair_order.issue:ASSET-01`；隔离 action=`isolation.issue:WS-A`）→ 各 Command(resume=token) | （两 interrupt 各自 resume） | 加载 cp-4，分别 resume；step=REPAIR/ISOLATION, status=CONFIRMED, gate_decision=PASS |
| T+20min | **写落库**（隔离） | write_via_appservice 调返工上下文 ACL（ReworkWriteAclClient，见 §7.2）：校验 token → POST /api/isolation-orders {batches:[B-501,B-502], reason, confirmation_id}，header X-Confirmation-Token。返工上下文 IsolationAggregate.issue 过不变式 + 事务发件箱落 BatchIsolated。**confirmation_id 做幂等键** | gate_isolation=PASS | cp-5(parent=cp-4)；step=ISOLATION_WRITE, node_type=CODE |
| T+20min | **写落库**（维修单） | 调设备/维修上下文应用服务落维修单 | gate_repair=PASS | （同 checkpoint 链）；step=REPAIR_WRITE, node_type=CODE |
| T+20min~T+45min | （维修进行 + 计量复校） | 李工完成维修，设备计量复校，复校结果回填 | — | — |
| T+45min | `gate_recalibration`（CODE） | 计量复校 gate：推复校确认卡 → interrupt → 人确认（**确定性 gate，不嵌 agent，agent 不碰复校红线**）。issue token（action=`calibration.confirm:ASSET-01`，确认动作） | gate_recalibration=PASS | cp-6；step=RECALIBRATION, status=CONFIRMED |
| T+46min | `gate_restart_first_article`（CODE） | barrier 等复校+点检 PASS → 推复产首件放行卡 → interrupt → 人确认 → 过点上下文放行复产（过点主事务 + 规则引擎） | gate_restart_first_article=PASS | cp-7；step=RESTART_FA, status=CONFIRMED |
| T+47min | `done`（CODE） | session.status=DONE | status=DONE | cp-8；step=DONE |

**场景②要点**：
- **B 的隔离范围判定是三维动态**（故障模式 × 漂移窗口 × 产品敏感度），规则引擎会随产品族/设备类型爆炸，agent 用"遥测形态推理 + FMEA 关联"降复杂度（痛点 B）。
- **隔离集命中夜班批次**：B 推理出软漂移窗口覆盖昨晚 22:00，隔离了夜班 B-501/B-502——这是人凭记忆"10 点报警才坏"会漏掉的（痛点 B.1 步骤 2 的真实风险）。
- **复校 / 复产首件两道红线是代码 gate**，agent 不碰（实现方案 §5.1 硬边界）。
- **隔离下达仍是代码**：B 只草拟隔离集，人确认后走返工上下文应用服务，过聚合根不变式 + 发件箱，agent 不直写。

---

### 14.3 场景③ 客诉 8D（COMPLAINT_8D）：嵌入 TraceabilityAgent (C) + DraftAgents (D)，含跨进程恢复

#### 14.3.1 业务背景

客户反馈批次 `P-2026-0605` 焊接不良退货。质量工程师王工触发客诉追溯。痛点在**跨 5 上下文手工串证据 + 5M1E 假设靠人脑排 + 版本串错**（痛点 C，见痛点文档 §C.1）——最隐蔽的坑是：工艺界面默认显示当前 v5，工程师若用 v5 分析批次 P（其实 P 过点时用的是 v3），根因分析建立在错误工艺基础上。

触发：`POST /agent/l3/complaint_8d/start`，body 带 batch_id=P-2026-0605 / tenant。session id=`S-CP-20260712-001`，scenario=COMPLAINT_8D。本场景同时演示**跨进程恢复**（§3.4）——在 gate 等人确认时 agent-service pod 被 OOM Kill，新 pod 接管续跑。

#### 14.3.2 逐步运行细节

图结构见架构图 §4.2：`TraceabilityAgent(C)` → `供应商批次追溯 ‖ 隔离范围判定` 并行 → `gate:ISOLATION` → `DraftAgents.draft_8d(D)` → `gate:8D_PUBLISH` → done。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（checkpoint / l3_step_record） |
|------|------|------|------|------|
| T+0 | `start` HTTP | session 写 MySQL（status=PLANNING），**点火在 pod-1**，create_task(_drive) | session.status=PLANNING | l3_session 行 |
| T+1s | `plan`（CODE） | 按 scenario=COMPLAINT_8D 决定步骤序列 | current_step=PLAN | cp-1 |
| T+2s | `traceability`（**AGENT C**，嵌入 L1） | _run_agent("traceability")：subgraph thread_id=`...001_traceability`，C 把 L1 诊断图作为子图调用：<br>① **版本钉死（ACL 代码做，非 agent）**：从过点记录强取批次 P 用的 route_version=v3（不是当前 v5），后续工艺查询强制 v3——agent 只消费钉死后的版本<br>② 汇聚跨上下文证据：过点 TestResult、设备遥测、锡膏批次 B-77、同批次不良率<br>③ **5M1E 假设排序（LLM 加权推理，规则引擎做不了）**：料因（B-77 在别处也有不良）置信度高、机因（设备参数漂移幅度小）置信度中<br>④ 输出排序假设 + 证据链 + 置信度 | agent_hypothesis={hypotheses:[{cause:"料-锡膏B-77",confidence:high},{cause:"机-设备漂移",confidence:medium}], evidence_chain:[...]}, agent_confidence=high | supervisor cp-2；subgraph checkpoint 落 sub-thread；step=TRACEABILITY, **node_type=AGENT**, capability=C, tool_call_traces 落库 |
| T+20s | `supplier_trace` ‖ `isolation_scope`（并行） | 左 `supplier_trace`（CODE，版本钉死后查）：查 B-77 供应商批次、在库品。右 `isolation_scope`（代码或复用 B 逻辑）：判同批次在库品隔离范围 | （并行） | cp-3(parent=cp-2)；step=SUPPLIER_TRACE/ISOLATION_SCOPE, node_type=CODE |
| T+25s | `gate_isolation`（CODE） | _gate：build_action_card（隔离集 + 每批次理由 + agent_hypothesis + evidence）→ push 给王工 → **interrupt(value=card)**。state 落 MySQL cp-4，ainvoke 挂起等待 | current_step=GATE_ISOLATION | **cp-4(parent=cp-3) 落 MySQL**，pod-1 的 _drive 协程挂起；step=ISOLATION, status=GATE_WAITING |
| T+25s~T+10min | ⚠️ **pod-1 被 OOM Kill**（跨进程恢复演示） | 14:35 pod-1 因内存压力被 K8s OOM Kill，_drive 协程销毁。**但 state 已在 MySQL cp-4，不在进程内存**——session 在 DB 仍 RUNNING，gate 步骤仍 GATE_WAITING。期间王工在另一终端确认隔离 | （pod-1 进程已死） | MySQL cp-4 仍在 |
| T+10min | 王工确认 → POST /confirm（**打到 pod-2**） | 王工点开隔离卡 evidence（trace_id 回溯 C 的 5M1E 推理），确认隔离 → POST /confirm {step:ISOLATION, approved:true} → **请求被负载均衡到 pod-2** → ConfirmationStore.issue token（action=`isolation.issue:WS-A`）→ pod-2 调 `graph.ainvoke(Command(resume=token), thread_id=S-CP-...001)` | （pod-2 接管） | **SqlSaver 从 MySQL 加载 cp-4**，反序列化 state（GATE_ISOLATION 处），interrupt 返回 token，继续执行；step=ISOLATION, status=CONFIRMED, gate_decision=PASS, gate_decided_by=u_wang |
| T+10min | **写落库**（隔离） | write_via_appservice 调返工上下文 ACL：POST /api/isolation-orders，header X-Confirmation-Token，confirmation_id 幂等。过聚合根不变式 + 发件箱 | gate_isolation=PASS | **cp-5(parent=cp-4)**（pod-2 写入，链续上）；step=ISOLATION_WRITE, node_type=CODE |
| T+11min | `draft_8d`（**AGENT D**） | _run_agent("draft_8d")：subgraph thread_id=`...001_draft_8d`，D 拉追溯链（C 已汇聚）+ 历史 8D 案例库（query_history_8d）→ **草拟 8D 报告**（根因/纠正/预防/案例对照，开放生成，代码写不出） | agent_hypothesis={draft_8d:{...}}, action_card=8D 发布卡 | supervisor cp-6(parent=cp-5)；subgraph checkpoint 落 sub-thread；step=DRAFT_8D, **node_type=AGENT**, capability=D |
| T+12min | `gate_8d_publish`（CODE） | _gate：推 8D 发布卡给王工 → interrupt | current_step=GATE_8D_PUBLISH | cp-7(parent=cp-6) 落 MySQL，挂起；step=8D_PUBLISH, status=GATE_WAITING |
| T+12min~T+30min | （等待人确认） | 王工审改 8D 草稿，确认发布 → POST /confirm → issue token（action=`report.publish_8d:P-2026-0605`）→ Command(resume=token) | （resume） | 加载 cp-7，resume；step=8D_PUBLISH, status=CONFIRMED, gate_decision=PASS |
| T+30min | **写落库**（8D 发布） | write_via_appservice 调文档/质量上下文应用服务发布 8D，过不变式 + 发件箱 | gate_8d_publish=PASS | cp-8(parent=cp-7)；step=8D_PUBLISH_WRITE, node_type=CODE |
| T+30min | `done`（CODE） | session.status=DONE（done 节点标 DONE，不依赖已死的 pod-1 _drive） | status=DONE | cp-9；step=DONE |

**场景③要点**：
- **跨进程恢复真实演练**（§3.4）：pod-1 OOM Kill 后，state 不丢（在 MySQL cp-4 不在进程内存），pod-2 用同一 thread_id 调 ainvoke(Command(resume=...)) 从 cp-4 续跑，checkpoint 链 cp-4→cp-5 跨 pod 续上——这是 SqlSaver + thread_id=session_id 映射（§2.3）的价值。done 节点是代码节点，自行标 DONE，不依赖原 _drive 协程。
- **版本钉死是 ACL 代码做**（非 agent）：C 只消费钉死后的 v3，不会用当前 v5 套历史批次 P——这层是代码红线，agent 不碰版本校验（痛点 C.4）。
- **5M1E 假设排序是 LLM 加权推理**：同样证据在不同产品族/历史背景下排序不同，规则引擎做不了（痛点 C.2）。
- **8D 草拟是 D 的开放生成**：代码写不出自然语言 8D，agent 草拟 + 人审改兜底幻觉（痛点 D）。

---

### 14.4 场景④ 工艺变更落地（PROCESS_CHANGE）：嵌入 DraftAgents.draft_sop (D)

#### 14.4.1 业务背景

工艺路线 RR-B 从 v4 升 v5，回流焊温区 3 从 240℃ 调到 245℃（为改善 0201 元件立碑）。工艺变更发布后，订阅 `ProcessRouteActivated` 事件触发 L3：要草拟新 SOP + 核对操作工资质 + 新工艺首件验证（痛点 D，见痛点文档 §D.1）。

触发：Kafka 监听器收 `ProcessRouteActivated{route_id:RR-B, version:v5}` → 调 `POST /agent/l3/process_change/start`。session id=`S-PC-20260712-001`，scenario=PROCESS_CHANGE。

#### 14.4.2 逐步运行细节

图结构见架构图 §4.3：`draft_sop(D) ‖ 资质核对(CODE)` 并行 → `barrier` → `gate:SOP_PUBLISH` → 新工艺首件验证 gate → done。

| 时刻 | 节点（性质） | 程序运行细节 | state 变化 | 持久化（checkpoint / l3_step_record） |
|------|------|------|------|------|
| T+0 | 事件触发 `start` | Kafka 监听器收 ProcessRouteActivated → POST /agent/l3/process_change/start。session 写 MySQL，create_task 点火 | session.status=PLANNING | l3_session 行 |
| T+1s | `plan`（CODE） | 按 scenario=PROCESS_CHANGE 决定步骤序列 | current_step=PLAN | cp-1 |
| T+2s | `draft_sop` ‖ `qualification_check`（并行派发） | conditional_edges 返回两目标并行：<br>左 `draft_sop`（**AGENT D**）：subgraph thread_id=`...001_draft_sop`，D 查 query_route_diff（v4→v5：温区3 +5℃）+ query_prior_sop（v4 旧 SOP）+ query_fmea（0201 立碑风险）→ **草拟新 SOP**（含差异点提示："温区3 245℃，关注 0201 立碑，首件重点检"）<br>右 `qualification_check`（**CODE，确定性，不嵌 agent**）：query 操作工资质 ∈ 工艺要求资质集？张工=回流焊资质√，李工=无资质✗（需培训） | 左 agent_hypothesis={draft_sop:{...}}, action_card=SOP 发布卡；右 qualification_result={qualified:[张工], unqualified:[李工]} | supervisor cp-2；D 的 subgraph checkpoint 落 sub-thread；step=DRAFT_SOP, **node_type=AGENT**, capability=D；step=QUALIFICATION, node_type=CODE |
| T+15s | `barrier`（CODE） | 等双分支汇合：SOP 草拟完成 ∧ 资质核对完成 → barrier_route=draft_release（资质不全则分流挂起推培训） | barrier_route=draft_release | cp-3(parent=cp-2)；step=BARRIER, node_type=CODE |
| T+15s | `gate_sop_publish`（CODE） | _gate：推 SOP 发布卡给工艺工程师（含 D 草拟的 SOP + 差异点提示 + evidence）→ interrupt | current_step=GATE_SOP_PUBLISH | cp-4(parent=cp-3) 落 MySQL，挂起；step=SOP_PUBLISH, status=GATE_WAITING |
| T+15s~T+25min | （等待人确认） | 工艺工程师审改 SOP 草稿，确认发布 → POST /confirm → issue token（action=`sop.publish:RR-B:v5`）→ Command(resume=token) | （resume） | 加载 cp-4，resume；step=SOP_PUBLISH, status=CONFIRMED, gate_decision=PASS |
| T+25min | **写落库**（SOP 发布） | write_via_appservice 调工艺/SOP 上下文应用服务发布 SOP v5，过不变式 + 发件箱落 SOPPublished | gate_sop_publish=PASS | cp-5(parent=cp-4)；step=SOP_PUBLISH_WRITE, node_type=CODE |
| T+25min | `gate_new_route_first_article`（CODE） | 新工艺首件验证 gate：推首件放行卡 → interrupt → 人确认 → 过点上下文放行首件（过点主事务 + 规则引擎）。issue token（action=`pass_execution.release:RR-B:v5`） | gate_new_route_first_article=PASS | cp-6(parent=cp-5)；step=NEW_ROUTE_FA, status=CONFIRMED |
| T+26min | `done`（CODE） | session.status=DONE | status=DONE | cp-7；step=DONE |

**场景④要点**：
- **SOP 草拟嵌 D（开放生成），资质核对是代码**——这是"该用代码的没用 AI"的典型体现（架构图 §4.3）：资质核对是"操作工资质 ∈ 工艺要求资质集"的确定性查询，代码做，不嵌 agent；只有 SOP 草拟（开放生成，代码写不出）嵌 D。
- **生成不碰红线**：D 只草拟 SOP，不触放过点放行、不碰隔离下达，生成能力严格限制在"草拟"（痛点 D.4）。
- **人审改兜底幻觉**：SOP 草稿必须人确认才发布，LLM 幻觉成本留在审改环节，不出厂。

---

### 14.5 4 场景对照表

| 场景 | scenario | 业务触发 | 嵌入 agent | gate 数 | 突出体现的机制 | 全程 PASS 时 LLM 调用 |
|------|----------|---------|-----------|--------|--------------|-------------------|
| ① 换线 | CHANGEOVER | 线长点"开始换线" | A（仅 mismatch 分支） | 3~4（首件/工艺/放行/可选处置） | 代码节点 vs agent 节点边界、barrier 确定性分流、重检回路、sub-thread 隔离 | **0**（快路径纯代码） |
| ② 设备故障复产 | FAULT_RESPONSE | 设备故障事件 | B（隔离范围判定） | 4（维修/隔离/复校/复产首件） | 并行 gate、B 的三维动态判定、复校/复产红线是代码 | 必调（B 是核心） |
| ③ 客诉 8D | COMPLAINT_8D | 客诉触发 | C（5M1E 排序）+ D（8D 草拟） | 2（隔离/8D 发布） | 跨进程恢复、版本钉死（代码）+ 5M1E 排序（agent）、开放生成 | 必调（C+D） |
| ④ 工艺变更落地 | PROCESS_CHANGE | ProcessRouteActivated 事件 | D（SOP 草拟） | 2（SOP 发布/新工艺首件） | SOP 草拟（agent）‖ 资质核对（代码）的边界、生成不碰红线 | 必调（D） |

**一句话**：4 个场景共享同一套机制（SqlSaver 持久化 / interrupt-resume / ConfirmationStore / ActionCardDispatcher / barrier / ACL 写客户端），区别只在**图的装配**（代码节点 + agent 能力的组合）与**触发源**（人 / 设备事件 / 客诉 / 工艺事件）。换线快路径全程零 LLM 是"懂什么时候不用 AI"；场景②③④的 agent 调用分别赚回 B/C/D 三类代码做不了的非确定段——但写闸门（confirmation gate + 应用服务 + 发件箱）在所有场景里始终一致，agent 只草拟、人确认、应用服务落库。