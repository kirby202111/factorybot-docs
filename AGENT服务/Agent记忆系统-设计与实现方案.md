# Agent 记忆系统 -- 设计与实现方案（Python 技术栈，L1/L2/L3 共用）

> 本文档把 factorybot Agent 中所有与"记忆"相关的设计沉淀为一处。它不是新设计，而是把散落在
> `Agent长程任务-实现细节深度剖析.md`（状态持久化 / interrupt-resume）、
> `AgentToken成本优化-设计与实现方案.md`（上下文压缩 / 缓存）、
> `Agent可观测性-设计与实现方案.md`（证据链落库）里的记忆相关内容，
> 按"存给谁看 / 活多久"两个维度重新组织，补齐它们各自没讲全的层间关系。

## 0. 先界定：这里的"记忆"指什么

很多人一听"Agent 记忆"就去找一个叫 `Memory` 的类--在 factorybot 里**没有这个单体模块**。
记忆是一套**分层架构**，按两个正交维度切成五层：

- **存给谁看**：给 LangGraph 引擎看（续跑用）/ 给 LLM 看（推理用）/ 给系统看（审计用）/ 给降本用（命中复用）
- **活多久**：瞬时（单次 LLM 调用）→ 会话内（一个 session）→ 跨会话（多个 session）→ 永久（落库归档）

一句话核心哲学，贯穿全栈：

> **模型看摘要，trace 落全文。**
> 喂给 LLM 的记忆是压缩过的、易失的；留给系统审计的记忆是全量的、永久的。
> 降本不牺牲证据链完整性。

这条哲学的落地锚点在 [result_compactor.py](../factorybot/app/infrastructure/cost/result_compactor.py) 与
[repos.py](../factorybot/app/infrastructure/persistence/repos.py) 的 `ToolCallTrace.output_payload`（存 FULL view）。

## 1. 定位与边界

### 1.1 记忆的红线（一开口就要讲）

1. **版本一致性红线**：`route_version` 必须出现在 state 字段、工具入参、缓存 key、trace 全链路。MES 追溯不允许"查错版本"。
2. **时变性红线**：MES 追溯答案随新数据到达而漂移，**语义缓存默认关闭**。只对时不变场景（不可变工艺版本、稳定模板）灰度开启。
3. **证据不空红线**：每个根因假设必须引用 `trace_id`；`tool_call_trace` 永远存全文，工程师 UI 读 trace 不读压缩版。
4. **写需确认红线**：任何写动作必须经 gate 人确认 + confirmation token，token 同时是下游应用服务的幂等键。

### 1.2 与既有文档的关系（避免重复）

| 既有文档 | 讲了什么 | 本文档的补充 |
|---|---|---|
| `Agent长程任务-实现细节深度剖析.md` | L3State / SqlSaver 三表 / interrupt-resume / 跨进程恢复 / ConfirmationStore | 把它讲的"工作记忆"+"确认令牌"纳入分层视图，补它与上下文压缩、证据链的层间关系 |
| `AgentToken成本优化-设计与实现方案.md` | ResultCompactor / CacheControl / EarlyStop / ToolResultCache / 语义缓存 | 把它讲的"上下文窗口记忆"+"跨会话缓存"纳入分层视图，补"模型看摘要"与"trace 落全文"的双路设计 |
| `Agent可观测性-设计与实现方案.md` | tool_call_trace / node_trace 表族 / 证据链回溯 | 把它讲的"证据链长期记忆"纳入分层视图，补 trace 与 LLM 压缩版的对应关系 |

本文档不重复上述文档已详述的实现细节，而是在"分层"视角下把它们串成一张图，并补齐设计哲学与权衡。

### 1.3 不覆盖

- 不覆盖 L1/L2/L3 各自的业务流程（见各自实现方案）。
- 不覆盖可观测性的指标体系细节（见可观测性文档）。
- 不覆盖写路径的 ACL 客户端实现（见长程任务文档 §7）。

## 2. 记忆分层模型

### 2.1 五层模型总表

| 层 | 名称 | 核心组件 | 存储介质 | 存什么 | 寿命 | 服务对象 | 落地状态 |
|---|---|---|---|---|---|---|---|
| L1 | 工作记忆 | `Checkpointer` + `L3State` | MySQL `SqlSaver`(real) / `MemorySaver`(mock) | 图状态快照（channel 值） | 会话级，可跨进程恢复 | LangGraph 引擎（interrupt/resume） | ✅ mock 跑通 / 🔧 SqlSaver 待启用 |
| L2 | 上下文窗口记忆 | `ResultCompactor` / `CacheControl` / `EarlyStop` | 内存（请求级） | 喂模型的消息压缩、prompt 缓存标记 | 单次 LLM 调用 ~ 5min/1h | LLM | 🔧 已实现类，待接线 |
| L3 | 证据链长期记忆 | `ToolCallTraceRepo` / `NodeTraceRepo` / `L3Step` | MySQL 平铺表 | 工具调用全文、节点执行记录 | 永久 | 工程师 UI / 审计 | ✅ 已落地并接线 |
| L4 | 跨会话缓存 | `ToolResultCache` | Redis | 稳定工具结果的版本化缓存 | 按 tool 策略 TTL | 降本（命中省一次 ACL+回灌） | 🔧 已实现类，灰度接入 |
| L5 | 会话元数据 & 令牌 | `L3Repo` / `SessionManager` / `ConfirmationStore` | MySQL + Redis | 会话生命周期、失败计数、确认令牌 | 会话级 ~ 30min | 编排器 / 写动作闸门 | ✅ 已落地并接线 |

> 落地状态图例：✅ 已落地并接线（mock 端到端跑通）/ 🔧 已实现类，待接线（真实模式或灰度启用）/ 📐 设计中。

### 2.2 分层架构图

```
                        ┌─────────────────────────────────────────────┐
                        │              LLM (ReAct 循环)                │
                        └─────────────────────────────────────────────┘
                              ▲                        ▲
              压缩后摘要回灌 │                        │ prompt 缓存标记
              (L2 ResultCompactor)            (L2 CacheControl)
                              │                        │
   ┌──────────────────────────┴────────────────────────┴──────────────┐
   │  L2 上下文窗口记忆（瞬时，请求级）                                  │
   │  ResultCompactor · CacheControl · EarlyStop · recursion_limit    │
   └──────────────────────────┬───────────────────────────────────────┘
                              │ 工具调用
   ┌──────────────────────────┴───────────────────────────────────────┐
   │  L3 证据链长期记忆（永久）  tool_call_trace / node_trace / l3_step │
   │  ★ output_payload 存 FULL view（压缩前的全文）                    │
   └──────────────────────────┬───────────────────────────────────────┘
                              │ 同步落 trace
   ┌──────────────────────────┴───────────────────────────────────────┐
   │  L4 跨会话缓存（Redis）  ToolResultCache  key=tc:{tenant}:{tool}:{hash(args+route_version)} │
   └──────────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────────┐
   │  L1 工作记忆（会话级，可跨进程）                                    │
   │  LangGraph Checkpointer  thread_id = session_id                   │
   │  MySQL: checkpoints / checkpoint_writes / checkpoint_blobs        │
   │  interrupt(value=card) ──resume──> Command(resume=token)          │
   └──────────────────────────────────────────────────────────────────┘
                              │
   ┌──────────────────────────┴───────────────────────────────────────┐
   │  L5 会话元数据 & 令牌                                              │
   │  L3Repo(会话状态机/失败计数) · ConfirmationStore(Redis, 30min TTL) │
   └──────────────────────────────────────────────────────────────────┘
```

### 2.3 数据流：一次工具调用如何流经各层

以 L1 诊断的一次工具调用为例（[tool_node.py](../factorybot/app/infrastructure/ai/tool_node.py)）：

```
agent_node 产出 pending_tool_calls
   │
   ▼
ToolNode.__call__
   │
   ├─[权限] capability + tenant scope 过滤 ─否─> save_denied (L3) ─> 返回 error 消息
   │
   ├─[执行] descriptor.handler(**args, tenant) -> view (FULL)
   │      │
   │      └─[L4] (设计中) ToolResultCache.get_or_compute 命中则直接返回 view
   │
   ├─[L3 落全文] trace_repo.save_ok(args, view=FULL_view, ...) -> trace_id
   │
   ├─[L2 压缩] (设计中) ResultCompactor.compact(tool_name, view) -> compacted
   │      ★ 当前 mock：模型看 trace_id + 全文；真实模式由 ResultCompactor 压缩
   │
   └─[回灌模型] tool 消息 {trace_id, data: compacted_or_full}
```

关键点：**trace 落的是 FULL view，模型看的是 compacted view**--两条路，同一份数据，不同保真度。
当前 mock 为简化把全文直接喂模型（[tool_node.py:90](../factorybot/app/infrastructure/ai/tool_node.py#L90) 注释），
真实模式接线 ResultCompactor 后才走压缩路径。

## 3. L1 工作记忆：Checkpointer + L3State

这是大家通常说的"Agent 记忆"--**图跑到一半挂起，靠它续跑**。

### 3.1 状态 schema：L3State（channel 模型）

[l3_state.py:128](../factorybot/app/domain/l3_state.py#L128) 定义 supervisor StateGraph 的 channel schema（`TypedDict, total=False`）。
每个字段是一个 LangGraph channel，节点用 `state.get()` 取。关键字段分组：

```python
class L3State(TypedDict, total=False):
    # 会话标识
    session_id: str
    scenario: str

    # 租户上下文（dict 形式以便 LangGraph 序列化）
    tenant: Optional[dict]

    # 业务上下文
    work_order_id: Optional[str]
    target_route_id: Optional[str]
    target_route_version: Optional[str]   # ★ 版本一致性红线

    # 会话状态（并发分支可能同写，用 last_wins reducer）
    status: Annotated[Optional[str], last_wins]
    current_step: Annotated[Optional[str], last_wins]

    # 各步骤结果 + gate 决策
    first_article_result: Optional[dict]
    gate_first_article: Optional[str]
    action_card: Optional[dict]
    ...

    # agent 通信
    pending_tool_calls: list[dict]
    tool_results: list[dict]

    # 写动作透传：gate 确认后把 token 存此，write 节点取用
    confirmation: Optional[dict]
```

`last_wins` reducer（[l3_state.py:15](../factorybot/app/domain/l3_state.py#L15)）：并发分支同写一 channel 时取后者，`None` 不覆盖。
四类场景（换线 / 故障复产 / 客诉 8D / 工艺变更）**共用一套 schema，各场景只装配自己用到的子集**--DDD 共享内核 + 限界上下文装配的落地。

### 3.2 持久化：thread_id = session_id

[checkpointer.py](../factorybot/app/infrastructure/persistence/checkpointer.py)：

```python
def get_checkpointer() -> Any:
    s = get_settings()
    if s.mysql_url and not s.is_mock:
        # real 模式：LangGraph SqlSaver 持久化到 MySQL 三表（示意，真实部署启用）
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    # mock 模式：进程内 MemorySaver（重启丢失，足够 demo）
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()
```

**关键映射**：`thread_id = session_id`（[l3_orchestrator.py:102](../factorybot/app/application/l3_orchestrator.py#L102)）。
LangGraph 按 thread_id 隔离 state；SqlSaver 的 thread_id 是联合主键的一部分，MySQL 行锁保证同 thread_id 的 checkpoint 写串行。

> 落地状态：当前 mock 两个分支都返回 `MemorySaver`（SqlSaver 代码注释示意，真实部署启用）。
> 即"工作记忆"在 mock 下是进程内易失的，端到端流程跑通靠的是同进程内 interrupt/resume；跨进程恢复要等 SqlSaver 接线。

### 3.3 SqlSaver 的 MySQL 三表结构（real 模式）

对齐 LangGraph 官方 SqlSaver，三表联合表达"一次会话的状态历史"：

| 表 | 主键 | 存什么 |
|---|---|---|
| `checkpoints` | `(thread_id, checkpoint_ns, checkpoint_id)` | 每个 checkpoint 的元信息 + parent 指针 |
| `checkpoint_writes` | `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)` | 单个 channel 的写入记录（变更跟踪） |
| `checkpoint_blobs` | `(thread_id, checkpoint_ns, channel, version)` | 大对象分片存储（按 channel 分桶） |

### 3.4 checkpoint 链与"存了什么"

checkpoint 是链式结构（每个带 parent 指针，类似 git commit 链）：

```
checkpoint_1 (parent=null)  <- plan_node done
checkpoint_2 (parent=cp_1)  <- first_article done
checkpoint_3 (parent=cp_2)  <- gate_first_article interrupt point
checkpoint_4 (parent=cp_3)  <- process_switch done (after resume)
```

存的是 **state 本身**（各 channel 当前值）。注意：`interrupt(value=card)` 的 value（动作卡）**不进 checkpoint**--它只是传给外部的值；
恢复时 LangGraph 从最新 checkpoint 重建 state，从 interrupt 的下一行继续。
L3 不用回退（gate 确认后不撤销），但链式结构是 LangGraph 内建能力。

### 3.5 interrupt / resume 协议

gate 节点暂停（[l3_orchestrator.py 注释](../factorybot/app/application/l3_orchestrator.py#L1-L7)）：

```python
async def await_confirmation(self, session_id, step, card):
    await self._dispatcher.push(card)           # 1. 推动作卡
    confirmation = await interrupt(value=card)  # 2. 暂停（GraphInterrupt）
    # —— 协程挂起，直到 Command(resume=...) 喂入 ——
    if not confirmation.valid_for(card.writes_via_action()):
        return "REJECT"
    return "PASS" if confirmation.approved else "REJECT"
```

`interrupt` 做四件事：① 存当前 checkpoint ② 抛 `GraphInterrupt`（控制流信号，非异常）③ 把 value 返回调用方 ④ 协程挂起。

人确认后续跑（[l3_orchestrator.py:128 resume()](../factorybot/app/application/l3_orchestrator.py#L128)）：

```python
state = await graph.aget_state(config)              # 取 pending 动作卡
card = _extract_pending_card(state)
token = await self._store.issue(session_id, step, approved, user_id,
                                action=card.writes_via_action())
await graph.ainvoke(Command(resume=token), config=config)  # 续跑
```

`Command(resume=token)` 内部：① 按 thread_id 从 MySQL 加载最新 checkpoint ② 定位 interrupt 点 ③ 让 interrupt 返回 token ④ 从 interrupt 下一行继续。

### 3.6 跨进程恢复（工作记忆真正发挥价值之处）

```
进程 A (pod-1): graph.ainvoke(initial, thread_id=S-001)
  -> plan_node -> first_article -> gate_first_article -> interrupt!
  -> state 落 MySQL checkpoint -> ainvoke 阻塞等待
  ---- pod-1 OOM Killed ----
进程 B (pod-2): 收到 POST /confirm
  -> graph.ainvoke(Command(resume=token), thread_id=S-001)
  -> SqlSaver 从 MySQL 加载 S-001 最新 checkpoint
  -> 反序列化 state（停在 gate_first_article）
  -> interrupt 返回 token -> 继续 process_switch -> ...
```

**内存里的 `asyncio.Task` 会丢，但 state 不丢**。这是选 `asyncio.create_task` 而非 Celery 的底气--`interrupt`/`resume` 是 LangGraph 原语，要求同事件循环协程语义；状态外置到 MySQL 才是跨进程恢复的真正靠山。

### 3.7 子图 thread 隔离

supervisor 调 subgraph 时用独立 sub-thread_id（文档 §9.2）：

```python
result = await sub.ainvoke(
    state,
    config={"configurable": {"thread_id": f"{state['session_id']}_{capability}"}}
)
```

`{session_id}_{capability}` 使 supervisor 与 subgraph 的 checkpoint 在 MySQL 分开存放，互不覆盖。
这是"多 session 并发 + 子图独立"的隔离手段。

### 3.8 L1 诊断图为何不带 checkpointer

[graph_builder.py:51](../factorybot/app/infrastructure/ai/graph_builder.py#L51) 注释"无 checkpointer，同步跑完"。
L1 是短链路 ReAct（recursion_limit=20），跑完即出报告，**不需要中途暂停**，所以不需要工作记忆持久化。

> **记忆按场景需要才上，不是无脑全配。** L1 无 checkpointer / 无 ResultCompactor 接线；
> L3 必带 checkpointer（长程任务要 gate 暂停 / 跨进程恢复）。这是分层记忆的"按需启用"原则。

## 4. L2 上下文窗口记忆：喂模型的瞬时记忆

这层解决"ReAct 每步重发 system prompt + 历史，token 二次方放大"问题。属于"给 LLM 看的瞬时记忆"。

### 4.1 问题：token 花在哪

```
总输入 token ≈ Σ_{step=1..N} [ system_prompt + tool_definitions + history_{1..step-1} ]
```

三个观察：① `system_prompt + tool_definitions` 每步重发，最大最浪费；② `history` 累积，step k 带 k-1 个历史结果，二次方放大；③ N（步数）是乘子。

### 4.2 ResultCompactor：模型看摘要，trace 落全文

[result_compactor.py:20](../factorybot/app/infrastructure/cost/result_compactor.py#L20)。工具结果回灌前做三件事：

```python
FIELD_WHITELIST: dict[str, list[str]] = {
    "query_pass_records": ["sn", "work_order_id", "station_id", "equipment_id",
                           "route_version", "decision", "blocking_reason"],
    "query_test_results": ["test_id", "station_id", "test_type", "raw_verdict"],
    "query_traceability_graph": ["serial_no", "subgraph_ref", "route_version"],
}
LIST_TRUNCATE = 5  # 列表最多保留前 5 项

class ResultCompactor:
    def compact(self, tool_name: str, view: Any) -> dict:
        # 1. 字段裁剪：按白名单只留关键字段
        # 2. 列表截断：超过 5 项截断
        # 3. 截断标注：_truncated / _omitted_count，让模型知道数据不全
        ...
        out[f"_{k}_truncated"] = True
        out["_omitted_count"] = omitted
```

**截断标注是关键**：让模型知道"这是截断后的 5 条，不是全部"，避免它把局部当全局下结论。

**红线**：`tool_call_trace.output_payload` 存的是压缩前的 FULL view（[repos.py:22](../factorybot/app/infrastructure/persistence/repos.py#L22) 注释"FULL view（证据链全文，非压缩）"）。
压缩只影响"喂模型"路径，证据链完整性不牺牲。

> 落地状态：类已实现，**当前 mock 未接线**（[tool_node.py:90](../factorybot/app/infrastructure/ai/tool_node.py#L90) 注释"真实模式下可由 ResultCompactor 压缩"）。
> mock 直接喂全文，因为 MockChatModel 是确定性驱动，不烧真实 token。

### 4.3 CacheControl：prompt 缓存标记

[cache_control.py](../factorybot/app/infrastructure/cost/cache_control.py)：把稳定的 system prompt / 工具定义打 `cache_control` 标记。

```python
@dataclass
class CacheControl:
    type: Literal["ephemeral"] = "ephemeral"
    ttl: Literal["5m", "1h"] = "5m"

def mark_system_prompt_cache(prompt: str, ttl: str = "5m") -> dict: ...   # system prompt TTL 5min
def mark_tools_cache(tools: list[dict], ttl: str = "1h") -> list[dict]: ...  # 工具定义 TTL 1h
```

- 定价：首次写 1.25x（5min）/ 2x（1h），后续命中 ~0.1x
- N 步会话里，system prompt + 工具定义从"全价发 N 次"变成"写 1 次 + 缓存读 N-1 次"
- **MES 约束**：只标 `system`/`tools` 块；动态上下文（user 问题、tool 结果）绝不进缓存块；
  动态工具集变更会破坏命中--权衡结论：固定工具集 + 缓存命中 通常优于 动态工具集 + 缓存未命中；
  `prompt_version` 变更自然失效（基于内容哈希）。

> 落地状态：类已实现，**ObservableChatModel 未接线**（[observable_chat_model.py](../factorybot/app/infrastructure/ai/observable_chat_model.py) 不接受 cache_control）。
> 真实 LLM 接入时在 ObservableChatModel 外层套 CacheControl.apply()。

### 4.4 EarlyStop：避免烧步数

[early_stop.py:10](../factorybot/app/infrastructure/cost/early_stop.py#L10)：检测冗余探索，提前收口转人工。

```python
class EarlyStopDetector:
    def __init__(self, max_tool_calls: int = 8, min_evidence: int = 2): ...

    def should_stop(self, tool_call_count, evidence_count, model_self_assess=None) -> tuple[bool, str]:
        if tool_call_count >= self._max_tool_calls:          # 工具调用上限
            return True, ...
        if model_self_assess is True and evidence_count >= self._min_evidence:  # 模型自评证据充分
            return True, ...
        if evidence_count >= self._min_evidence * 2:         # 证据冗余
            return True, ...
        return False, ""
```

触发后路由到 `needs_human_review`，不强行作答。

### 4.5 其他杠杆

- `recursion_limit`：L1=20 / L3=40 硬上限，兜底封顶单会话步数（[graph_builder.py:92](../factorybot/app/infrastructure/ai/graph_builder.py#L92)）
- 并行工具调用：`ToolNode` 用 `asyncio.gather` 并发执行多 tool_call，减少回灌次数（降 N）

## 5. L3 证据链长期记忆：给系统留的全量记忆

这层是 MES **追溯性 (Traceability)** 硬约束的落地--所有工具调用、节点执行必须可回溯。对应可观测性文档 §7。

### 5.1 ToolCallTrace（工具调用平铺表）

[repos.py:14](../factorybot/app/infrastructure/persistence/repos.py#L14) + [models.py:18](../factorybot/app/infrastructure/persistence/models.py#L18)：

```python
@dataclass
class ToolCallTrace:
    trace_id: str
    session_id: str
    step_no: int
    tool_name: str
    bounded_context: str          # ★ 锚定 DDD 限界上下文（如"工艺管理上下文"）
    input_payload: dict
    output_payload: dict          # ★ FULL view（证据链全文，非压缩）
    status: str                   # OK | DENIED | ERROR
    latency_ms: int
    tenant_id: str
    occurred_at: datetime
```

三种状态都落：`save_ok` / `save_denied`（被 gate 拒）/ `save_error`。
`bounded_context` 字段把 trace 锚定到 DDD 限界上下文，给工程师 UI 按上下文回溯用。

> 落地状态：✅ 已落地并接线（[tool_node.py:83](../factorybot/app/infrastructure/ai/tool_node.py#L83) 每次调用落一行）。

### 5.2 NodeTrace / L3Step / DraftTrace

- `NodeTraceRepo`（[repos.py:97](../factorybot/app/infrastructure/persistence/repos.py#L97)）：CODE/AGENT 节点执行记录，含 `agent_hypothesis` / `agent_confidence` / `tool_call_traces` 反向引用
- `L3Step`（[l3_state.py:105](../factorybot/app/domain/l3_state.py#L105)）：步骤级记录，状态 `PENDING|RUNNING|GATE_WAITING|CONFIRMED|FAILED`
- `DraftTraceRepo`（[repos.py:85](../factorybot/app/infrastructure/persistence/repos.py#L85)）：L2 草稿归档（L2 只落草稿不落库）

real 模式落 MySQL（[models.py](../factorybot/app/infrastructure/persistence/models.py)），表结构对齐可观测文档 §7.1 / 长程任务 §2.2。

### 5.3 与工程师 UI 回溯的关系

证据链 = span 树的业务投影（可观测性文档 §4.5）。工程师 UI 读的是 `tool_call_trace.output_payload`（全文），
不是 L2 压缩版。这就是"模型看摘要，trace 落全文"的后半句--**LLM 会幻觉，但 trace 永远是全量真相**。

## 6. L4 跨会话缓存：ToolResultCache

[tool_cache.py:15](../factorybot/app/infrastructure/redis_/tool_cache.py#L15)。Redis 精确缓存（**非语义缓存**）：

```python
class ToolResultCache:
    def _key(self, tenant_id, tool_name, args, route_version) -> str:
        payload = json.dumps({"t": tenant_id, "a": args, "r": route_version}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return f"tc:{tenant_id}:{tool_name}:{digest}"

    async def get(self, tenant_id, tool_name, args, route_version=None): ...
    async def set(self, tenant_id, tool_name, args, value, route_version=None, ttl=None): ...
```

### 6.1 版本化精确缓存

- `route_version` **必须进 key 的哈希 payload**--版本一致性红线延伸到缓存层
- 按 key 精确缓存，非语义缓存；命中即归零（省一次 ACL 调用 + 一次 LLM 回灌）

### 6.2 可缓存性按工具分级

| 工具 | 可缓存性 | key 维度 | TTL |
|---|---|---|---|
| `query_process_route(route_id, route_version)` | 高（不可变版本） | route_id + route_version + tenant | 长 |
| `query_material_batch(batch_no)` | 中（慢变） | batch_no + tenant | 中（~10min） |
| `query_pass_records(serial_no)` | 低（持续追加） | serial_no + tenant + max_ts | 短或不缓存 |
| `query_device_params(asset_id, time_range)` | 中（历史不可变） | asset_id + time_range + tenant | 历史窗口长 |

### 6.3 语义缓存为何默认关闭（MES 时变性红线）

语义缓存（embedding 相似度复用答案）在 MES **危险**：追溯答案随新数据到达而变。
昨天的根因，明天加了 50 条缺陷记录后可能就错了。

| 场景 | 语义缓存 | 原因 |
|---|---|---|
| "RR-100 工艺 v3 焊接工站参数" | 允许 | 不可变版本 |
| "8D 报告模板格式" | 允许 | 稳定模板 |
| "批次 B-77 焊接缺陷根因" | **禁止** | 答案随数据变 |
| "SN-001 过点轨迹" | **禁止** | 持续追加 |

> 初期默认关闭语义缓存，只开工具结果缓存；等可观测数据证明某些稳定问题高频重复，再按场景灰度。
> 落地状态：`ToolResultCache` 基础类已实现，ACL 侧 `get_or_compute` 封装待接（灰度）。

## 7. L5 会话元数据 & 确认令牌

### 7.1 L3Repo / SessionManager：会话生命周期记忆

[L3Repo](../factorybot/app/infrastructure/persistence/repos.py#L120) 承载：
- 会话状态机 `PLANNING -> RUNNING -> SUSPENDED/DONE/FAILED`（[l3_state.py:27](../factorybot/app/domain/l3_state.py#L27)）
- **失败计数** `failure_count: dict[str,int]`，按 `(session_id, capability)` 追踪 agent 连续失败，≥2 次触发 `SUSPENDED`（agent 故障隔离，不自动重试）
- `suspend_reason`、gate 决策记录、步骤记录

[SessionManager](../factorybot/app/infrastructure/longtask/session_manager.py#L10) 是薄封装，real 模式可扩展为 Redis-backed 会话恢复（任意副本恢复任意会话上下文）。

### 7.2 失败计数与 agent 故障隔离

```python
# L3Repo
async def increment_failure_count(self, session_id, capability) -> int:
    key = (session_id, capability)
    self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
    return self._failure_counts[key]
```

`FailureTracker` 调用：agent 成功则 `reset_failure_count`；失败则 `increment`，≥2 次 `update_status(SUSPENDED)` + `log_suspend_reason`。
**不自动重试**--agent 连续失败说明证据不足或工具异常，转人工更安全。

### 7.3 ConfirmationStore：写动作的人确认记忆

[confirmation_store.py:32](../factorybot/app/infrastructure/redis_/confirmation_store.py#L32)。每个 gate 确认签发 token（`secrets.token_hex(16)`，TTL 30min）：

```
Key1: confirm:{token_id}                     -> JSON payload（action/approved/user_id/issued_at）
Key2: confirm:session:{session_id}:{step}    -> token_id（防重复确认，幂等）
```

`action = f"{verb}:{session_id}"`（如 `activate_route:S-xxx`）绑定 session+step，校验时 `valid_for(expected_action)` 防篡改。
两个 key 都 TTL 30min。`is_already_confirmed` 提供幂等：同 session+step 已确认则返回既有 token。

**token 双重身份**：既是 L1 resume 的 `Command(resume=token)` 入参，又是下游应用服务的**幂等键**--
Agent 侧重试不会导致重复写（如重复创建隔离单）。这是 L5 记忆与写路径的交汇点。

> 落地状态：✅ 已落地并接线（[l3_orchestrator.py:140](../factorybot/app/application/l3_orchestrator.py#L140) resume 时 issue；container 装配）。

## 8. 设计哲学与关键权衡

把这五层串起来的几条硬约束与权衡：

### 8.1 模型看摘要，trace 落全文

**权衡**：降本 vs 证据完整性。`ResultCompactor` 喂模型压缩版（字段投影 + 列表截断），但 `tool_call_trace.output_payload` 存全文。
工程师 UI 读 trace 不读压缩版。**证据不空红线**得以保全。

### 8.2 版本一致性红线贯穿全栈

`route_version` 出现在：L3State 字段 → 工具入参（system prompt 强约束"查工艺必须带 route_version"）→ L4 缓存 key 哈希 → L3 trace。
MES 追溯不允许查错版本，这条红线从领域约束一路下沉到缓存层 key 设计。

### 8.3 时变性决定缓存策略

**权衡**：零成本答案复用 vs 数据漂移正确性风险。语义缓存默认关，因为 MES 答案随数据漂移。
这是**领域特性倒逼技术选型**，不是通用 Agent 的做法。通用 Agent 鼓励语义缓存降本，MES 反其道行之。

### 8.4 记忆按场景需要才上

L1 无 checkpointer（短链路跑完即出）、无 ResultCompactor 接线；L3 必带 checkpointer（长程任务要 gate 暂停 / 跨进程恢复）。
**不无脑全配**--记忆组件都有成本（MySQL I/O / 缓存失效复杂度 / 接线维护），按场景痛点启用。

### 8.5 状态外置换跨进程生存

**权衡**：每次节点执行一次 MySQL 单行 INSERT 的延迟 vs 跨进程生存。选前者换后者。
`asyncio.create_task` 的单进程故障域靠状态外置弥补--进程死了 task 丢，但 state 在 MySQL，下个 `Command(resume=...)` 从任意 pod 续跑。
checkpoint 粒度是 per-node（非 per-tool-call），开销有界。

### 8.6 并发隔离靠 thread_id

`session_id` 隔离多会话；`{session_id}_{capability}` 隔离子图；MySQL 行锁保证同 thread_id 串行写。
工具集再按 `capability + tenant scope` 隔离（[tool_node.py:63](../factorybot/app/infrastructure/ai/tool_node.py#L63)）。
三层隔离让多 session 并发不串台。

### 8.7 固定工具集 + prompt 缓存 优于 动态工具集

**权衡**：缓存命中率 vs 小工具集省 token。结论：固定工具集 + 缓存命中（~0.1x）通常比动态工具集 + 缓存未命中更省。
动态工具绑定只在工具集极大时（L3 14 上下文）才考虑。

## 9. 存储介质总表

| 存储 | 技术 | 存什么 | key | TTL | 用途 | 落地 |
|---|---|---|---|---|---|---|
| `checkpoints` | MySQL (SqlSaver) | L3State 全量快照 | (thread_id, checkpoint_ns, checkpoint_id) | 永久 | L1 工作记忆，interrupt/resume | 🔧 待启用 |
| `checkpoint_writes` | MySQL (SqlSaver) | 单 channel 写入 | (+ task_id, idx) | 永久 | checkpoint 变更跟踪 | 🔧 待启用 |
| `checkpoint_blobs` | MySQL (SqlSaver) | 大对象分片 | (+ channel, version) | 永久 | blob 存储 | 🔧 待启用 |
| `tool_call_trace` | MySQL (app) | 工具调用全文 | trace_id | 永久 | L3 证据链 | ✅ |
| `node_trace` / `l3_step` | MySQL (app) | 节点/步骤记录 | record_id | 永久 | L3 审计 | ✅ |
| `l3_session` | MySQL (app) | 会话元数据 | session_id | 永久 | L5 生命周期 | ✅ |
| `failure_count` | MySQL (app, via L3Repo) | 失败计数 | (session_id, capability) | 会话级 | L5 故障隔离 | ✅ |
| `confirm:{token_id}` | Redis | 确认令牌 payload | token_id | 30min | L5 写动作闸门 | ✅ |
| `confirm:session:{sid}:{step}` | Redis | 防重复确认 | (session_id, step) | 30min | L5 幂等 | ✅ |
| `tc:{tenant}:{tool}:{hash}` | Redis | 工具结果缓存 | tenant+tool+args+route_version | 按 tool 策略 | L4 降本 | 🔧 灰度 |
| `llm_call_log` | MySQL (app) | LLM 调用指标 | call_id | 永久 | 可观测/降本归因 | ✅ |
| `_active_tasks` | 进程内 dict | session_id→Task | session_id | 进程寿命 | 活跃任务跟踪 | ✅ |
| prompt cache | provider 侧 | system prompt+工具定义 | 内容哈希 | 5m/1h | L2 降本 | 🔧 待接线 |

## 10. 落地状态与接线清单

当前 mock（`RUN_MODE=mock`）已端到端跑通 L1 诊断 → L2 草稿 → L3 换线（3 个 gate interrupt/resume → DONE），
7 个 pytest 全绿。但**记忆系统的各层落地进度不一致**，下表是真实模式接线清单：

| 组件 | 类实现 | mock 接线 | 真实模式待办 |
|---|---|---|---|
| `MemorySaver` (checkpointer) | ✅ | ✅ L3 各场景图 | — |
| `SqlSaver` (MySQL checkpointer) | 📐 注释示意 | — | 接 `langgraph.checkpoint.mysql.AsyncSqlSaver` + 连接池 |
| `ToolCallTraceRepo` | ✅ | ✅ ToolNode | 替换为 SQLAlchemy 实现 |
| `L3Repo` / `SessionManager` | ✅ | ✅ orchestrator | 替换为 SQLAlchemy 实现 |
| `ConfirmationStore` | ✅ | ✅ orchestrator | FakeRedis → 真 Redis |
| `ResultCompactor` | ✅ | 🔧 未接线 | ToolNode 回灌前调用 `compact()` |
| `CacheControl` | ✅ | 🔧 未接线 | ObservableChatModel 外层套 `apply()` |
| `EarlyStopDetector` | ✅ | 🔧 未接线 | L1 图条件边接入 `should_stop()` |
| `ToolResultCache` | ✅ | 🔧 未接线 | ACL 客户端 `get_or_compute` 封装 |

**设计原则**：mock 验证的是"流程正确性"（记忆流转、interrupt/resume、证据链落库），
真实模式才需要"降本有效性"（压缩、缓存、prompt 缓存命中）。两者解耦，可分阶段启用。

## 11. 与既有文档的引用映射

| 本文章节 | 详细实现见 |
|---|---|
| §3 L1 工作记忆 | `Agent长程任务-实现细节深度剖析.md` §2-4（SqlSaver / interrupt-resume / 跨进程恢复） |
| §3.7 子图隔离 | `Agent长程任务-实现细节深度剖析.md` §9 |
| §4 L2 上下文窗口记忆 | `AgentToken成本优化-设计与实现方案.md` §6-7（Prompt Caching / ResultCompactor / EarlyStop） |
| §5 L3 证据链 | `Agent可观测性-设计与实现方案.md` §7（trace 落库表族 / 证据链回溯） |
| §6 L4 跨会话缓存 | `AgentToken成本优化-设计与实现方案.md` §8（工具结果缓存 / 语义缓存） |
| §7.3 ConfirmationStore | `Agent长程任务-实现细节深度剖析.md` §5（Token 签发与校验） |
| §7.2 失败计数 | `Agent长程任务-实现细节深度剖析.md` §11（agent 连续失败隔离） |

---

> **一句话总结**：factorybot Agent 的记忆是五层分层架构--L1 工作记忆（checkpointer 续跑）、
> L2 上下文窗口记忆（压缩 + prompt 缓存）、L3 证据链长期记忆（全文落库审计）、
> L4 跨会话缓存（版本化精确缓存）、L5 会话元数据 & 令牌（生命周期 + 写动作闸门）。
> 核心哲学"模型看摘要，trace 落全文"贯穿 L2↔L3；版本一致性红线贯穿 L1→L4；时变性红线决定 L4 缓存策略。
