# FactoryAgent —— 一运行时三能力（1 Runtime + 3 Capabilities）架构定义

> **本文定位**：把"一个 Agent、三项能力"从口号收敛成**可评审的边界定义**——运行时共享什么、能力独有什么、两者之间的契约是什么、什么条件下才允许拆成独立服务。
> **与既有文档的关系**：[总体设计与实现方案](FactoryAgent-总体设计与实现方案.md) §3/§4 给架构全景与包结构，[统一架构与流程图](FactoryAgent-统一架构与流程图.md) 给三种能力形态的流程图，本文只做一件事：**定义 runtime 与 capability 的接缝**，不重复图和流程。选型理由见 [能力边界与选型决策卡](FactoryAgent-能力边界与选型决策卡.md)，是否该拆多 Agent 见 [车间 MES 中的 Multi-Agent：判据、模式与真实业务场景详解](../车间MES的Multi-Agent真实业务场景详解.md) 的 §2 判据、§3 两类模式。
> **口径纪律**：本文是**设计定义**，不声明"已上线"；代码事实以仓库为准，效果数字以离线评测集为准，不在文档里编。

---

## 0. 要点结论

1. **"多"的是决策实体，不是部署单元**：三个能力共用一个进程、一套 `AgentRuntime`、一张 `Toolkit`、一个 `SessionService`（`AgentState` 持久化）。
2. **能力差异是装配差异**，不是运行期 `if capability == ...`：`flows` 在启动时按能力装配一次，运行时只按 key 取装配好的 flow 步骤 / 能力节点（应用层 Python 代码，框架不提供协同原语）。
3. **必须隔离的四项是上下文、状态、工具集、写权限与预算**；其余（进程、连接池、中间件、可观测）共享。
4. **共享但必须分维**：缓存键、限流配额、`SessionService` 命名空间、trace 属性、指标标签、confirmation token 作用域，都要带 `capability`。
5. **能力之间只传结构化 artifact，不传对话历史**——这是"同进程"与"跨进程"唯一的语义差别，也是将来能无痛拆分的唯一前提。
6. **拆进程的判据是运维属性**（扩缩容 / 发布 / 故障隔离 / 依赖 / 安全 / 组织），不是"它们做不同的事"。

---

## 1. 三层坐标

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ L1 运行时 Runtime（1 个，进程级，进程内唯一）                              │
│    FastAPI 入口 / 事件订阅 · AgentRuntime · Toolkit · Permission 策略      │
│    SessionService · AgentState · ACL 客户端 · Model 抽象                    │
│    middleware(脱敏/压缩/trace) · metrics · Permission gate · dispatcher     │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ 按 capability 取：spec + flow 步骤/能力节点 + tool 切片
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌───────────────┐      ┌───────────────┐      ┌───────────────┐
│ L2 能力①      │      │ L2 能力②      │      │ L2 能力③      │
│ diagnosis     │      │ drafting      │      │ orchestration │
│ 只读 / 20 步  │      │ 只产 Draft    │      │ 受限写 / 40 步│
│ 60s           │      │ 8 步 / 90s    │      │ 300s/段       │
└───────┬───────┘      └───────┬───────┘      └───────┬───────┘
        │                      │                      │
        ▼                      ▼                      ▼
┌───────────────┐      ┌───────────────┐      ┌───────────────┐
│ L3 运行时形态  │      │ L3 运行时形态  │      │ L3 运行时形态  │
│ open_react    │      │ fixed_chain   │      │ flow_code     │
│ model↔tool    │      │ 取证据→检索→生成│      │ 确定性步骤+能力节点│
└───────────────┘      └───────────────┘      └───────────────┘
```

**判据**：任何一段代码写在 L1 还是 L2，只问一句——**"它是否随能力不同而不同"**。不同 → L2；相同 → L1。写在 L1 却按能力分支，就是边界没切干净。

---

## 2. L1：运行时共享清单

| 组件 | 职责 | 注意 |
|---|---|---|
| FastAPI 入口 | `/agent/diagnose`、`/agent/draft`、`/agent/orchestration/{scenario}/start`、`/agent/orchestration/{session_id}/advance`（审批后的确认回调，起下一段） | 只做协议，不含业务判断 |
| 事件订阅 | `DomainEventTrigger`：`equipment.fault`→编排/`fault_response`、`process.route.activated`→草稿/`SOP`、`defect_rate.spike`→诊断/`auto` | 只调 `invoke()`，不复用 flow 内部逻辑 |
| `AgentRuntime` | 能力注册表、flow 装配、会话驱动、能力节点调用、超时与步数兜底 | 唯一能 `await Agent 的 reply` 的地方 |
| `Toolkit` | 按 `capability + tenant` 双维下发工具切片 | 切片是权限，不是便利 |
| `Permission` | 启动四类断言：只读 / 无写 client / 写需确认 / 无放行工具 | 失败即进程起不来 |
| `SessionService` | 建会话、写步骤、收口状态 | 每行必带 `capability` |
| `AgentState` | 显式状态 + 框架 `Storage`（本项目选 `AsyncSQLAlchemyStorage` 落 **MySQL**；`SessionService` 是其上门面）持久化，**仅会话内上下文与多轮记忆**（不做跨闸门续跑） | `session_id` 唯一，跨能力不共用会话 |
| ACL 客户端池 | 过点/工艺/物料/设备/工装/遥测 + 写 client | 写 client 只下发给有写权限的能力 |
| `Model` 抽象 | provider 可插拔、结构化输出（`Content Block`）强约束 | 按能力限并发，避免一个能力打满 |
| middleware | 脱敏、结果压缩、trace 注入 | 配置按能力可覆盖，不用全局常量 |
| Permission gate | Redis：token 绑定 `action:target` + 过期 | token 必须绑定 session + capability |
| `ActionCardDispatcher` | 事件系统 + `AGUIProtocolMiddleware` 实时 + Kafka 持久兜底 | 投递失败进死信，不阻塞 flow 步骤 |
| 可观测 | OTel + prometheus；一次请求一个 `trace_id` | span 必挂 `capability` 与 `session_id` |

### 2.1 共享但必须分维（最容易被写漏的六处）

| 项 | 必须带的维度 | 漏了会怎样 |
|---|---|---|
| 工具结果缓存 key | `capability` + `tenant` + 版本锚点 | 编排读到诊断的旧缓存，版本串错 |
| LLM 并发/配额 | `capability` | 编排长驻占满 worker，诊断全部超时 |
| `SessionService` 会话键 | `session_id`（不跨能力复用） | 两个能力撞同一会话，状态互相覆盖 |
| trace 属性 | `capability`、`session_id`、`parent_session_id` | 无法回放"编排里调诊断"的那一段 |
| 指标标签 | `capability`、`node_type`、`scenario` | 无法回答"哪个能力退化" |
| confirmation token 作用域 | `session` + `step` + `action:target` | 一张卡被另一个会话复用 |

---

## 3. L2：能力独有清单

```python
class CapabilitySpec(BaseModel):
    name: str                        # diagnosis / drafting / orchestration
    flow_shape: FlowShape            # open_react / fixed_chain / flow_code / fanout_aggregate
    writes_allowed: bool             # 诊断 False / 草稿 False / 编排 True
    step_budget: int                 # 步数与工具调用预算上限：20 / 8 / 40
    segment_timeout_s: float         # 单次调用的计算超时：60 / 90 / 300（**不含等人确认**，等确认在工单侧计时）
    tool_tags: list[str]             # ["read:pass", "read:process"] 等
```

一个能力"独有"的完整清单：

| 独有项 | 内容 | 为什么不能共享 |
|---|---|---|
| flow / 能力节点实例 | `diagnosis`（统一 `Agent` 循环） / `draft_chain`（`Agent` + 策略，应用层顺序 `await`） / `orchestration`（`flows/` 应用层代码 + 能力节点） | 形态不同，共享等于写死一种 |
| State 类型 | 各自 `AgentState`（Pydantic）；flow 步骤不读写父会话未声明键 | 共享 schema 会静默串字段 |
| system prompt | 版本化，按能力存放 | 混在一起会角色串扰 |
| 工具切片 | `toolkit.tools_for(capability, tenant)` | 共享 = 诊断拿到写工具 |
| 写权限 | `writes_allowed` + 工具级 `requires_confirmation`/`writes_via` | 共享 = 红线靠自觉 |
| 预算 | 步数与工具调用预算上限、会话超时、token 配额 | 共享只能取最严或最松 |
| 会话命名空间 | `(capability, session_id)`；编排调子能力用 `parent_session_id` | 共享 status 会互相污染 |
| 策略/Builder | 草稿的 `rework_order`/`eight_d`/`sop`/`disposition_card`/`isolation_card` | 策略是能力的内部实现 |

### 3.1 能力矩阵

| 维度 | 诊断 `diagnosis` | 草稿 `drafting` | 编排 `orchestration` |
|---|---|---|---|
| 运行时形态 | `open_react`（model ↔ tool 循环） | `fixed_chain`（4 步骤直线） | `flow_code`（确定性步骤 + 能力节点 + barrier + gate） |
| 是否开放规划 | 是（取证路径由中间结果驱动） | 否 | 否（步骤顺序显式写在代码里） |
| 工具权限 | 只读 | 无写 client | 受限写（`writes_via`） |
| 产物 | `DiagnosisReport`（假设 + 证据引用） | `Draft`（`requires_confirmation` 恒真） | `ActionCard` / `GateDecision` / session 状态 |
| 步数 / 单次调用超时 | 20 / 60s | 8 / 90s | 40 / 300s（**不含等人确认**，等待期在工单侧） |
| 入口 | `POST /agent/diagnose` | `POST /agent/draft` | `POST /agent/orchestration/{scenario}/start` |
| 场景枚举 | 自由诊断 / `auto` | `draft_kind` | `changeover` / `fault_response` / `complaint_8d` / `process_change` |
| 分段终点 | 无 | 无（返回草稿等前端确认） | gate 步骤：产 ActionCard + 落成 MES 工单/待办实体，**本段 return**（不挂起；审批后由 `/advance` 起下一段） |
| 评测对象 | 诊断能力 | 草稿编写能力 | 流程编排能力 |

> 上表是三项**内置能力**。另有**运行时形态④ `fanout_aggregate`**（中心化只读聚合：代码统筹 + 单点裁决 + 多视角并发取证），落地为独立 capability 条目 `aggregate`（语义上属"诊断族"，因 `flows` 按 capability 装配而单列 key），定义见 [中心化校验的 fan-in 归并（应用层代码）](FactoryAgent-中心化聚合flow.md)。它**不是第四层**：同一套只读工具切片、同一条只读断言、同一种产物（证据 + 假设），只多一段 fan-out/fan-in 归并代码和一份 `EvidencePack` 契约。并发是 `asyncio.gather` 各自独立的 `Agent.reply()`，归并与裁决是应用层代码，不是框架协同原语（2.0 的 `MsgHub` / `sequential_pipeline` / `fanout_pipeline` 已删除）。

---

## 4. L1 与 L2 之间的契约

### 4.1 唯一入口

```python
async def invoke(
    capability: str,
    payload: dict,
    tenant: TenantContext,
    parent_session_id: str | None = None,
) -> dict
```

四件事在 `invoke` 内固定完成，能力实现不得绕过：

1. 建会话（带 `capability`、`parent_session_id`）；
2. 取 `spec` 设定**步数与工具调用预算上限**与 `wait_for` 超时；
3. 用 `session.id` 作为 `AgentState` 的会话键，由 `SessionService` 加载/落回；
4. 异常归一（超时/超步数 → `TIMEOUT` + 转人工，不抛栈到 API 层）。

### 4.2 三种调用形态，同一套契约

| 调用方 | 形态 | 关键约束 |
|---|---|---|
| 工程师 UI / 外部系统 | 能力即入口 | 只传 `payload` + `tenant` |
| 编排 flow 的非确定步骤 | 能力即能力节点调用 | 必带 `parent_session_id`，同 trace |
| 事件订阅器 | 能力即被触发方 | 从事件 envelope 取 `TenantContext`，不带写命令 |

### 4.3 artifact 契约（能力之间只传这个）

| 方向 | 传什么 | 明确不传 |
|---|---|---|
| 诊断 → 草稿 | `DiagnosisReport`：假设排序、证据引用、`evidence_set_id`、置信度 | ReAct 的 message 历史、工具原始返回 |
| 编排 → 诊断 | 结构化异常入参（工单/批次/设备/缺陷码 + 版本锚点） | 编排的 state 全量 |
| 草稿 → 编排 | `Draft`：`intent` + `payload` + `requires_confirmation` | Builder 中间步骤 |
| 编排 → 外部 | `ActionCard`（责任角色 + 证据 `trace_id`） | 模型原始输出 |

> 判据：**artifact 必须可序列化、可审计、可引用**。做不到，说明它本质是对话，不应该跨能力传。

---

## 5. 十条硬约束

1. 能力之间只通过 `invoke()` + artifact 交互，**不直接 import 另一个能力的 flow 或内部函数**。
2. **不共享可变单例**：barrier 状态、编排装配缓存、middleware 实例若必须存在，按 `session_id` 隔离。
3. 每个能力独立 State 类型；flow 步骤**不读写父会话未声明的键**。
4. `writes_allowed=False` 的能力**不得持有写 client**（启动断言 ②）。
5. 写工具必须 `requires_confirmation=True` 且声明 `writes_via`（启动断言 ①）。
6. 放行/拦截类工具**不注册到任何能力**（启动断言 ③）。
7. 草稿**只产 Draft 不落库**；落库走正常应用服务 + 人工确认。
8. 跨能力会话**不共享 `status` 字段**，用 `parent_session_id` 表达从属。
9. 超时与步数上限**按能力取值**，禁止全局常量兜底。
10. 缓存、配额、指标、trace **一律带 `capability` 维度**。

---

## 6. 一条请求怎么走（以客诉 8D 为例）

```text
POST /agent/orchestration/complaint_8d/start
  └─ runtime.invoke("orchestration", payload, tenant)
       session=S1(capability=ORCHESTRATION, scenario=COMPLAINT_8D, trace=T1)
       ├─ 确定性步骤：拉工单/批次/缺陷码（无 LLM）
       ├─ 确定性步骤：判定隔离范围门 → gate
       │     gate 产出 ActionCard 并落成 MES 隔离待办 → 事件系统推质量工程师 + Kafka 兜底
       │     ★ 本段在此 return：不挂起、不占执行槽（责任人分派/催办/超时在工单侧）
       │     （工单审批完成）POST /advance → 网关一次性 token → 重新发起下一段（重查当下事实）
       ├─ 能力节点（下一段，确认后起）：runtime.invoke("diagnosis", 异常入参, parent_session_id=S1)
       │     session=S2(capability=DIAGNOSIS, parent=S1, trace=T1)
       │     └─ ReAct 取证 ≤10 次工具调用 → DiagnosisReport
       ├─ 能力节点：runtime.invoke("drafting", {kind: EIGHT_D, report: ...}, parent=S1)
       │     session=S3(capability=DRAFTING, parent=S1, trace=T1)
       │     └─ 取证据 → 检索 SOP → 结构化生成 → Draft(requires_confirmation)
       └─ 收口：Draft 返回前端，人工编辑后走应用服务落库
```

一次请求一条 trace，三个 session 用 `parent_session_id` 串成树——这就是"一个 Agent"在可观测上的直接体现：**能看到编排第 3 步调起的诊断循环的每一次工具调用**。gate 之后的段是新一次调用，靠同一个 `session_id` 归回同一棵树（不靠快照）。

---

## 7. 启动断言（进程起不来比带病上线好）

| # | 断言 | 失败含义 |
|---|---|---|
| ① | 所有 `read_only=False` 工具必须 `requires_confirmation` 且有 `writes_via` | 写路径可能旁路应用服务 |
| ② | `writes_allowed=False` 的能力不得持有写 client | 只读能力可写 |
| ③ | 任何能力都不得注册放行/拦截类工具前缀 | 碰过点红线 |
| ④ | 各 capability 的**写**工具集互斥（一个写工具不能同属两个能力）；只读工具允许跨能力共享（如 `diagnosis` 与 `aggregate` 共用只读切片） | 写权限被复制 / 权限切片被破坏 |
| ⑤ | 编排 flow（`flows/` 应用层代码）自身不持工具（只有能力节点持） | 编排变成开放 ReAct |
| ⑥ | `flow_shape=fanout_aggregate` 的能力必须用 `asyncio.gather` 并发各自独立的 `Agent.reply()` 做 fan-out，且 fan-in 归并与裁决在应用层代码、子会话 spec 继承 `writes_allowed=False` | 聚合退化成"多个可写 Agent 各自落库" |

---

## 8. 可拆分性（Splittability）预留

拆分不是架构目标，是运维手段。下列六条**任一成立**才考虑把某个能力拆成独立服务：

| 触发条件 | 典型场景 |
|---|---|
| 资源画像冲突 | 编排的分段点火 + `/advance` 回调是突发批量（开工集中在换线窗口），诊断是秒级交互请求，两者扩缩容曲线不同 |
| 独立发布节奏 | 草稿 Builder/prompt 高频迭代，编排 flow 稳定 |
| 故障隔离 | 某个模型供应商不可用时不拖垮整条链 |
| 依赖/语言冲突 | 草稿需要 GPU 侧模型、或 Java 侧 Agent |
| 安全边界 | 不可信输入与有写权限的 Agent 必须物理隔离 |
| 组织边界 | 第三方 Agent / 不同 owner |

**拆分时只允许换 transport**：能力间调用（`invoke()`）→ HTTP/Kafka，逻辑不改、契约不改。这就要求现在守住第 5 节的第 1、2、3 条。

**两条已发生过的反面案例**（说明共享可变状态的真实代价）：

- `barrier` 跨场景复用，PROCESS_CHANGE 跌进换线的 kitting 分支，会话被污染为 `WAITING`；
- `retry_tooling` 死代码，gate REJECT 后的重试回路不可达，评测集里的重试用例必然失败且失败原因误导。

两者都不是"耦合太紧"，而是**同进程里的隐式共享**。

---

## 9. 与评测的映射

| 层 | 被测对象 | 数据源 |
|---|---|---|
| 路由 / 入口 | 能力选得对不对（**入口网关的意图路由**；三项能力本身不承担） | `route_taken` / `intent` |
| 工具调用 | 选工具、装参数、顺序 | `tool_call_traces` |
| 轨迹 | 里程碑、步数冗余、收敛 | span 序列 + `agent_step_record` |
| 安全 | 写越界、版本锚点、租户、放行工具 | 启动断言 + 步骤审计 |

三点要求：

1. **指标按 `capability` 维度切片**，否则"诊断退化"和"草稿退化"在总分里无法区分。
2. **跨能力 case（8D、换线 mismatch）需要额外断言 artifact 完整性**：字段齐、版本锚点在、证据引用可解析。同进程时由 Pydantic 免费提供，拆进程后成为必须显式测试的内容。
3. **门禁按对象切，不按进程切**：现在是 1 套 harness + 6 个 `object_type`；拆成 3 服务后会变成 3 套门禁 + 跨能力组合门禁，归因成本显著上升。

---

## 10. 五种反模式

| 反模式 | 现象 | 后果 |
|---|---|---|
| 万能 Agent | 一个 ReAct + 全部工具 + 一个 prompt | 工具 20+ 选不准、权限收敛不了、60s 的诊断与 300s 的编排段挤在同一预算里 |
| 伪统一 | 一套 runtime，但能力之间直接 import 内部函数、共享 session.status | 拆分时才发现是分布式单体 |
| 层级继承 | 编排"调用"草稿"调用"诊断，高层拿低层能力、接不上就重写一份 | 8D 这类横跨三层的需求被迫实现两遍 |
| 全局常量兜底 | 一个步数与工具调用预算上限、一个超时、一个缓存 key | 最严的拖死最松的；版本锚点串错 |
| 进程级拆分起步 | 一上来三个微服务 | 三套 gate/会话/指标/评测口径，调试图谱化 |

---

## 11. 演进路径

| 阶段 | 动作 | 拆分判断 |
|---|---|---|
| 1 | 诊断（纯只读）起量，1 runtime + 1 capability | 不拆 |
| 2 | 加草稿（无写 client），共用 registry/session/trace | 不拆（同进程零边际成本） |
| 3 | 加编排（受限写 + gate + HITL 确认） | 不拆；若出现"长驻饿死突发"，**只拆编排** |
| 4 | 能力数继续增长 | 优先按"资源画像"拆，不按业务概念拆 |
| 5 | 多模块只读聚合需求：加运行时形态④ `fanout_aggregate`（先 2 路视角，probe 动态收敛） | 不拆；只新增 `CapabilitySpec` 条目 + 一段 fan-in 归并代码，进程与会话表不动。见 [中心化校验的 fan-in 归并（应用层代码）](FactoryAgent-中心化聚合flow.md) §11 |

**顺序理由**：能力一个比一个风险高（只读 → 无写 client → 受限写），基础设施却在第一步就位，后两步只加 `CapabilitySpec` 和 flow 装配。

---

## 12. 一句话总结

> **一个进程、一套运行时、一张工具注册表（`Toolkit`）、一套 `AgentState` + `SessionService`；三种内置运行时形态（另可按需装配 `fanout_aggregate`）、按能力分档的步数与工具调用预算、按能力切的工具切片、按能力签的产物契约。** 共享的是基础设施，隔离的是上下文、状态、权限与预算；能力之间只传可序列化的 artifact——这条守住了，今天是一个服务，明天拆三个服务，只是换一次 transport。
