# Agent 记忆系统 -- 设计与实现方案（Python 技术栈，三项能力共用）

> 本文档把 factorybot Agent 中所有与"记忆"相关的设计沉淀为一处。它不是新设计，而是把散落在
> `整体技术选型与模块划分.md` §3.5.3（人工确认与写闸门）、
> `AgentToken成本优化-设计与实现方案.md`（上下文压缩 / 缓存）、
> `Agent可观测性-设计与实现方案.md`（证据链落库）里的记忆相关内容，
> 按"存给谁看 / 活多久"两个维度重新组织，补齐它们各自没讲全的层间关系。
>
> **底座口径**：AgentScope 2.0（Python `>=3.12`）。`AgentState` / `SessionService`（组合 storage 与 message bus 的门面，**不绑定具体后端**；2.0.5 起由旧名改名而来，旧名已不可导入）/ `Permission` / `Toolkit` / `Offloader` / `ToolOffloadMiddleware` 为框架已核实能力名；本项目 Storage 选型为 `AsyncSQLAlchemyStorage` 落 MySQL（见 §3.2 与 ADR-07）。文中 `self._xxx(...)` 一类方法签名是本项目的内部封装命名，不代表框架 API。**框架不提供 `Pipeline` / `MsgHub` / `sequential_pipeline` / `fanout_pipeline`**（1.x API，2.0.0 起 `agentscope/pipeline`、`agentscope/msghub` 模块内容已删除），也没有 Handoffs/Routing 式的移交原语与独立"Plan 模块"：本文所有顺序/并发形状一律指本项目 `flows/` 目录里的应用层 Python 代码（顺序 `await` 串联、`asyncio.gather` 并发多个各自独立的 `Agent.reply()`、应用层显式合并），与框架无关。
>
> **落地状态声明（重要）**：本文是**设计方案**，不是现状说明书。当前代码仓 `E:\factorybot` 的 `src/` 下只有 `agents/tool_test.py`、`api/{health,index,tool_test}.py`、`schemas/tool_test.py`、`tools/get_current_time.py`、`factory.py` 这套脚手架，**编排与记忆系统完全未落地**。因此文中出现的 `app/...` 一类路径（如 `app/infrastructure/persistence/repos.py`）一律是**设计目标路径（文件当前不存在）**，"已实现/已落地/跑通"等表述已统一改述为"设计目标，尚未落地"；组件名（`ToolCallTraceRepo`、`NodeTraceRepo`、`ToolResultCache` 等）是本文拟定的设计命名，不是既有代码。

## 0. 先界定：这里的"记忆"指什么

很多人一听"Agent 记忆"就去找一个叫 `Memory` 的类--在 factorybot 里**没有这个单体模块**。
记忆是一套**分层架构**，按两个正交维度切成五层：

- **存给谁看**：给 `SessionService` 看（会话内上下文与多轮记忆）/ 给 LLM 看（推理用）/ 给系统看（审计用）/ 给降本用（命中复用）
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
| `整体技术选型与模块划分.md` §3.5.3、`FactoryAgent/FactoryAgent-流程编排能力.md` §4.2 | `OrchestrationState`（`AgentState`）/ SessionService 会话内上下文 / gate 分段与确认回调 / 一次性确认令牌 | 把它讲的"工作记忆"+"确认令牌"纳入分层视图，补它与上下文压缩、证据链的层间关系 |
| `AgentToken成本优化-设计与实现方案.md` | `Offloader` + `ToolOffloadMiddleware` / 缓存标记 Middleware / 早停 Middleware / ToolResultCache / 语义缓存 | 把它讲的"上下文窗口记忆"+"跨会话缓存"纳入分层视图，补"模型看摘要"与"trace 落全文"的双路设计 |
| `Agent可观测性-设计与实现方案.md` | tool_call_trace / node_trace 表族 / 证据链回溯 | 把它讲的"证据链长期记忆"纳入分层视图，补 trace 与 LLM 压缩版的对应关系 |

本文档不重复上述文档已详述的实现细节，而是在"分层"视角下把它们串成一张图，并补齐设计哲学与权衡。

### 1.3 不覆盖

- 不覆盖三项能力各自的业务流程（见各自实现方案）。
- 不覆盖可观测性的指标体系细节（见可观测性文档）。
- 不覆盖写路径的 ACL 客户端实现（见 `FactoryAgent/FactoryAgent-流程编排能力.md` §4.3）。

## 2. 记忆分层模型

### 2.1 五层模型总表

| 层 | 名称 | 核心组件 | 存储介质 | 存什么 | 寿命 | 服务对象 | 落地状态 |
|---|---|---|---|---|---|---|---|
| M1 | 工作记忆 | `SessionService` + `OrchestrationState`（`AgentState`） | 框架 `Storage` 后端（本项目选 `AsyncSQLAlchemyStorage` 落 **MySQL**；框架默认实现为 `RedisStorage`） | 会话内上下文字段值（类型化）+ **消息历史** | 永久（历史长期留存，支撑关掉页面后隔天接着问） | 多轮对话与会话隔离；**不服务跨闸门续跑**：编排到 gate 就产待办并 return，下一段重查当下事实 | 📐 设计目标，尚未落地 |
| M2 | 上下文窗口记忆 | `Offloader` / `ToolOffloadMiddleware` / 缓存标记与早停 Middleware | 内存（请求级） | 喂模型的消息压缩、prompt 缓存标记 | 单次 LLM 调用 ~ 5min/1h | LLM | 📐 设计目标，尚未落地 |
| M3 | 证据链长期记忆 | `ToolCallTraceRepo` / `NodeTraceRepo` / `OrchestrationStep` | MySQL 平铺表 | 工具调用全文、节点执行记录 | 永久 | 工程师 UI / 审计 | 📐 设计目标，尚未落地 |
| 自治 | 跨会话缓存 | `ToolResultCache` | Redis | 稳定工具结果的版本化缓存 | 按 tool 策略 TTL | 降本（命中省一次 ACL+回灌） | 📐 设计目标，尚未落地（语义缓存默认关闭） |
| L5 | 会话元数据 & 确认记录 | `Repo`（会话台账）/ Permission 确认记录存储 | MySQL + Redis | 会话生命周期、失败计数、gate 确认令牌 | 会话级 ~ 30min | 编排器 / 写动作闸门 | 📐 设计目标，尚未落地 |

> 落地状态图例：📐 设计目标，尚未落地（本文全部条目均为此状态；代码仓现状见文首"落地状态声明"）。
> **命名提醒**：M1 的 `SessionService` 是 AgentScope 2.0 的会话能力（框架侧，`app/_service/_session.py`；**2.0.5 起由旧名改名而来，按旧名写不可导入**）。注意它是「组合 storage 与 message bus，处理会话创建/更新/删除、探测 `SessionStatus`、派发中断」的**门面，不绑定具体存储后端**——不要把选型写成它的属性。用途限于**会话内上下文与多轮对话记忆**；L5 里的会话台账是本项目 MySQL 业务表（`agent_session` / 失败计数），两者职责不同不可混称。本项目**不自研**会话封装类，也不在其上实现挂起/恢复语义。

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
   │  M1 工作记忆（会话内上下文 · 永久留存，可跨页面续聊）              │
   │  AgentScope Storage（AsyncSQLAlchemy → MySQL）会话键 = session_id   │
   │  存：AgentState 会话内上下文与多轮记忆（类型化字段值）               │
   │  gate 产待办即本段终点——不挂起、不载回快照续跑                     │
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

这是大家通常说的"Agent 记忆"--**多轮对话与会话隔离靠它**；它**不**用来"跑到一半挂起、事后继续"。

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

    # 各步骤结果 + gate 决策——⚠️ 字段有两种身份，必须逐字段标清（判据见 §3.6 与 §3.9）
    first_article_result: dict | None = None       # 业务事实取值：只作本段留痕，跨段/跨轮不作输入
    first_article_observed_at: str | None = None   # 取值时刻；没有它这个字段就是个危险的过期事实
    gate_first_article: str | None = None          # 人做的决策，非事实 → 可持久复用
    action_card: dict | None = None                # 产物（不是对外部世界的断言）→ 可持久复用
    ...

    # agent 通信
    pending_tool_calls: list[dict] = []
    tool_results: list[dict] = []

    # 写动作透传：gate 确认后把确认记录存此，write 步骤取用
    confirmation: dict | None = None
```

并发分支同写一个字段时在汇合（barrier）处取后者、`None` 不覆盖有效值--这是本项目在应用层合并时自定的归并规则，不依赖任何引擎/框架的状态归并原语（框架无共享状态图，也无广播原语）。
四类场景（换线 / 故障复产 / 客诉 8D / 工艺变更）**共用一套 schema，各场景只装配自己用到的子集**--DDD 共享内核 + 限界上下文装配的落地。

### 3.2 持久化：会话键 = session_id

会话与消息的持久化由 AgentScope 2.0 的 `Storage` 后端承载（随 FastAPI Agent Service（`create_app`）的 lifespan 装配），`storage` 与 `message_bus` **两者都是必填参数**，框架不提供隐式兜底：

```python
# 构造形态按官方 docs 2.0.8 写；本项目基线 2.0.11，升级到该版本需复确认（见 ADR-01 存储行 / §14-7）
from agentscope.app import create_app
from agentscope.app.storage import AsyncSQLAlchemyStorage      # 可选 extra `agentscope[sql]`，驱动需自装 asyncmy
from agentscope.app.message_bus import RedisMessageBus

storage = AsyncSQLAlchemyStorage("mysql+asyncmy://...")   # 本项目选型：落到已有的 MySQL
message_bus = RedisMessageBus(host="...", port=6379)       # 只有 Redis 实现，换成 SQL 存储也省不掉

app = create_app(storage=storage, message_bus=message_bus, ...)
```

**选型理由（本轮改判，原口径为 Redis-backed）**：`StorageBase` 契约覆盖 agent / session / credential / **message** / schedule / team / knowledge base 记录，因此**消息历史归框架 Storage 管，不需要自建表**，框架也自带 `GET /sessions/{id}/messages`（`offset` / `limit` 分页）供前端拉历史会话。落 MySQL 后即得到三个收益：**关掉页面若干天后仍能接着问**、审计可查、指标可 SQL 直取；而 Redis 做长期留存要靠 TTL 与 `maxmemory` 淘汰，天然不是"隔几周翻回来"的位置。项目已有 `mysql:8.0`，**零新增基础设施**。

**关键映射**：会话键 = `session_id`（本项目在编排服务装配处建立该映射，设计目标）。
`SessionService` 按 `session_id` 隔离 `AgentState`；同一会话的状态更新串行落盘（单写者），不同会话互不影响。

> 落地状态：**设计目标，尚未落地**——代码仓尚无会话层实现，`storage` / `message_bus` 的接线属于真实部署阶段的待建工作。
> 设计约束：`SessionService` + `AgentState` 的用途限定为**会话内上下文与多轮对话记忆**；本项目**不在其上实现 gate 挂起/续跑**（见 §3.6）。下一段为何要重查事实而不是取回会话里的旧字段，理由也在 §3.6。
> ⚠️ **选型改判带来的新风险**：会话从"会话级 TTL"变成长期留存后，"把查过的业务事实留在会话里"会从隐患变成历史包袱。因此必须同时执行 §3.9 的字段可持久性判据：哪些字段可以存着复用，哪些只能留痕、每轮重查。

### 3.3 MySQL 侧：不建自研会话状态表，但框架的会话/消息记录就落在这个库

原方案在 MySQL 自建三张会话状态/快照表；本架构下 **仍然不建自研会话状态表**，只留业务可观测表：

| 表 | 主键 | 存什么 |
|---|---|---|
| `agent_session` | `session_id` | 会话台账：状态机、场景、租户、待办原因（转人工待办的缘由） |
| `agent_step_record` / `node_trace` | `(session_id, step)` | 每步执行记录（CODE/AGENT 性质、gate 决策、trace 引用） |
| `tool_call_trace` | `trace_id` | 工具调用全文（证据链） |

**两条必须分清的易混项**（否则读者会以为选型绕回了原点）：

1. **已取消的 `SqlSaver` ≠ 现选的 `AsyncSQLAlchemyStorage`**。前者是本项目自研的**断点续跑快照表**（作恢复执行态用，该语义已整体取消，见 ADR-05）；后者是**框架自己的会话/消息持久化后端**，只回答"这段对话聊过什么"，不回答"从哪一步继续跑"。选了后者**不等于**恢复前者。
2. **框架表归框架，业务表归业务**。框架的 session / message 表由框架的 Alembic 迁移管（本项目不写它们的 DDL）；`agent_session` 只做**业务投影**（状态机、场景、闸门、`waiting_reason`），**不重复存消息**。

另：字段**演进序列**（每个历史值都留一份，见 §3.4）框架不提供——框架存的是当前值。如需这条序列，才把 `AgentState` **可选归档**到 MySQL 审计表（只读，仅供复盘，**不作为恢复源**）。

### 3.4 字段演进序列与"存了什么"

每次状态更新带 parent 指针，形成可追溯的字段演进序列（类似 git commit 链，**仅供审计复盘，不作为恢复入口**）：

```
snap_1 (parent=null)   <- plan 执行完
snap_2 (parent=snap_1) <- first_article 执行完
snap_3 (parent=snap_2) <- gate_first_article 产待办（本段结束，不是挂起）
snap_4 (parent=snap_3) <- 下一段重查事实后 process_switch 执行完
```

存的是 **state 本身**（各字段的当前值）。注意：推给责任人的**动作卡以事件形式流出**，不进状态快照--快照只在需要"这步要落库"时留一份意图字段与待办 id；
下一段**不从快照继续**，而是按 `session_id` + `evidence_set_id` 重查当下事实（见 §3.6）。
编排能力不用回退（gate 确认后不撤销），序列只服务审计与复盘。

### 3.5 gate 分段与确认回调协议

gate 步骤不是挂起点，而是**本段终点**（本项目在 `EXPLORE` / 自定义 deny 规则之上组合出的写闸门策略拦截写工具执行，接线点拟建于 `app/application/orchestration_service.py`，设计目标路径）：

```python
async def open_gate(self, session_id, step, card):
    await self._dispatcher.push(card)              # 1. 经事件系统推动作卡
    ticket = await self._tickets.create(           # 2. 待办落成 MES 已有工单/审批实体
        card, session_id=session_id, step=step)    #    （维修单/隔离单/首件放行/8D/SOP）
    # —— 本段 flow 在此 return：协程不让出等事件，不保存待恢复快照 ——
    return GateOutcome(step=step, waiting_ticket=ticket.id, decision="WAITING")
```

写闸门做四件事：① 先把本段执行记录落 `agent_step_record`（含待办 id）② 拦住写工具执行（闸门在每次执行，不在提示词层）③ 会话标 `WAITING` ④ 流出待办与动作卡事件。责任人分派、催办、超时升级全在工单实体生命周期里，agent 侧不计时。

审批完成后起下一段（回调 `POST /agent/orchestration/{session_id}/advance`，`advance()` 是本项目封装的方法名，非框架 API）：

```python
ctx   = await self._sessions.load_identity(session_id)      # 只取会话标识 + scenario + 待确认动作
token = await self._permission.record_confirmation(session_id, step, approved, user_id,
                                                   action=card.writes_via_action())  # 网关一次性令牌（主闸）
facts = await self._flows.requery_live_facts(ctx)            # ★ 重查当下现场事实，不取历史快照
await self._flows.run_next_segment(ctx, facts, token)        # 新的一次短调用，幂等可重入
```

下一段内部：① 按 `session_id` 取会话标识与 `evidence_set_id` ② **重查当下事实** ③ 校验一次性动作令牌 ④ 过写路径。**不存在"从拦截点继续"这层语义**——分段之间的衔接靠业务事实，不靠内存态。

### 3.6 为什么不做跨进程恢复（原"工作记忆价值"论证不再成立）

曾经的设计是：副本 A 跑到 gate 挂起 -> `AgentState` 落 Redis（旧方案的存储后端；现选型见 §3.2）-> 进程死 -> 副本 B 收到确认后按 `session_id` 载回 state 从断点继续。**这套机制本项目不实现，也不找框架等价物替换**（框架只有单次调用作用域内的 `RequireUserConfirmEvent` 与 `interrupt` 端点，没有 `Command(resume=…)` / checkpoint 这类从存档位置续跑的原语），理由三条：

1. **没有需要保住的计算态**：gate 之间几乎全是确定性代码步骤（换线快路径 LLM 调用为 0），"挂起期"只是在等人，不持有必须原样恢复的中间结果。
2. **重查比重放更正确**：MES 现场事实**时变**（首件状态、路线版本、钢网在借、齐套数量都会变），把 T+3s 的快照载回 T+10min 的进程继续跑，等于用过期事实做当下的决策。与 §1.1 时变性红线（语义缓存默认关闭）是同一条理由。
3. **等待本就有更可靠的载体**：每个 gate 产出的都是 MES 已有业务对象（维修单 / 隔离单 / 首件放行 / 8D / SOP），状态机、责任人分派、催办、超时升级、审计时间线全在现成的审批流里（ADR-05）。

于是进程中途死亡**不需要特殊处理**：每段以 `session_id + step` 为幂等键、写路径另以 `confirmation_id` 为幂等键，本段落库后下一段照常由回调重新发起。`BackgroundTaskManager` 只托管**单次调用的短后台任务**，不再承担"长任务不随请求生命周期终止"这一职责。

### 3.7 能力节点的子会话隔离

主编排调用能力 Agent 时使用独立的子会话键（见 `FactoryAgent/FactoryAgent-流程编排能力.md` §6.2）：

```python
result = await agent.reply(
    state,
    session_id=f"{state.session_id}:{capability}",   # 子会话键
)
```

`{session_id}:{capability}` 使主编排状态与能力节点的推理状态在 Session store 分开存放，互不覆盖。
这是"多 session 并发 + 能力节点独立"的隔离手段（调用时机由本项目 `flows/` 里的应用层代码决定——顺序 `await` 串联、`asyncio.gather` 并发多个各自独立的 `Agent.reply()`；框架无移交原语，隔离一律靠会话键）。

### 3.8 分层记忆的正确启用方式：按**用途**接，不按能力开关整套

本节旧写法是"诊断能力不接会话存储"。本轮复核判定**该结论与其论证均不成立**，已改写；权威源是 `FactoryAgent/FactoryAgent-诊断能力.md` 的存储职责表与装配骨架，它写的是诊断**接** `AgentState` + `SessionService`，用途为多轮追问与会话隔离。

错误出在论证的一步跳跃上：`SessionService` 的用途至少有**两件独立的事**——① 多轮追问与会话隔离，② gate 挂起与断点续跑。旧文把它窄化成"会话存储就是为了中途暂停"，于是"短链路不需要中途暂停"顺手推出了"不需要会话存储"。取消挂起层（ADR-05）撤掉的是 ②，与 ① 无关。

当前口径：

| 能力 | 接 M1 的哪个用途 | 明确不接什么 |
|---|---|---|
| 诊断 | **多轮追问与会话隔离**。现场典型问法是"这批焊接不良什么原因" -> "上一班次呢" -> "它和钢网有没有关系"，追问依赖上一轮的指代与结论，不接会话就做不出这种连贯 | gate 挂起/续跑语义——短链路一次 `reply` 跑完即出，本来就没有中途暂停 |
| 草稿编写 | 会话隔离 + 产物留存 | 同上 |
| 流程编排 | 会话隔离与多轮上下文 | 分段之间**不靠**会话快照恢复，靠 `session_id` + 重查事实衔接 |

> **记忆按需启用的正确粒度是"用途"，不是"能力"。** 旧写法"某能力整套不接记忆"看着简洁，实际会连带砍掉该能力最需要的多轮上下文。
> 顺带修正一处连带结论：`Offloader` 属 M2 上下文压缩，与 Session 是两层。旧文"诊断无 Offloader 接线"的理由不能挂在 Session 上。准确表述是：诊断目前**按设计目标不接线** M2（步数预算 20、历史较短，先走"模型直接看全文"的简化路径），这是**尚未接线的分阶段策略**，不是"按需所以不需要"的裁定——诊断若拉到设备参数曲线这类超大结果，M2 仍须接线（见 §4.2）。

### 3.9 字段可持久性判据：哪些能存着复用，哪些只能留痕

会话长期留存后，第一个要回答的问题变成：**这个字段下次取出来用，对不对？** 判据只有一条：

> **这条数据换个时刻重算，结果会不会不同？** 会不同 -> 只能留痕，不能当下一轮 / 下一段的输入；不会不同 -> 可持久化并复用。

| 可持久并复用（左列） | 只能留痕、每轮重查（右列） |
|---|---|
| 用户原话与模型回复（消息历史）、指代锚点（"它" = SN-001）、已确认的 gate 决策与 `confirmation_id`、产物文本（草稿正文、动作卡内容）、`evidence_set_id` 引用、版本锚点 `route_version`（锚点本身是意图的一部分） | 首件状态、齐套数量、钢网在借、路线 / BOM 的**当下生效值**、过点记录、设备实时参数 |

"要求查最新数据"与"会话持久化"约束的是**两列不同的东西**，作用域不重叠，因此**不矛盾**——只有把右列的取值结果当会话状态存下来、下次直接复用，两者才真正打架。

同一份现场事实有两种**身份**：作为 trace 存下来（"当时 agent 看到齐套数 = 3"）永久合法，工程师回看、审计、评测复现都要它；把 `3` 这个值取来当下一段的判断依据则非法。前者是对历史事实的陈述，后者是对当前事实的断言。这也是 §3.6 要求"重查而不是载回快照"的根据，与 §8.3 语义缓存默认关闭、§3.8 的用途切分是同一条时变性红线在三个层的投影。

落地约束两条：

1. 右列字段进 `OrchestrationState` 必须配套 `*_observed_at`（见 §3.1），且下一段**禁止读取**，只允许写入与归档进 trace。
2. 跨页面续聊（恢复会话继续问）与跨闸门推进（确认后起下一段）**都要走重查**，区别只在重查范围：前者查回答里引用的事实，后者查决策依据的事实。

### 3.10 取数策略闸门：把 §3.9 的判据变成不可绕过的执行点

§3.9 回答的是"哪些字段能存着复用"，但它是**文档判据**，不是运行时约束：ReAct 里的模型能读到 M1 消息历史（含历史 `tool_result` 块）与 M2 压缩结果，仅在 system prompt 里写"必须重查"无法保证它照做。本节把判据执行化——与 §1.1 时变性红线、§3.6 重查而非重放、§6.3 语义缓存默认关闭是同一条约束在会话层、执行层、缓存层的投影。

> **落地状态**：本节全部为 📐 设计目标，尚未落地。`DataScopePolicy` / `AnswerValidator` / `history_rehydration_policy` 均为本项目拟定的自研封装命名，**不是 AgentScope 2.0 框架 API**；代码仓现状见文首"落地状态声明"。

#### 3.10.1 不新增锚点维度：`data_scope` 只由 `as_of` 派生

项目已有 `as_of`（网关 §6.1）与 `version_anchor`（§8.2）。**`version_anchor` 是贯穿全链路的约束条件，不是取数语义**：§8.2 要求它出现在 state、工具入参、缓存 key、trace 里，几乎每个请求都带；若把"带了 version"判成"按版本缓存答案"，等于给绝大多数现场查询开了答案缓存，直接击穿 §1.1 时变性红线。典型反例："SN-001 按 v3 路线过了哪几站"——version 只是过滤条件，问的是**当下**的过点记录（持续追加、时变）。

因此 `data_scope` 只有两值，且**不是新请求字段**，由纯函数派生：

| 请求里已有的信号 | 派生 `data_scope` | 语义 |
|---|---|---|
| `as_of` 非空 | `AS_OF(ts)` | 历史回放，必须按 `as_of` 取 M3 trace / 历史读模型快照，**禁查实时**（查实时反而答错） |
| `as_of` 为空（默认） | `CURRENT` | 现场当下事实；`version` 是否存在都不改变它 |

`version` / `version_kind` **不参与 scope 派生**，只下沉为工具级 `IMMUTABLE` 的缓存 key 维度与 `AUTHORITATIVE_RECORD` 的版本锚点（§3.10.3）。`as_of` 与 `version` 同时非空时不冲突：前者决定"看哪个时刻"，后者决定"按哪个版本解释这个时刻"。

派生规则放 DDD 共享内核，网关与 `agent-service` **各自从同一请求字段调用同一函数**，不传 `data_scope`、不新增协议字段；判定 0 次 LLM，语义模糊时按 `Agent路由与多轮上下文-市面对照与演进方向.md` §3.3 补问，不猜。`turn_id` 由 `agent-service` 编排入口按 `session_id` + 请求序号**生成一次**，随能力调用下传（能力节点不自行生成），进 M3 trace 与出口校验；网关只派生 `data_scope` 供缓存决策，不需要 `turn_id`。

#### 3.10.2 新鲜度的边界是「轮次」，不是时长

MES 现场事实持续追加（§6.2 已把 `query_pass_records` 判为"持续追加、短 TTL 或不缓存"），齐套数 / 首件状态在几秒内就可能变，`max_staleness_ms` 给不出安全阈值。因此判定用 **turn 边界**：同一 turn 内的重复取数允许复用请求级 memo；跨 turn（跨页面续聊、跨闸门 `advance()` 新段）的 **`LIVE_ONLY` 取值一律重查**。`IMMUTABLE` 是版本化不可变数据，不受 turn 边界约束，可跨 turn 复用——这正是它与 `LIVE_ONLY` 的分界。`observed_at` 只作标注、审计与出口校验依据，**不参与"还新鲜吗"的判定**。

这样 §3.5 的 `requery_live_facts`、§3.9 落地约束 2 的"两种重查范围"都成为同一规则的特例，不必逐处写约定；§3.8 的多轮追问连贯性也不受影响——记忆仍提供指代与叙事，只是不再提供事实取值。

#### 3.10.3 四类取数策略与默认口径

| `data_class` | 允许的数据源 | 典型 | 说明 |
|---|---|---|---|
| `LIVE_ONLY` | 本 turn 实时工具 | 首件状态、齐套数量、钢网在借、过点记录、设备实时参数 | 缓存 / 状态取值 / 历史 `tool_result` 一律不得作为断言依据 |
| `AS_OF_ONLY` | M3 `tool_call_trace` 中 `occurred_at <= as_of` 的历史记录 / 按 `as_of` 的读模型快照 | "当时过点轨迹""回放当时的判定" | 由 `evidence_set_id` 锚点定位；实时结果在此语义下是错的 |
| `IMMUTABLE` | §6 版本化工具结果缓存（长 TTL，可跨 turn） | `query_process_route(route_id, v3)` | key = `route_id + version_anchor + tenant`（§6.2 已有）；`version` 在这里才是取数依据，长 TTL 不需要数据水位 |
| `AUTHORITATIVE_RECORD` | 记忆即权威源 | gate 决策、`confirmation_id`、指代锚点、`evidence_set_id`、`version_anchor` | 不是"允许用记忆"，而是**必须用记忆，且禁止被实时数据覆盖** |

```python
# 设计目标路径：app/domain/data_freshness_policy.py（项目自研，非框架 API）
DATA_SCOPE_POLICY = {
    "query_kitting_qty":     DataClass.LIVE_ONLY,
    "query_pass_records":    DataClass.LIVE_ONLY,
    "query_process_route":   DataClass.IMMUTABLE,           # 需带 version_anchor
    "gate_first_article":    DataClass.AUTHORITATIVE_RECORD,
    "seed.anchor_entity":    DataClass.AUTHORITATIVE_RECORD,
}
DEFAULT = DataClass.LIVE_ONLY       # ★ 未登记一律偏向查新（fail-safe）

def resolve_data_scope(request) -> DataScope: ...          # 只看 as_of，不看 version
def resolve(request, tool_or_field, turn_id) -> SourceDecision: ...
```

**默认必须是 `LIVE_ONLY`**：MES 里用错旧数据的代价远大于多查一次工具。第 4 类把"该用记忆"从"省钱优化"升格为语义要求——这也是 §3.10.6 里 `redundant_live` 反例的判定依据。注意 `AUTHORITATIVE_RECORD` 里的 `evidence_set_id` / `version_anchor` 是**引用与锚点**（本身就该存），`AS_OF_ONLY` 的历史记录是它们指向的内容，两者不重叠。

#### 3.10.4 三个执行点：入口 / 取数 / 出口

| 位置 | 组件 | 强制规则 |
|---|---|---|
| 入口 | `resolve_data_scope(request)`（网关 / 编排入口，纯函数） | 只派生 `AS_OF` / `CURRENT`、不落协议字段；结果供缓存与取数闸门消费 |
| 取数 | `ToolExecHook`（§2.3 已拟建）+ ACL `get_or_compute` | **唯一取数通道**，与 `Permission` 同层，照搬 ADR-04「主闸在工具网关，模型绕不过」。`data_class == LIVE_ONLY` 且（`CURRENT` 或跨 turn）→ 强制实时，缓存命中也作废；`IMMUTABLE` / `AS_OF_ONLY` / `AUTHORITATIVE_RECORD` 按各自允许的数据源取 |
| 出口 | `AnswerValidator`（`agent-service` 侧，能力产出答案前；确定性校验） | 对答案 `sources` 里每条断言，**按其 `data_class` 查同一张策略表**核验证据出处：`LIVE_ONLY` 必须 ① `turn_id == 当前 turn` ② `occurred_at >= 请求开始时间` ③ `source == LIVE`；`IMMUTABLE` 允许 `TOOL_CACHE` 但必须带 `version_anchor`；`AS_OF_ONLY` 必须来自 `as_of` 证据集；`AUTHORITATIVE_RECORD` 必须来自会话 / 台账。不满足 → 拒绝作答，触发重查或置 `needs_human_review` |

`CURRENT` 请求里合法引用不可变版本数据（如"按 v3 路线查过点"）不会被误判：校验按 `data_class` 分支，不再一律要求 `LIVE_ONLY`。取数决策**不给 LLM**：模型可以"想用旧值"，但闸门不返回旧值。出口校验也不是让 LLM 判答案对不对，而是查 M3 `tool_call_trace` 核对证据出处，无需新表、无需 judge 模型；网关只消费 `needs_human_review`，不查 agent 侧 trace 表（保持 `Agent入口网关-意图路由.md` §1.2 的无状态分派器定位）。

#### 3.10.5 两处需要同步的既有设计

1. **消息历史回灌（§3.9 落地约束 1 没覆盖的路径，📐 待补）**：框架 message 记录永久留存且含 `tool_result` 块（见 §9），跨页面续聊会把上一轮"齐套数 = 3"的原文喂回模型。历史 `tool_result` 回灌走 `history_rehydration_policy`（复用 §4.2 `result_compactor` 的策略，加 `mode="history"`），降级为引用块 `{hint, trace_id, observed_at, usable_as_fact: false}`：**保留叙事与指代**，只取消其"可作事实断言"的资格，出口校验兜底。不做数值剥离——剥离会让模型不知道上一轮聊过什么，反而破坏 §3.8 的多轮追问。
2. **网关答案缓存与工具结果缓存（已在网关文档同步）**：`Agent入口网关-意图路由.md` §9 的 `GatewayService.chat()` 原为无条件"缓存命中即返回"，与本节冲突；现改为按 `data_scope` 决定是否读写缓存——**仅 `AS_OF` 可缓存，`CURRENT` 一律跳过，即便请求带 `version`**（网关 §5 / §6.1 / §9 / §11.1 同步）。§6 工具结果缓存对 `LIVE_ONLY` 只允许 `scope="turn"` 的请求级 memo，跨 turn 失效；`IMMUTABLE` 仍按 §6.2 长 TTL 缓存。两处共用同一张策略表，避免双份口径。

#### 3.10.6 可观测、评测门禁与落地顺序

- `ToolCallTrace`（§5.1）加 `data_scope` / `data_class` / `source` / `turn_id` 四列；网关 `gateway_cache_hit_total` 按 `data_scope` 打 label，`CURRENT` 跳过缓存的请求不进命中率分母。
- 评测集补三类反例：**`stale_hit`**（该查却用了旧值）、**`redundant_live`**（该用记忆却重复查）、**`version_leak`**（请求带 `version` 却被整体判成可缓存、返回旧现场数据，专门回归 §3.10.1 的防呆）；`stale_fact_rate = 0` 作 `EvalGate` 硬门禁，接 `RAG与Agent评测` 侧自研 `EvalRunner` / `EvalGate`（📐 设计目标）。没有这道门禁，"保证"只是设计宣言。
- 落地顺序（最小改动优先）：① 派生 `resolve_data_scope`（0 侵入）→ ② 取数闸门 → ③ 两处缓存策略化 → ④ 历史回灌降级 → ⑤ 出口校验 + 评测门禁。与 §10「先验证流程正确性，再启用降本有效性」一致：①~④ 属正确性，缓存命中率属降本，**不得用后者压前者**。

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
    turn_id: str                  # ★ 新鲜度边界（§3.10.2）：跨 turn 的 LIVE_ONLY 取值不得作断言
    data_scope: str               # CURRENT | AS_OF（只由 as_of 派生，非协议字段）
    data_class: str               # LIVE_ONLY | AS_OF_ONLY | IMMUTABLE | AUTHORITATIVE_RECORD
    source: str                   # LIVE | TOOL_CACHE | MEMORY | HISTORY_TRACE
```

三种状态都落：`save_ok` / `save_denied`（被 gate 拒）/ `save_error`。
`bounded_context` 字段把 trace 锚定到 DDD 限界上下文，给工程师 UI 按上下文回溯用。
后四列是 §3.10 取数策略闸门的决策留痕：出口校验按 `data_class` 分支核对证据出处（`LIVE_ONLY` 才校 `turn_id` + `occurred_at`），工程师 UI 也能直接看出"这条事实是当场查的，还是复用来的"。均为 📐 设计目标，尚未落地。

> 落地状态：**设计目标，尚未落地**——每次工具调用落一行 `tool_call_trace` 由工具执行节点（Toolkit 回调处）实现，代码仓尚无该节点。

### 5.2 NodeTrace / OrchestrationStep / DraftTrace

- `NodeTraceRepo`：flow 步骤（CODE / AGENT 性质）执行记录，含 `agent_hypothesis` / `agent_confidence` / `tool_call_traces` 反向引用
- `OrchestrationStep`：步骤级记录，状态 `PENDING|RUNNING|GATE_WAITING|CONFIRMED|FAILED`
- `DraftTraceRepo`：草稿归档（草稿能力只落草稿不落库）

以上三者的落库位置为 MySQL（`app/infrastructure/persistence/models.py` 拟建，设计目标路径），表结构对齐可观测文档 §7.1 / 流程编排能力 §5——**均为设计目标，尚未落地**。

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
- 会话状态机 `PLANNING -> RUNNING -> WAITING/DONE/FAILED`（`WAITING` = 已产待办、本段已结束，**不是挂起态**）
- **失败计数** `failure_count: dict[str,int]`，按 `(session_id, capability)` 追踪 agent 连续失败，≥2 次本段置 `WAITING` 并转人工待办（agent 故障隔离，不自动重试）
- `waiting_reason`（转人工待办的缘由）、gate 决策记录、步骤记录

会话内上下文与消息历史的存储由框架 `Storage` 后端承担（本项目选 `AsyncSQLAlchemyStorage` 落 MySQL，见 §3.2），对外经 `SessionService` 访问；本项目**不自研**会话封装类，业务侧只保留"按 `session_id` 取回台账"这一层；**不做续跑语义**，下一段由确认回调重新发起并重查事实。

### 7.2 失败计数与 agent 故障隔离

```python
# Repo
async def increment_failure_count(self, session_id, capability) -> int:
    key = (session_id, capability)
    self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
    return self._failure_counts[key]
```

`FailureTracker` 调用：agent 成功则 `reset_failure_count`；失败则 `increment`，≥2 次 `update_status(WAITING)` + `log_waiting_reason`（并开人工待办）。
**不自动重试**--agent 连续失败说明证据不足或工具异常，转人工更安全。

### 7.3 Permission 确认记录：写动作的人确认记忆

ConfirmationStore 的语义已并入 **Permission 系统**，落库形态只是 Permission 在 Redis 里的确认记录存储（本项目拟建，设计目标）。每个 gate 确认签发 token（`secrets.token_hex(16)`，TTL 30min）：

```
Key1: confirm:{token_id}                     -> JSON payload（action/approved/user_id/issued_at）
Key2: confirm:session:{session_id}:{step}    -> token_id（防重复确认，幂等）
```

`action = f"{verb}:{session_id}"`（如 `activate_route:S-xxx`）绑定 session+step，校验时 `valid_for(expected_action)` 防篡改。
两个 key 都 TTL 30min。`is_already_confirmed` 提供幂等：同 session+step 已确认则返回既有 token。

**token 双重身份**：既是 `/advance` 回调起下一段时交回 gate 判定的**凭据**（Permission 只在此凭据有效时放行写工具执行），又是下游应用服务的**幂等键**--
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

> **执行化落点**：本节只回答"缓存该不该开"；"该查新时真的查了、该用记忆时真的用了"由 §3.10 的取数策略闸门保证——四类 `data_class`、turn 边界、取数闸门、出口校验与评测门禁是这条红线在运行时的形态，不是额外一套设计。

### 8.4 记忆按需启用的粒度是"用途"，不是"能力"

诊断与编排**都接** M1 的"多轮对话与会话隔离"用途；**都不接**它的"gate 挂起 / 断点续跑"用途（逐条理由见 §3.8；旧写法"诊断整套不接会话存储"已推翻）。分段之间靠重查事实衔接，不靠会话快照恢复。
**不无脑全配** -- 记忆组件都有成本（存储 I/O / 缓存失效复杂度 / 接线维护），但裁剪要按**用途**切：同一层内该用的用途用、不该用的用途关掉，而不是按能力整层开关。

> 顺带把口径钉清楚：本项目真正需要的是**外部化状态（审计与可重入）+ 上下文压缩**这两件事，**不需要跨进程挂起/恢复**；多 Agent 也只是可选的实现方式之一，不是必要条件。本项目的分层记忆就是围绕这两件事建的。

### 8.5 状态外置换的是审计与可重入，不是挂起恢复

**权衡**：每次步骤执行一次状态写入（框架 Storage 落 MySQL + 业务台账同库）的延迟 vs 可审计、可重入。选前者换后者。
⚠️ 本轮把 Storage 从 Redis 改成 MySQL 后，**这条权衡的代价变了**：per-step 状态写从内存型后端换成关系库，多一次连接占用与页写——粒度仍是 per-step（非 per-tool-call）且框架在调用结束才整体写回，但**具体延迟量级待压测**（与过点 200ms 红线分属不同链路，不得拿红线数据代替实测）。
⚠️ 注意这一条**不是**"为了进程死后能接着跑"：本项目不载回快照续跑（理由见 §3.6），而是保证每步的输入输出与 gate 决策可回溯、且每段幂等可重入--进程死了本段重跑或下一段照常发起，不需要"同一个在途调用被唤醒"。
状态记录粒度是 per-step（非 per-tool-call），开销有界。

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
| 会话内上下文（AgentState） | 框架 `Storage`（本项目选 `AsyncSQLAlchemyStorage` 落 **MySQL**；默认实现为 `RedisStorage`） | 会话内字段值 + 多轮对话记忆 | `session_id`（能力子会话 `{session_id}:{capability}`） | 永久（业务可配清理策略） | M1 工作记忆；**不作为 gate 续跑的快照源**（分段靠重查事实衔接）；字段能不能存着复用按 §3.9 判 | 📐 设计目标 |
| 消息历史（框架 message 记录） | 同上 `Storage` | 会话内消息（含 tool_call / tool_result 块） | `session_id` + `offset` 分页 | 永久 | 跨页面续聊与前端拉历史（框架自带 `GET /sessions/{id}/messages`）；**不需自建表** | 📐 设计目标 |
| `agent_state_archive` | MySQL（**可选**） | 字段**演进序列**归档（框架只存当前值，序列需自归档） | session_id + 快照序号 | 永久 | 审计复盘，**不作为恢复源** | 📐 设计目标 |
| `tool_call_trace` | MySQL (app) | 工具调用全文 | trace_id | 永久 | M3 证据链 | 📐 设计目标 |
| `node_trace`（flow 步骤记录）/ `agent_step` | MySQL (app) | flow 步骤 / 委托执行记录 | record_id | 永久 | 编排能力审计 | 📐 设计目标 |
| `agent_session` | MySQL (app) | 会话元数据 | session_id | 永久 | L5 生命周期 | 📐 设计目标 |
| `failure_count` | MySQL (app, via Repo) | 失败计数 | (session_id, capability) | 会话级 | L5 故障隔离 | 📐 设计目标 |
| `confirm:{token_id}` | Redis | 确认令牌 payload | token_id | 30min | L5 写动作闸门 | 📐 设计目标 |
| `confirm:session:{sid}:{step}` | Redis | 防重复确认 | (session_id, step) | 30min | L5 幂等 | 📐 设计目标 |
| `tc:{tenant}:{tool}:{hash}` | Redis | 工具结果缓存 | tenant+tool+args+version_anchor | 按 tool 策略 | 自治 降本 | 📐 设计目标 |
| `llm_call_log` | MySQL (app) | LLM 调用指标 | call_id | 永久 | 可观测/降本归因 | 📐 设计目标 |
| `BackgroundTaskManager` 活跃任务登记表 | 框架托管（`app/_manager/`） | session_id->后台任务 | session_id | 进程寿命 | **单次调用的短后台任务**跟踪（不做 gate 计时/催办） | 📐 设计目标 |
| prompt cache | provider 侧 | system prompt+工具定义 | 内容哈希 | 5m/1h | 上下文窗口层降本 | 📐 设计目标 |

## 10. 落地状态与接线清单

**现状（核实过）**：代码仓 `E:\factorybot` 的 `src/` 下只有 `agents/tool_test.py`、`api/{health,index,tool_test}.py`、`schemas/tool_test.py`、`tools/get_current_time.py`、`factory.py` 这套脚手架，**诊断 / 草稿 / 编排三项能力与记忆系统的各层全部未落地**（没有 mock 端到端流程，也没有相应 pytest）。本节因此不是"接线进度表"，而是**建设清单**：

| 组件 | 设计状态 | 落地待办 |
|---|---|---|
| `SessionService` + `AgentState`（M1 工作记忆） | 📐 设计目标 | 随 `create_app` 的 lifespan 装配：`storage` 选 `AsyncSQLAlchemyStorage`（落 MySQL，存会话与消息），`message_bus` 选 `RedisMessageBus`（框架必填且只有 Redis 实现）；仅用于会话内上下文与多轮记忆，不自研会话封装类，**不实现挂起/续跑** |
| MySQL 状态归档（可选） | 📐 设计目标 | 落 `agent_state_archive` 归档表，仅供审计复盘，不作为恢复源 |
| `ToolCallTraceRepo` | 📐 设计目标 | Toolkit 工具执行回调处每次落一行（SQLAlchemy） |
| `Repo`（会话台账） | 📐 设计目标 | `agent_session` / `failure_count` 落 MySQL |
| Permission 确认记录存储 | 📐 设计目标 | `PermissionEngine` 叠加层 + 网关一次性确认令牌；Redis 存确认记录 |
| `Offloader` / `ToolOffloadMiddleware` 压缩策略 | 📐 设计目标 | 工具结果回灌前走压缩/卸载 |
| 缓存标记 Middleware | 📐 设计目标 | 插在模型调用的 Middleware 链外层 |
| 早停 Middleware | 📐 设计目标 | 接在诊断 Agent 的收敛判定处 |
| `ToolResultCache` | 📐 设计目标 | ACL 客户端 `get_or_compute` 封装；`LIVE_ONLY` 只允许 `scope="turn"` 的请求级 memo（§3.10.5） |
| `DataScopePolicy` + 取数闸门（§3.10） | 📐 设计目标 | 共享内核策略表 + `ToolExecHook` 侧唯一取数通道；与 `Permission` 同层，模型绕不过 |
| `AnswerValidator` + `history_rehydration_policy`（§3.10） | 📐 设计目标 | 答案出口按 `turn_id` / `occurred_at` / `data_class` 校验证据出处；历史 `tool_result` 降级为引用块；评测集补 `stale_hit` / `redundant_live` 两类反例并接 `EvalGate` |

> 上表的 `SessionService` / `Offloader` / `ToolOffloadMiddleware` / `PermissionEngine` 是 **AgentScope 2.0 已核实的框架能力名**；
> 表内 `Repo` / `ToolResultCache` / `ToolCallTraceRepo` 一类是本项目的自研封装与台账仓储**命名（尚未创建）**，不是框架 API。

**设计原则**：先用最小端到端链路验证"流程正确性"（记忆流转、gate 分段/确认回调、证据链落库），
再启用"降本有效性"（压缩、缓存、prompt 缓存命中）。两者解耦，可分阶段启用。

## 11. 与既有文档的引用映射

| 本文章节 | 详细实现见 |
|---|---|
| §3 M1 工作记忆 | `整体技术选型与模块划分.md` §3.5.3、`FactoryAgent/FactoryAgent-流程编排能力.md` §4.2（`SessionService` + `AgentState` 的限定用途 / gate 分段与确认回调 / 为何不做跨进程恢复） |
| §3.7 能力节点子会话隔离 | `FactoryAgent/FactoryAgent-流程编排能力.md` §6.2 |
| §4 M2 上下文窗口记忆 | `AgentToken成本优化-设计与实现方案.md` §6-7（Prompt Caching / `Offloader` 压缩 / 早停 Middleware） |
| §5 M3 证据链 | `Agent可观测性-设计与实现方案.md` §7（trace 落库表族 / 证据链回溯） |
| §6 自治 跨会话缓存 | `AgentToken成本优化-设计与实现方案.md` §8（工具结果缓存 / 语义缓存） |
| §7.3 Permission 确认记录 | `FactoryAgent/FactoryAgent-流程编排能力.md` §4.2、`架构设计v2/00-架构决策记录ADR.md` ADR-04（Token 签发与校验，主闸在工具网关） |
| §7.2 失败计数 | `FactoryAgent/FactoryAgent-流程编排能力.md` §6.1（能力节点连续失败转人工待办） |

---

> **一句话总结**：factorybot Agent 的记忆是五层分层架构--M1 工作记忆（`SessionService` + `AgentState`，**只管会话内上下文与多轮记忆，不做挂起续跑**）、
> M2 上下文窗口记忆（压缩 + prompt 缓存）、M3 证据链长期记忆（全文落库审计）、
> 自治 跨会话缓存（版本化精确缓存）、L5 会话元数据 & 令牌（生命周期 + 写动作闸门）。
> 核心哲学"模型看摘要，trace 落全文"贯穿 M2↔M3；版本一致性红线贯穿 M1->M4；时变性红线决定 M4 缓存策略。
