# Agent 记忆系统 -- 设计与实现方案（Python 技术栈，三项能力共用）

> 本文档把 factorybot Agent 中所有与"记忆"相关的设计沉淀为一处。它不是新设计，而是把散落在
> `Agent长程任务-实现细节深度剖析.md`（状态持久化 / gate 挂起与续跑）、
> `AgentToken成本优化-设计与实现方案.md`（上下文压缩 / 缓存）、
> `Agent可观测性-设计与实现方案.md`（证据链落库）里的记忆相关内容，
> 按"存给谁看 / 活多久"两个维度重新组织，补齐它们各自没讲全的层间关系。
>
> **底座口径**：AgentScope 2.0（Python `>=3.12`）。`AgentState` / `SessionService`（Redis-backed；2.0.5 起由旧名改名而来，旧名已不可导入）/ `Permission` / `Toolkit` / `Offloader` / `ToolOffloadMiddleware` 为框架已核实能力名；文中 `self._xxx(...)` 一类方法签名是本项目的内部封装命名，不代表框架 API。**框架不提供 `Pipeline` / `MsgHub` / `sequential_pipeline` / `fanout_pipeline`**（1.x API，2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 模块内容已删除），也没有 Handoffs/Routing 式的移交原语与独立"Plan 模块"：本文所有顺序/并发形状一律指本项目 `flows/` 目录里的应用层 Python 代码（顺序 `await` 串联、`asyncio.gather` 并发多个各自独立的 `Agent.reply()`、应用层显式合并），与框架无关。
>
> **落地状态声明（重要）**：本文是**设计方案**，不是现状说明书。当前代码仓 `E:\factorybot` 的 `src/` 下只有 `agents/tool_test.py`、`api/{health,index,tool_test}.py`、`schemas/tool_test.py`、`tools/get_current_time.py`、`factory.py` 这套脚手架，**编排与记忆系统完全未落地**。因此文中出现的 `app/...` 一类路径（如 `app/infrastructure/persistence/repos.py`）一律是**设计目标路径（文件当前不存在）**，"已实现/已落地/跑通"等表述已统一改述为"设计目标，尚未落地"；组件名（`ToolCallTraceRepo`、`NodeTraceRepo`、`ToolResultCache` 等）是本文拟定的设计命名，不是既有代码。

## 0. 先界定：这里的"记忆"指什么

很多人一听"Agent 记忆"就去找一个叫 `Memory` 的类--在 factorybot 里**没有这个单体模块**。
记忆是一套**分层架构**，按两个正交维度切成五层：

- **存给谁看**：给 `SessionService` 看（续跑用）/ 给 LLM 看（推理用）/ 给系统看（审计用）/ 给降本用（命中复用）
- **活多久**：瞬时（单次 LLM 调用）-> 会话内（一个 session）-> 跨会话（多个 session）-> 永久（落库归档）

一句话核心哲学，贯穿全栈：

> **模型看摘要，trace 落全文。**
> 喂给 LLM 的记忆是压缩过的、易失的；留给系统审计的记忆是全量的、永久的。
> 降本不牺牲证据链完整性。

这条哲学的设计锚点定在项目拟建的 `app/infrastructure/cost/result_compactor.py` 与
`app/infrastructure/persistence/repos.py` 的 `ToolCallTrace.output_payload`（存 FULL view）——**两者均为设计目标路径，尚未落地**。

## 1. 定位与边界

### 1.1 记忆的红线（一开口就要讲）

1. **版本一致性红线**：版本锚点（`version_anchor`，route/bom/rule/asset/standard）必须贯穿 state 字段、工具入参、缓存 key、trace 全链路。MES 追溯不允许"查错版本"。
2. **时变性红线**：MES 追溯答案随新数据到达而漂移，**语义缓存默认关闭**。只对时不变场景（不可变工艺版本、稳定模板）灰度开启。
3. **证据不空红线**：每个根因假设必须引用 `trace_id`；`tool_call_trace` 永远存全文，工程师 UI 读 trace 不读压缩版。
4. **写需确认红线**：任何写动作必须经 gate 人确认 + confirmation token，token 同时是下游应用服务的幂等键。

### 1.2 与既有文档的关系（避免重复）

| 既有文档 | 讲了什么 | 本文档的补充 |
|---|---|---|
| `Agent长程任务-实现细节深度剖析.md` | `OrchestrationState`（`AgentState`）/ SessionService 状态快照 / gate 挂起-续跑 / 跨进程恢复 / Permission 确认记录 | 把它讲的"工作记忆"+"确认令牌"纳入分层视图，补它与上下文压缩、证据链的层间关系 |
| `AgentToken成本优化-设计与实现方案.md` | `Offloader` + `ToolOffloadMiddleware` / 缓存标记 Middleware / 早停 Middleware / ToolResultCache / 语义缓存 | 把它讲的"上下文窗口记忆"+"跨会话缓存"纳入分层视图，补"模型看摘要"与"trace 落全文"的双路设计 |
| `Agent可观测性-设计与实现方案.md` | tool_call_trace / node_trace 表族 / 证据链回溯 | 把它讲的"证据链长期记忆"纳入分层视图，补 trace 与 LLM 压缩版的对应关系 |

本文档不重复上述文档已详述的实现细节，而是在"分层"视角下把它们串成一张图，并补齐设计哲学与权衡。

### 1.3 不覆盖

- 不覆盖三项能力各自的业务流程（见各自实现方案）。
- 不覆盖可观测性的指标体系细节（见可观测性文档）。
- 不覆盖写路径的 ACL 客户端实现（见长程任务文档 §7）。

## 2. 记忆分层模型

### 2.1 五层模型总表

| 层 | 名称 | 核心组件 | 存储介质 | 存什么 | 寿命 | 服务对象 | 落地状态 |
|---|---|---|---|---|---|---|---|
| M1 | 工作记忆 | `SessionService` + `OrchestrationState`（`AgentState`） | Redis-backed Session store | 显式状态快照（类型化字段值） | 会话级，可跨进程恢复 | 编排续跑（gate 挂起 -> 确认事件续跑） | 📐 设计目标，尚未落地 |
| M2 | 上下文窗口记忆 | `Offloader` / `ToolOffloadMiddleware` / 缓存标记与早停 Middleware | 内存（请求级） | 喂模型的消息压缩、prompt 缓存标记 | 单次 LLM 调用 ~ 5min/1h | LLM | 📐 设计目标，尚未落地 |
| M3 | 证据链长期记忆 | `ToolCallTraceRepo` / `NodeTraceRepo` / `OrchestrationStep` | MySQL 平铺表 | 工具调用全文、节点执行记录 | 永久 | 工程师 UI / 审计 | 📐 设计目标，尚未落地 |
| 自治 | 跨会话缓存 | `ToolResultCache` | Redis | 稳定工具结果的版本化缓存 | 按 tool 策略 TTL | 降本（命中省一次 ACL+回灌） | 📐 设计目标，尚未落地（语义缓存默认关闭） |
| L5 | 会话元数据 & 确认记录 | `Repo`（会话台账）/ Permission 确认记录存储 | MySQL + Redis | 会话生命周期、失败计数、gate 确认令牌 | 会话级 ~ 30min | 编排器 / 写动作闸门 | 📐 设计目标，尚未落地 |

> 落地状态图例：📐 设计目标，尚未落地（本文全部条目均为此状态；代码仓现状见文首"落地状态声明"）。
> **命名提醒**：M1 的 `SessionService` 是 AgentScope 2.0 的会话恢复能力（框架侧，Redis-backed，`app/_service/_session.py`；**2.0.5 起由旧名改名而来，按旧名写不可导入**）；L5 里的会话台账是本项目 MySQL 业务表（`agent_session` / 失败计数），两者职责不同不可混称。本项目**不自研**会话封装类，恢复语义一律由框架 `SessionService` 承担。

### 2.2 分层架构图

```
                        ┌─────────────────────────────────────────────┐
                        │              LLM (ReAct 循环)                │
                        └─────────────────────────────────────────────┘
                              ▲                        ▲
              压缩后摘要回灌 │                        │ prompt 缓存标记
              (M2 Offloader)                (M2 缓存标记 Middleware)
                              │                        │
   ┌──────────────────────────┴────────────────────────┴──────────────┐
   │  M2 上下文窗口记忆（瞬时，请求级）                                  │
   │  Offloader · ToolOffloadMiddleware · 缓存/早停 Middleware · 步数预算│
   └──────────────────────────┬───────────────────────────────────────┘
                              │ 工具调用
   ┌──────────────────────────┴───────────────────────────────────────┐
   │  M3 证据链长期记忆（永久）  tool_call_trace / node_trace / agent_step │
   │  ★ output_payload 存 FULL view（压缩前的全文）                    │
   └──────────────────────────┬───────────────────────────────────────┘
                              │ 同步落 trace
   ┌──────────────────────────┴───────────────────────────────────────┐
   │  自治 跨会话缓存（Redis）  ToolResultCache  key=tc:{tenant}:{tool}:{hash(args+version_anchor)} │
   └──────────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────────┐
   │  M1 工作记忆（会话级，可跨进程）                                    │
   │  AgentScope SessionService（Redis-backed） 恢复键 = session_id     │
   │  存：AgentState 显式状态快照（类型化字段值）                        │
   │  gate 拦截挂起 ──确认事件──> 任意副本按 session_id 续跑            │
   └──────────────────────────────────────────────────────────────────┘
                              │
   ┌──────────────────────────┴───────────────────────────────────────┐
   │  L5 会话元数据 & 确认记录                                          │
   │  Repo(会话状态机/失败计数) · Permission 确认记录(Redis, 30min TTL)│
   └──────────────────────────────────────────────────────────────────┘
```

### 2.3 数据流：一次工具调用如何流经各层

以 诊断的一次工具调用为例（工具执行封装在 Toolkit 内，本项目的工具执行钩子 `ToolExecHook` 拟建于 `app/infrastructure/ai/tool_exec_hook.py`，设计目标路径）：

```
Agent（内置 ReAct）产出 pending_tool_calls
   │
   ▼
工具执行节点（Toolkit / Tool）
   │
   ├─[权限] Permission 策略 + capability + tenant scope 过滤 ─否─> save_denied (M3) ─> 返回 error 消息
   │
   ├─[执行] descriptor.handler(**args, tenant) -> view (FULL)
   │      │
   │      └─[自治] (设计中) ToolResultCache.get_or_compute 命中则直接返回 view
   │
   ├─[M3 落全文] trace_repo.save_ok(args, view=FULL_view, ...) -> trace_id
   │
   ├─[M2 压缩/卸载] (设计目标) Offloader / ToolOffloadMiddleware -> compacted
   │      ★ 分阶段：先模型看 trace_id + 全文；Offloader 接线后走压缩/卸载超大结果
   │
   └─[回灌模型] tool 消息 {trace_id, data: compacted_or_full}
```

关键点：**trace 落的是 FULL view，模型看的是 compacted view**--两条路，同一份数据，不同保真度。
落地时先走"模型直接看全文"的简化路径（这是设计上的分阶段策略，不是既有代码行为），接线 `Offloader` / `ToolOffloadMiddleware` 后才走压缩路径。

## 3. M1 工作记忆：SessionService + AgentState（OrchestrationState）

这是大家通常说的"Agent 记忆"--**编排跑到一半挂起，靠它续跑**。

### 3.1 状态 schema：OrchestrationState（显式类型化字段）

本项目拟在 `app/domain/orchestration.py`（设计目标路径）定义主编排的状态 schema（`AgentState` 的显式类型化字段）。
每个字段都是可校验、可序列化的显式声明（Pydantic 模型 / Content Block 路线），节点直接取字段。关键字段分组：

```python
class OrchestrationState(BaseModel):      # AgentState 的业务投影（显式类型化）
    # 会话标识
    session_id: str = ""
    scenario: str = ""

    # 租户上下文（结构化字段，序列化由 Pydantic 负责）
    tenant: TenantContext | None = None

    # 业务上下文
    work_order_id: str | None = None
    target_route_id: str | None = None
    target_route_version: str | None = None   # ★ 版本一致性红线

    # 会话状态（asyncio.gather 并发分支可能同写，汇合处按 last-wins 归并——本项目应用层 barrier 的归并规则）
    status: str = "PLANNING"
    current_step: str = ""

    # 各步骤结果 + gate 决策
    first_article_result: dict | None = None
    gate_first_article: str | None = None
    action_card: dict | None = None
    ...

    # agent 通信
    pending_tool_calls: list[dict] = []
    tool_results: list[dict] = []

    # 写动作透传：gate 确认后把确认记录存此，write 步骤取用
    confirmation: dict | None = None
```

并发分支同写一个字段时在汇合（barrier）处取后者、`None` 不覆盖有效值--这是本项目在应用层合并时自定的归并规则，不依赖任何引擎/框架的状态归并原语（框架无共享状态图，也无广播原语）。
四类场景（换线 / 故障复产 / 客诉 8D / 工艺变更）**共用一套 schema，各场景只装配自己用到的子集**--DDD 共享内核 + 限界上下文装配的落地。

### 3.2 持久化：恢复键 = session_id

会话状态的持久化由 AgentScope 2.0 的 `SessionService` 承载（随 FastAPI Agent Service（`create_app`）的 lifespan 装配），存储后端由配置决定：

```python
# app/infrastructure/session/  （本项目接线层，示意；构造签名以目标版本官方 API 为准）
# 会话服务：agentscope 的 SessionService（app/_service/_session.py，Redis-backed storage）
# 业务侧只见 session_id，不感知存储后端；恢复语义不由本项目自己实现
session_service = build_session_service(settings)   # 存储后端由配置决定，业务侧只见 session_id
```

**关键映射**：恢复键 = `session_id`（本项目在编排服务装配处建立该映射，设计目标）。
`SessionService` 按 `session_id` 隔离 `AgentState`；同一会话的状态更新串行落盘（单写者），不同会话互不影响。

> 落地状态：**设计目标，尚未落地**——代码仓尚无会话层实现，Redis-backed storage 接线属于真实部署阶段的待建工作。
> 设计约束："工作记忆"必须是可跨进程恢复的（跨进程恢复依赖 Redis-backed storage 接线），gate 挂起-确认续跑走 `event.RequireUserConfirmEvent`。

### 3.3 MySQL 侧不再承担会话状态表

原方案在 MySQL 建三张会话状态表；本架构下 **MySQL 不再有会话状态/快照表**，只留业务可观测表：

| 表 | 主键 | 存什么 |
|---|---|---|
| `agent_session` | `session_id` | 会话台账：状态机、场景、租户、挂起原因 |
| `agent_step_record` / `node_trace` | `(session_id, step)` | 每步执行记录（CODE/AGENT 性质、gate 决策、trace 引用） |
| `tool_call_trace` | `trace_id` | 工具调用全文（证据链） |

会话状态快照本身在 Redis-backed Session store；如需长期留痕，可把 `AgentState` **可选归档**到 MySQL 审计表（只读，不参与续跑）。

### 3.4 状态快照序列与"存了什么"

每次状态更新带 parent 指针，形成可追溯的快照序列（类似 git commit 链）：

```
snapshot_1 (parent=null)   <- plan 执行完
snapshot_2 (parent=snap_1) <- first_article 执行完
snapshot_3 (parent=snap_2) <- gate_first_article 挂起待人确认处
snapshot_4 (parent=snap_3) <- process_switch 执行完（确认后续跑）
```

存的是 **state 本身**（各字段的当前值）。注意：推给责任人的**动作卡以事件形式流出**，不进状态快照--快照只在需要"这步要落库"时留一份意图字段；
续跑时从 Session store 取最新 `AgentState`，从 gate 拦截点的下一步继续。
编排能力不用回退（gate 确认后不撤销），序列只服务审计与复盘。

### 3.5 gate 挂起 / 续跑协议

gate 步骤挂起（本项目在 `EXPLORE` / 自定义 deny 规则之上组合出的写闸门策略拦截写工具执行，接线点拟建于 `app/application/orchestration_service.py`，设计目标路径）：

```python
async def await_confirmation(self, session_id, step, card):
    await self._dispatcher.push(card)    # 1. 经事件系统推动作卡
    confirmation = await self._permission.await_hitl(session_id, step, card)  # 2. 挂起等待确认事件
    # —— 协程让出，AgentState 已在 Session store，直到确认事件投来 ——
    if not confirmation.valid_for(card.writes_via_action()):
        return "REJECT"
    return "PASS" if confirmation.approved else "REJECT"
```

Permission gate 做四件事：① 先把 `AgentState` 落 Session store ② 拦住写工具执行（闸门在每次执行，不在提示词层）③ 会话标 `GATE_WAITING` ④ 流出待确认事件。

人确认后续跑（本项目在编排服务里封装 `resume()`，方法名是本项目封装，非框架 API）：

```python
state = await self._sessions.load_state(session_id)      # 取回 AgentState 与待确认动作卡
card = _extract_pending_card(state)
token = await self._permission.record_confirmation(session_id, step, approved, user_id,
                                                    action=card.writes_via_action())
await self._events.publish(GateConfirmed(session_id, step, token_id=token.id))  # 确认事件 -> 续跑
```

续跑内部：① 按 `session_id` 从 Session store 取回 `AgentState` ② 定位 `GATE_WAITING` 的那一步 ③ 把确认记录交回 gate 判定 ④ 从拦截点的下一步继续。

### 3.6 跨进程恢复（工作记忆真正发挥价值之处）

```
副本 A (pod-1): 编排 run(initial_state, session_id=S-001)
  -> plan -> first_article -> gate_first_article -> Permission gate 拦截
  -> AgentState 落 Redis-backed Session store -> 本次驱动结束（不占执行槽）
  ---- pod-1 OOM Killed ----
副本 B (pod-2): 收到 POST /confirm，确认事件进入事件系统
  -> 按 session_id=S-001 从 Session store 取回 AgentState
  -> 反序列化 state（停在 gate_first_article）
  -> 确认记录交回 gate 判定 -> 继续 process_switch -> ...
```

**内存里的后台任务会丢，但 state 不丢**。续跑不需要"同一个在途调用被唤醒"这种前提：后台任务由 `BackgroundTaskManager` 托管、状态外置在 Session store，确认事件一到，任意副本都能接手--这才是跨进程恢复的真正靠山。

### 3.7 能力节点的子会话隔离

主编排调用能力 Agent 时使用独立的子会话键（长程任务文档 §9.2）：

```python
result = await agent.reply(
    state,
    session_id=f"{state.session_id}:{capability}",   # 子会话键
)
```

`{session_id}:{capability}` 使主编排状态与能力节点的推理状态在 Session store 分开存放，互不覆盖。
这是"多 session 并发 + 能力节点独立"的隔离手段（调用时机由本项目 `flows/` 里的应用层代码决定——顺序 `await` 串联、`asyncio.gather` 并发多个各自独立的 `Agent.reply()`；框架无移交原语，隔离一律靠会话键）。

### 3.8 诊断能力为何不注册会话恢复

诊断能力是短链路 ReAct（步数与工具调用预算 = 20），一次 `reply` 跑完即出报告，**不需要中途暂停**，所以不注册 Session、不做工作记忆持久化。

> **记忆按场景需要才上，不是无脑全配。** 诊断能力无 Session 持久化 / 无 Offloader 接线；
> 编排能力必接 `SessionService`（长程任务要 gate 挂起 / 跨进程恢复）。这是分层记忆的"按需启用"原则。

## 4. M2 上下文窗口记忆：喂模型的瞬时记忆

这层解决"ReAct 每步重发 system prompt + 历史，token 二次方放大"问题。属于"给 LLM 看的瞬时记忆"。

### 4.1 问题：token 花在哪

```
总输入 token ≈ Σ_{step=1..N} [ system_prompt + tool_definitions + history_{1..step-1} ]
```

三个观察：① `system_prompt + tool_definitions` 每步重发，最大最浪费；② `history` 累积，step k 带 k-1 个历史结果，二次方放大；③ N（步数）是乘子。

### 4.2 Offloader / ToolOffloadMiddleware：模型看摘要，trace 落全文

上下文压缩与超大工具结果卸载由 AgentScope 2.0 的 `Offloader` + `ToolOffloadMiddleware` 承载（框架内生），本项目的字段白名单策略拟建于 `app/infrastructure/cost/result_compactor.py`（设计目标路径）。工具结果回灌前做三件事：

```python
FIELD_WHITELIST: dict[str, list[str]] = {
    "query_pass_records": ["sn", "work_order_id", "station_id", "equipment_id",
                           "route_version", "decision", "blocking_reason"],
    "query_test_results": ["test_id", "station_id", "test_type", "raw_verdict"],
    "trace_lookup": ["serial_no", "evidence_set_id", "version", "version_kind", "version_ref_id"],
}
LIST_TRUNCATE = 5  # 列表最多保留前 5 项

class OffloadCompactPolicy:          # 本项目的压缩策略，由 Offloader / ToolOffloadMiddleware 调用
    def compact(self, tool_name: str, view: Any) -> dict:
        # 1. 字段裁剪：按白名单只留关键字段
        # 2. 列表截断：超过 5 项截断
        # 3. 截断标注：_truncated / _omitted_count，让模型知道数据不全
        ...
        out[f"_{k}_truncated"] = True
        out["_omitted_count"] = omitted
```

**截断标注是关键**：让模型知道"这是截断后的 5 条，不是全部"，避免它把局部当全局下结论。

**红线**：`tool_call_trace.output_payload` 存的是压缩前的 FULL view（FULL view = 证据链全文，非压缩）。
压缩只影响"喂模型"路径，证据链完整性不牺牲。

> 落地状态：**策略为设计目标，尚未落地**——本项目尚无工具执行节点代码，`Offloader` 压缩/卸载路径属接线期工作。
> 分阶段理由：先用确定性假模型（设计阶段的联调手段，非既有代码）把全文直接喂模型，此时不烧真实 token；真实 LLM 接入后再启用压缩/卸载路径。

### 4.3 缓存标记 Middleware：prompt 缓存标记

`CacheControl`（本项目拟建的自定义成本 Middleware）的角色：把稳定的 system prompt / 工具定义打 provider 的 `cache_control` 标记。

```python
@dataclass
class CacheControl:                  # provider 侧缓存标记的数据形状（非框架 API）
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

> 落地状态：**设计目标，尚未落地**——缓存标记 Middleware 与自定义 Middleware 链均未接线（LLM 观测走 `TracingMiddleware` + 自定义观测 Middleware，见可观测性文档）。
> 真实 LLM 接入时把缓存标记插在模型调用的 Middleware 链上，与观测埋点同一处生效。

### 4.4 早停 Middleware：避免烧步数

自定义早停 Middleware（本项目拟建于 `app/infrastructure/cost/early_stop.py`，设计目标路径）：检测冗余探索，提前收口转人工。

```python
class EarlyStopDetector:             # 本项目自定义的早停 Middleware 判定体
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

- **步数与工具调用预算上限**：诊断能力=20 / 编排能力=40 硬上限，兜底封顶单会话步数（在能力装配处配置，见可观测性文档的 `agent_step_budget_hit_total` 指标口径）
- 并行工具调用：Toolkit 的工具执行节点用 `asyncio.gather` 并发执行多 tool_call，减少回灌次数（降 N）

## 5. M3 证据链长期记忆：给系统留的全量记忆

这层是 MES **追溯性 (Traceability)** 硬约束的落地--所有工具调用、节点执行必须可回溯。对应可观测性文档 §7。

### 5.1 ToolCallTrace（工具调用平铺表）

本项目拟建于 `app/infrastructure/persistence/`（`repos.py` + `models.py`，设计目标路径）：

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

> 落地状态：**设计目标，尚未落地**——每次工具调用落一行 `tool_call_trace` 由工具执行节点（Toolkit 回调处）实现，代码仓尚无该节点。

### 5.2 NodeTrace / OrchestrationStep / DraftTrace

- `NodeTraceRepo`：flow 步骤（CODE / AGENT 性质）执行记录，含 `agent_hypothesis` / `agent_confidence` / `tool_call_traces` 反向引用
- `OrchestrationStep`：步骤级记录，状态 `PENDING|RUNNING|GATE_WAITING|CONFIRMED|FAILED`
- `DraftTraceRepo`：草稿归档（草稿能力只落草稿不落库）

以上三者的落库位置为 MySQL（`app/infrastructure/persistence/models.py` 拟建，设计目标路径），表结构对齐可观测文档 §7.1 / 长程任务 §2.2——**均为设计目标，尚未落地**。

### 5.3 与工程师 UI 回溯的关系

证据链 = span 树的业务投影（可观测性文档 §4.5）。工程师 UI 读的是 `tool_call_trace.output_payload`（全文），
不是 M2 压缩版。这就是"模型看摘要，trace 落全文"的后半句--**LLM 会幻觉，但 trace 永远是全量真相**。

## 6. 自治 跨会话缓存：ToolResultCache

本项目拟建于 `app/infrastructure/redis_/tool_cache.py`（设计目标路径）。Redis 精确缓存（**非语义缓存**）：

```python
class ToolResultCache:
    def _key(self, tenant_id, tool_name, args, version_anchor) -> str:
        payload = json.dumps({"t": tenant_id, "a": args, "v": version_anchor.model_dump_json() if version_anchor else ""}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return f"tc:{tenant_id}:{tool_name}:{digest}"

    async def get(self, tenant_id, tool_name, args, version_anchor=None): ...
    async def set(self, tenant_id, tool_name, args, value, version_anchor=None, ttl=None): ...
```

### 6.1 版本化精确缓存

- `version_anchor` **必须进 key 的哈希 payload**--版本一致性红线延伸到缓存层
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
> 落地状态：**设计目标，尚未落地**——`ToolResultCache` 类与 ACL 侧 `get_or_compute` 封装均待建；启用时按场景灰度。

## 7. L5 会话元数据 & 确认记录

### 7.1 Repo / 会话台账：会话生命周期记忆

会话台账由本项目的 `Repo`（拟建 `app/infrastructure/persistence/repos.py`，设计目标路径）承载（MySQL 业务台账，与 M1 的框架 `SessionService` 职责不同）：
- 会话状态机 `PLANNING -> RUNNING -> SUSPENDED/DONE/FAILED`
- **失败计数** `failure_count: dict[str,int]`，按 `(session_id, capability)` 追踪 agent 连续失败，≥2 次触发 `SUSPENDED`（agent 故障隔离，不自动重试）
- `suspend_reason`、gate 决策记录、步骤记录

会话状态的**恢复**能力由框架 `SessionService`（Redis-backed）承担；本项目**不自研**会话封装类，业务侧只保留"按 `session_id` 取回台账"这一层，续跑语义不再自己实现。

### 7.2 失败计数与 agent 故障隔离

```python
# Repo
async def increment_failure_count(self, session_id, capability) -> int:
    key = (session_id, capability)
    self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
    return self._failure_counts[key]
```

`FailureTracker` 调用：agent 成功则 `reset_failure_count`；失败则 `increment`，≥2 次 `update_status(SUSPENDED)` + `log_suspend_reason`。
**不自动重试**--agent 连续失败说明证据不足或工具异常，转人工更安全。

### 7.3 Permission 确认记录：写动作的人确认记忆

ConfirmationStore 的语义已并入 **Permission 系统**，落库形态只是 Permission 在 Redis 里的确认记录存储（本项目拟建，设计目标）。每个 gate 确认签发 token（`secrets.token_hex(16)`，TTL 30min）：

```
Key1: confirm:{token_id}                     -> JSON payload（action/approved/user_id/issued_at）
Key2: confirm:session:{session_id}:{step}    -> token_id（防重复确认，幂等）
```

`action = f"{verb}:{session_id}"`（如 `activate_route:S-xxx`）绑定 session+step，校验时 `valid_for(expected_action)` 防篡改。
两个 key 都 TTL 30min。`is_already_confirmed` 提供幂等：同 session+step 已确认则返回既有 token。

**token 双重身份**：既是确认事件触发续跑时交回 gate 判定的**凭据**（Permission 只在此凭据有效时放行写工具执行），又是下游应用服务的**幂等键**--
Agent 侧重试不会导致重复写（如重复创建隔离单）。这是 L5 记忆与写路径的交汇点。

> 落地状态：**设计目标，尚未落地**——确认令牌的签发与校验在 gate 判定处实现（真边界在工具网关的服务端二次鉴权 + 一次性确认令牌，`Permission` 只是叠加层，见 ADR-04）。

## 8. 设计哲学与关键权衡

把这五层串起来的几条硬约束与权衡：

### 8.1 模型看摘要，trace 落全文

**权衡**：降本 vs 证据完整性。`Offloader` / `ToolOffloadMiddleware` 喂模型压缩版（字段投影 + 列表截断），但 `tool_call_trace.output_payload` 存全文。
工程师 UI 读 trace 不读压缩版。**证据不空红线**得以保全。

### 8.2 版本一致性红线贯穿全栈

`route_version` 出现在：OrchestrationState 字段 -> 工具入参（system prompt 强约束"查工艺必须带 route_version"）-> M4 缓存 key 哈希 -> 编排能力 trace。
MES 追溯不允许查错版本，这条红线从领域约束一路下沉到缓存层 key 设计。

### 8.3 时变性决定缓存策略

**权衡**：零成本答案复用 vs 数据漂移正确性风险。语义缓存默认关，因为 MES 答案随数据漂移。
这是**领域特性倒逼技术选型**，不是通用 Agent 的做法。通用 Agent 鼓励语义缓存降本，MES 反其道行之。

### 8.4 记忆按场景需要才上

诊断能力不注册会话恢复（短链路跑完即出）、不接 `Offloader`；编排能力必接 `SessionService`（长程任务要 gate 挂起 / 跨进程恢复）。
**不无脑全配**--记忆组件都有成本（存储 I/O / 缓存失效复杂度 / 接线维护），按场景痛点启用。

> 顺带把口径钉清楚：长程任务真正需要的是**外部化状态 + 上下文压缩**这两件事，多 Agent 只是可选的实现方式之一，不是必要条件。本项目的分层记忆就是围绕这两件事建的。

### 8.5 状态外置换跨进程生存

**权衡**：每次步骤执行一次状态写入（Redis-backed Session store）的延迟 vs 跨进程生存。选前者换后者。
后台任务的单副本故障域靠状态外置弥补--进程死了任务丢，但 `AgentState` 在 Session store，确认事件一到就按 `session_id` 从任意副本续跑。
状态快照粒度是 per-step（非 per-tool-call），开销有界。

### 8.6 并发隔离靠 session_id

`session_id` 隔离多会话；`{session_id}:{capability}` 隔离能力节点；同一会话的状态更新由 `SessionService` 串行落盘（单写者），不靠数据库行锁。
工具集再按 `capability + tenant scope` 收敛可见性（Toolkit 装配处按能力注入）--但**安全边界建在工具网关/执行层**：`PermissionEngine` + `PermissionRule` 对每一次工具执行做策略与 scope 校验（框架 Permission 是叠加层，不是唯一边界，见 ADR-04）。仅靠"这个工具只挂给了某个能力"不构成隔离。
三层设计（会话键隔离 + 可见性收敛 + 执行层 Permission）让多 session 并发不串台。

### 8.7 固定工具集 + prompt 缓存 优于 动态工具集

**权衡**：缓存命中率 vs 小工具集省 token。结论：固定工具集 + 缓存命中（~0.1x）通常比动态工具集 + 缓存未命中更省。
动态工具绑定只在工具集极大时（编排能力覆盖 14 个上下文）才考虑。

## 9. 存储介质总表

| 存储 | 技术 | 存什么 | key | TTL | 用途 | 落地 |
|---|---|---|---|---|---|---|
| 会话状态快照（AgentState） | Redis-backed Session store（`SessionService`） | 编排状态全量快照 + 待确认上下文 | `session_id`（能力子会话 `{session_id}:{capability}`） | 会话级 + TTL 策略 | M1 工作记忆，gate 挂起 -> 确认事件续跑 | 📐 设计目标 |
| `agent_state_archive` | MySQL（**可选**） | 状态快照归档留痕 | session_id + 快照序号 | 永久 | 审计复盘，不参与续跑 | 📐 设计目标 |
| `tool_call_trace` | MySQL (app) | 工具调用全文 | trace_id | 永久 | M3 证据链 | 📐 设计目标 |
| `node_trace`（flow 步骤记录）/ `agent_step` | MySQL (app) | flow 步骤 / 委托执行记录 | record_id | 永久 | 编排能力审计 | 📐 设计目标 |
| `agent_session` | MySQL (app) | 会话元数据 | session_id | 永久 | L5 生命周期 | 📐 设计目标 |
| `failure_count` | MySQL (app, via Repo) | 失败计数 | (session_id, capability) | 会话级 | L5 故障隔离 | 📐 设计目标 |
| `confirm:{token_id}` | Redis | 确认令牌 payload | token_id | 30min | L5 写动作闸门 | 📐 设计目标 |
| `confirm:session:{sid}:{step}` | Redis | 防重复确认 | (session_id, step) | 30min | L5 幂等 | 📐 设计目标 |
| `tc:{tenant}:{tool}:{hash}` | Redis | 工具结果缓存 | tenant+tool+args+version_anchor | 按 tool 策略 | 自治 降本 | 📐 设计目标 |
| `llm_call_log` | MySQL (app) | LLM 调用指标 | call_id | 永久 | 可观测/降本归因 | 📐 设计目标 |
| `BackgroundTaskManager` 活跃任务登记表 | 框架托管（`app/_manager/`） | session_id->后台任务 | session_id | 进程寿命 | 活跃任务跟踪 | 📐 设计目标 |
| prompt cache | provider 侧 | system prompt+工具定义 | 内容哈希 | 5m/1h | 上下文窗口层降本 | 📐 设计目标 |

## 10. 落地状态与接线清单

**现状（核实过）**：代码仓 `E:\factorybot` 的 `src/` 下只有 `agents/tool_test.py`、`api/{health,index,tool_test}.py`、`schemas/tool_test.py`、`tools/get_current_time.py`、`factory.py` 这套脚手架，**诊断 / 草稿 / 编排三项能力与记忆系统的各层全部未落地**（没有 mock 端到端流程，也没有相应 pytest）。本节因此不是"接线进度表"，而是**建设清单**：

| 组件 | 设计状态 | 落地待办 |
|---|---|---|
| `SessionService` + `AgentState`（M1 工作记忆） | 📐 设计目标 | 随 `create_app` 的 lifespan 装配 Redis-backed storage；恢复语义用框架能力，不自研 |
| MySQL 状态快照归档（可选） | 📐 设计目标 | 落 `agent_state_archive` 归档表，不参与续跑 |
| `ToolCallTraceRepo` | 📐 设计目标 | Toolkit 工具执行回调处每次落一行（SQLAlchemy） |
| `Repo`（会话台账） | 📐 设计目标 | `agent_session` / `failure_count` 落 MySQL |
| Permission 确认记录存储 | 📐 设计目标 | `PermissionEngine` 叠加层 + 网关一次性确认令牌；Redis 存确认记录 |
| `Offloader` / `ToolOffloadMiddleware` 压缩策略 | 📐 设计目标 | 工具结果回灌前走压缩/卸载 |
| 缓存标记 Middleware | 📐 设计目标 | 插在模型调用的 Middleware 链外层 |
| 早停 Middleware | 📐 设计目标 | 接在诊断 Agent 的收敛判定处 |
| `ToolResultCache` | 📐 设计目标 | ACL 客户端 `get_or_compute` 封装 |

> 上表的 `SessionService` / `Offloader` / `ToolOffloadMiddleware` / `PermissionEngine` 是 **AgentScope 2.0 已核实的框架能力名**；
> 表内 `Repo` / `ToolResultCache` / `ToolCallTraceRepo` 一类是本项目的自研封装与台账仓储**命名（尚未创建）**，不是框架 API。

**设计原则**：先用最小端到端链路验证"流程正确性"（记忆流转、gate 挂起/确认续跑、证据链落库），
再启用"降本有效性"（压缩、缓存、prompt 缓存命中）。两者解耦，可分阶段启用。

## 11. 与既有文档的引用映射

| 本文章节 | 详细实现见 |
|---|---|
| §3 M1 工作记忆 | `Agent长程任务-实现细节深度剖析.md` §2-4（`SessionService` + `AgentState` / gate 挂起-确认续跑 / 跨进程恢复） |
| §3.7 能力节点子会话隔离 | `Agent长程任务-实现细节深度剖析.md` §9 |
| §4 M2 上下文窗口记忆 | `AgentToken成本优化-设计与实现方案.md` §6-7（Prompt Caching / `Offloader` 压缩 / 早停 Middleware） |
| §5 M3 证据链 | `Agent可观测性-设计与实现方案.md` §7（trace 落库表族 / 证据链回溯） |
| §6 自治 跨会话缓存 | `AgentToken成本优化-设计与实现方案.md` §8（工具结果缓存 / 语义缓存） |
| §7.3 Permission 确认记录 | `Agent长程任务-实现细节深度剖析.md` §5（Token 签发与校验） |
| §7.2 失败计数 | `Agent长程任务-实现细节深度剖析.md` §11（agent 连续失败隔离） |

---

> **一句话总结**：factorybot Agent 的记忆是五层分层架构--M1 工作记忆（`SessionService` + `AgentState` 续跑）、
> M2 上下文窗口记忆（压缩 + prompt 缓存）、M3 证据链长期记忆（全文落库审计）、
> 自治 跨会话缓存（版本化精确缓存）、L5 会话元数据 & 令牌（生命周期 + 写动作闸门）。
> 核心哲学"模型看摘要，trace 落全文"贯穿 M2↔M3；版本一致性红线贯穿 M1->M4；时变性红线决定 M4 缓存策略。
