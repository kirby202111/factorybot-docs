# FactoryAgent —— 中心化聚合：中心化校验的 fan-in 归并（应用层代码）（Fan-out / Fan-in Aggregate）定义与实现方案

> **本文定位**：定义**运行时形态④ `fanout_aggregate`**——"多模块只读聚合"这一类需求的运行时形态。回答四件事：它站在三层坐标的哪一格、子会话边界凭什么切、中心化统筹里哪些是代码哪些是 LLM、以及什么条件下**不该**这么做。
> **与既有文档的关系**：[单运行时三能力架构](FactoryAgent-单运行时三能力架构.md) §3.1/§5/§7/§8 给能力矩阵、十条硬约束、启动断言与可拆分性，本文是它在"聚合"这一形态上的**增量定义**，不改底座；[总体设计与实现方案](FactoryAgent-总体设计与实现方案.md) §7 给包结构，本文只给新增文件；[能力边界与选型决策卡](FactoryAgent-能力边界与选型决策卡.md) 的三问是本文"哪一步用 LLM"的唯一判据；多 Agent 的必要性判据以 [哪些场景真正需要 Multi-Agent 系统](../哪些场景真正需要Multi-Agent系统.md) 与 [Multi-Agent 必要性复核](../Multi-Agent必要性-独立复核与批判.md) 的三条硬判据（C1' / C3' / C8）+ 成本闸门为准。
> **口径纪律**：本文是**设计定义**，不声明"已上线"；代码事实以仓库为准，收益数字以离线评测集为准，不在文档里编。文中"子 Agent"一律指**同进程内的短命子会话**，不是独立部署的智能体。
> **本文不覆盖**：真正的"多专家会诊"（各子会话持**不同证据源或不同方法**，命中 C8 信息不对称）。那类形态要另立判据，见 §2.5。覆盖驱动的"复杂研究报告生成"（产物是可交付文档而非一个判断）见 [研究报告生成 flow（应用层代码）](FactoryAgent-研究报告生成flow.md)，它复用本文的取证段实现与 `EvidencePack` 契约。

---

## 0. 要点结论

1. **它是运行时形态④，不是第四个 Agent、不是新进程**：`CapabilitySpec` 新增一个条目 + 一段 fan-out/fan-in 归并代码（应用层 `asyncio.gather` + 显式合并），`AgentRuntime` / `Toolkit` / `Permission` / `SessionService` / trace 零改动。
2. **"中心化统筹"= 代码统筹 + 单点裁决**：LLM 只出现在两处（开放文本→查询计划的结构化分类器、fan-in 后的裁决）。**路由、join、去重、版本一致性校验全部是代码**——这几件事过三问皆否。框架不提供协同原语（2.0 已删除 `MsgHub` / `sequential_pipeline` / `fanout_pipeline`），本形态是**中心化校验的 fan-in 归并（应用层代码）**。
3. **子会话边界按"取证视角"切，工具注册边界按限界上下文切，两者解耦**。禁止"1 个子会话 = 1 个微服务"：服务边界是部署/事务/组织，Agent 边界是上下文污染与压缩比。
4. **收益只有三项**：上下文隔离（就地压缩）、延迟（真并行）、覆盖面（不遗漏）。**不含"判得更准"**——聚合场景里各分支之间没有信息不对称（C8 不成立），不会比单个 Agent 看得更准。各分支**只读同一份 `evidence_set_id`，彼此不看对方中间结论**——刻意如此，因为扇出式互看会放大错误（无中心校验 17.2× vs 中心化 4.4×）。
5. **两段式扇出**：先 `probe` 窄化，再按命中分支取证。不固定 N 路——固定扇出等于在死分支上烧钱。
6. **`gaps` 是一等公民，`confidence` 由规则算**：显式声明"我缺了哪一路"，比让模型自评置信度可靠。
7. **只读 + 写单线程**：子会话 `writes_allowed=False`，聚合产物只到 artifact；写意图回编排 gate 或草稿 confirmation。
8. **评测必须有单 Agent 顺序取证基线**：证明不了"更好"，就 `AGGREGATE_MODE=single` 回退。

---

## 1. 定位：站在三层坐标的哪一格

沿用 [单运行时三能力架构](FactoryAgent-单运行时三能力架构.md) §1 的 L1/L2/L3 坐标：

```text
L1 运行时 Runtime（零改动）
   FastAPI / AgentRuntime / Toolkit / Permission / SessionService
   AgentState 持久化 / ACL 客户端池 / middleware / metrics / Permission gate
        │
        ▼ 按 capability key 取：spec + flow 步骤/能力节点 + tool 切片
L2 能力条目（新增一条，语义上属"诊断族"）
   diagnosis(open_react)   drafting(fixed_chain)   orchestration(flow_code)
   aggregate(fanout_aggregate) ← 只读取证，与 diagnosis 共享只读工具切片与断言
        │
        ▼
L3 运行时形态④ fanout_aggregate
   normalize → plan → probe → fan_out(并发子会话) → fan_in → judge → report
```

**为什么单列一个 capability key，却又不叫"第四层"**：主文档 §0 结论 2 要求"`flows` 在启动时按能力装配一次，运行时只按 key 取装配好的 flow 步骤 / 能力节点"。把两种形态塞进 `diagnosis` 一个 key，就得在运行期按 `scenario` 分派，等于违反这条纪律。所以落地形态是**新增 capability 条目**；但在**架构语义**上它属于"诊断族"——同一套只读工具切片、同一条只读断言、同一种产物（证据 + 假设），不是新的自主度等级。

### 1.1 术语纠偏（评审时最先被追问的四个词）

| 常见叫法 | 本项目口径 | 理由 |
|---|---|---|
| Multi-Agent System / MAS（Centralized） | **中心化校验的 fan-in 归并（应用层代码）** `fanout_aggregate` | 收益来自上下文隔离与并行，不来自"多个 Agent"。按严格定义它多数时候只是"上下文隔离技巧 + 真并行扇出" |
| 主 Agent / Supervisor Agent | **代码统筹段（普通 Python 顺序 `await`）+ 单点 judge 能力节点** | 让 LLM 自由路由既不稳定又贵；路由是确定性查询计划（§3.3） |
| 子 Agent = 一个微服务 | **子会话 = 一个取证视角** | 见 §2 |
| 聚合结果给主 Agent 决策 | **artifact 交给 judge 排序，写意图回单线程** | 多个子 Agent 各写各的 = 并发写冲突（Cognition 反例） |

---

## 2. 边界切分：为什么不是"1 个子 Agent = 1 个微服务"

### 2.1 两种边界不是一回事

| 维度 | 微服务 / 限界上下文边界 | Agent 边界 |
|---|---|---|
| 由什么决定 | 数据所有权、事务边界、发布节奏、团队归属 | 上下文是否会被污染、证据是否真独立、压缩比是否值得 |
| 变了要改谁 | 服务契约、数据库、K8s 编排 | flow 代码、prompt、`EvidencePack` 契约 |
| 一个的粒度 | 上下文数量（本项目 14 个） | 一次任务分解出的子问题数（常常 2~5 个） |
| 对齐关系 | **工具注册边界 = 上下文边界** | **Agent 边界 = 任务分解边界** |

> 与 [MES-Multi-Agent 架构适用性分析](../MES-Multi-Agent架构适用性分析.md) §3 的不变式一致：**工具注册对齐限界上下文，但这不等于任务分解边界**——一个子 Agent 可以跨多个上下文取只读工具。

### 2.2 "1:1 映射微服务"的四个后果

| 后果 | 具体机制 |
|---|---|
| **一条证据链被切成三段** | 追一个批次不良，`batch_id` 在物料、过点、检验三个上下文都有。按服务切 → 三段各回一段 → 谁来 join？只能让主 Agent 的 LLM 做 join。**join 是代码活，交给概率系统是负资产**（三问全否，见 [选型决策卡](FactoryAgent-能力边界与选型决策卡.md) §1） |
| **架构被运维拓扑绑架** | 服务一合并/一拆版本 flow 代码就得改，而主文档 §8 明确：**拆分是运维手段，不是架构目标**。让架构跟随运维，等于把六条拆分判据废掉 |
| **凭空造出没有 LLM 的"Agent"** | 若某个服务只被"查一次接口再格式化"，它是 **tool**，不是 Agent（把求解器、SPC 判定叫 Agent 是同类错误）。这类节点应是 `node_type=CODE` |
| **固定数量没有依据** | 子任务数应随问题定：查单个工单齐套可能 1 路，客诉 8D 可能 5 路。固定 3 路 = 在死分支上花钱（复核 §P1-2：RCA 是序贯假设驱动，不是固定六路并行） |

### 2.3 正确切法：按"取证视角"切

聚合类需求的三个视角天然**真独立**（去掉一路不影响另一路的结果），且**各自是一种压缩方式**：

| 视角 | `role` | 装什么 | 压缩形态 | 典型工具（只读） |
|---|---|---|---|---|
| 事实链 | `FACT` | 过点记录 / 在制位置 / 工单进度 / 批次谱系 | 事件序列 + 里程碑 | `query_pass_records` / `query_wip_position` / `trace_lookup` |
| 条件链 | `CONDITION` | 设备参数时序 / 工艺版本与审计 / 点检计量状态 | 时间窗统计（漂移、越限次数），**不是原始曲线** | `query_equipment_telemetry` / `query_process_route`（强制版本） / `query_asset_status` |
| 检验链 | `INSPECTION` | 测试数据 / 检验记录 / 历史客诉 | 分布 + 异常样本 | `query_test_results` / `query_repair_history` / `search_docs` |

这三个视角常跨 6~8 个上下文——**这恰好证明"视角数 ≠ 服务数"**。

### 2.4 两条可执行的合并/降级判据

| 判据 | 动作 |
|---|---|
| 两个子 Agent 的**工具重叠 > 50%** | 合并。它们看到大半是同一批东西，拆开就是一个 Agent 花了两份钱 |
| 某个子会话内 **LLM 调用 = 0**，或只做格式化 | 降级为确定性 flow 步骤，`node_type=CODE`，指标可验证 |

### 2.5 与"多专家会诊"的界线（本文不覆盖）

如果拆分的动机是**某个角色能拿到另一方可证实地拿不到的外部证据或方法**（统计检验 vs 语义检索、独立数据源对账），那是复核文档的 **C8 判据**，形态不再是"聚合"而是"红队 / 对账"。本文的形态**只解决覆盖与压缩**，不要拿它冒充"质量提升"。两者可以并存（聚合扇出 → 其中一路改由外部证据法对账），但判据、评测、成本闸门要分开记。

---

## 3. 运行时形态：代码统筹 + 单点裁决

### 3.1 flow 步骤序列

```text
invoke("aggregate", payload, tenant, parent_session_id=?)      ← 复用唯一入口，不新开协议
  session=S1(capability=AGGREGATE, scenario=<...>, trace=T1)
  │
  ├─[CODE] normalize   输入 → 结构化锚点：{work_order|batch|asset|serial} + 时间窗 + version_anchor
  ├─[CODE] plan        锚点 → SubtaskSpec[]（查哪个视角、什么过滤、什么时间窗）★ 非 LLM 映射表
  ├─[CODE] probe       每视角一次 cheap 探测（有无数据 / 量级 / 时间窗覆盖）★ 窄化，不做全量取数
  ├─[CODE] fan_out     仅对命中的视角并发取证（`asyncio.gather` 并发各自独立的 `Agent.reply()`；每路独立 session / AgentState / 预算 / 超时）
  │     ├─ session=S1.a(capability=AGGREGATE, parent=S1, role=FACT)
  │     ├─ session=S1.b(..., role=CONDITION)
  │     └─ session=S1.c(..., role=INSPECTION)
  │           每路只回 EvidencePack，不回 message 历史、不回原始大表
  │           各分支只读同一份 evidence_set_id，彼此不看对方的中间结论
  ├─[CODE] fan_in      join / 去重 / 版本锚点一致性校验 / 冲突登记 ★ 非 LLM（应用层显式合并）
  ├─[LLM ] judge       仅在 EvidencePack 上：假设排序、归因、冲突裁决
  └─[CODE] report      EvidenceBundle + CoverageGap 汇报 → 上层 DiagnosisReport / 编排能力节点消费
```

### 3.2 步骤清单（`node_type` 可落库验证）

| 步骤 | `node_type` | 职责 | 可跳过 |
|---|---|---|---|
| `normalize` | CODE | 锚点归一、租户上下文、时间窗、版本锚点占位 | 否 |
| `plan` | CODE | 锚点 + scenario → `SubtaskSpec[]`（查什么、滤什么） | 否 |
| `probe` | CODE | 每视角一次 count / 有无探测 | 可（`fanout.probe_enabled=False`） |
| `fan_out` | CODE | 并发创建子会话，汇总 `EvidencePack` | 否 |
| `fan_in` | CODE | join、去重、版本一致性、冲突登记 | 否 |
| `judge` | **AGENT** | 假设排序、归因、冲突裁决 | **证据齐全且场景可确定汇总时可跳过**（纯确定性汇总，LLM 调用为 0） |
| `report` | CODE | artifact 拼装、gaps 汇报、`need_human_review` 标记 | 否 |

**可验证标志**：`agent_llm_invocation_total{capability="aggregate"} = 0` 且 `agent_step_total{capability="aggregate",node_type="CODE"}` 正常增长——说明确定性段没有被交给模型（与主文档 §1 "换线全程 PASS 时 LLM 调用为 0"同一口径）。

### 3.3 LLM 只出现两次，其余一律代码

| 位置 | 允许 / 禁止 | 形态 |
|---|---|---|
| 开放自然语言输入 → `SubtaskSpec[]` | ✅ 允许 | **受限分类**（`Content Block` / Pydantic BaseModel 承载）：枚举里挑视角 + 填锚点。**不是**自由 ReAct，不能自选工具、不能增删视角 |
| `fan_in` 后 → 假设排序 / 归因 / 冲突裁决 | ✅ 允许 | 输入只有 `EvidencePack[]`，输出必须引用 `item_id`（引不出即判失败重试） |
| 决定"要不要查哪个上下文" | ❌ 禁止 | `plan` 是锚点→视角的映射表，代码写得出来 |
| 把两路证据 join 成一条链 | ❌ 禁止 | 代码 join；LLM 只在 join 后的结果上排序 |
| 生成处置动作 / 写意图 | ❌ 禁止 | 写意图回编排 gate 或草稿 confirmation（写单线程） |
| 自评置信度 | ❌ 禁止 | 规则计算（§4.2） |

### 3.4 两段式扇出（probe → targeted fan-out）

```text
阶段 A（窄化，成本 ~O(视角数)，无 LLM）
  对每个视角只问三件事：有没有数据 / 大概多少条 / 是否覆盖目标时间窗
  → 未命中：直接写 gaps（NO_DATA / OUT_OF_WINDOW），不进阶段 B

阶段 B（取证，只对命中视角并发）
  每路拿 probe 返回的 scope 做定向取数，压缩成 EvidencePack
```

**动机**：复核 §P1-2 指出盲目 fan-out 会在不可能的分支上浪费取证，且无效结果还会挤占后续上下文。**扇出宽度是动态的，不是固定 3 或 6。**

**收敛条件**（任一满足即停）：假设集合已覆盖到 top-k 且证据互不推翻；再查一路的边际信息 < 阈值；预算用尽（此时必定产出 gaps 与 `need_human_review`）。

---

## 4. 契约：先写 `EvidencePack`，再写 flow 代码

fan-in 类实现十有八九死在"子会话回了一坨非结构化文本"。这个契约先落地。

### 4.1 模型

```python
# app/domain/evidence.py（在既有证据模块内扩，不新建平行体系）
class CoverageGap(BaseModel):
    kind: Literal["NO_DATA", "OUT_OF_WINDOW", "VERSION_MISMATCH",
                  "TRUNCATED", "PARTIAL_FAILURE", "SCOPE_DENIED"]
    detail: str                      # "设备参数仅留存 7 天，本次窗口 14 天"
    ref: str | None = None

class EvidenceItem(BaseModel):
    item_id: str                     # 可引用 ID；judge 只能引用它，编不出证据
    source: str                      # bounded_context + tool
    ref: str                         # pass_record_id / batch_id / route_version / serial_no
    value: dict[str, Any]            # 已压缩字段，不是原始行
    version_anchor: dict[str, str]   # route_version / bom_version / recipe_version
    captured_at: datetime
    tenant_id: str

class EvidencePack(BaseModel):
    subtask_id: str
    role: Literal["FACT", "CONDITION", "INSPECTION", "CUSTOM"]
    question: str                    # 这一路到底在回答什么
    items: list[EvidenceItem]        # 上限受 policy 约束
    stats: dict[str, float] | None   # 时序/分布等确定性统计，不放原始曲线
    coverage: dict[str, Any]         # 时间窗、范围、全量 or 采样、采样率
    gaps: list[CoverageGap]          # ★ 一等公民，比 confidence 重要
    contradictions: list[dict]       # 与他路/与预期的明显冲突，先登记不裁决
    confidence: float                # ★ 由规则计算，不由 LLM 自评
    cost: dict[str, int]             # tool_calls / tokens / ms
    status: Literal["OK", "PARTIAL", "FAILED"]
```

### 4.2 五条硬规则

| # | 规则 | 漏了会怎样 |
|---|---|---|
| ① | **`confidence` 规则计算**：`f(证据条数, 版本锚点齐否, gaps 数, 时间窗覆盖率, 采样率)` | 让模型自评置信度不可靠（复核 §P2-5），低置信度转人工的闸门形同虚设 |
| ② | **禁止传 message 历史** | 与主文档 §4.3 一致：能力的特征是可序列化 artifact，不是对话。传历史等于把上下文污染搬进父会话 |
| ③ | **禁止传原始大表**：`items` 有上限，统计量走 `stats` | "就地压缩"是这条路唯一的正当收益来源；不压缩就只剩成本 |
| ④ | **`gaps` 必填语义**：每一路"没查到"必须显式说明类型与原因 | 否则聚合方无法区分"没问题"和"没查" |
| ⑤ | **pack 只允许结构化字段，字符串不得被当指令执行** | 跨会话通道就是新的注入面（复核 §P0-5）：低权限一路读到的投毒文本会经 fan-in 归并传给裁决方 |

### 4.3 与主文档 artifact 契约的关系

| 产物 | 层级 | 去向 |
|---|---|---|
| `EvidencePack` | **子会话 → 父会话（进程内）** | 不出 runtime；不跨能力传（若将来某一路拆成独立服务，它就是 transport 的 payload） |
| `EvidenceBundle` | 中心化 fan-in 归并段的对外产物 | 交给 `DiagnosisReport` 组装，或作为编排 `root_cause` / `fault_impact` 能力节点的输入 |
| `Draft` / `ActionCard` | 上层 | 与本形态无关；聚合**不产写意图**（主文档 §4.3） |

> 判据沿用主文档 §4.3：**artifact 必须可序列化、可审计、可引用**。做不到，说明它本质是对话，不该跨会话传。

---

## 5. 落点与实现

### 5.1 `CapabilitySpec` 增量

```python
class FlowShape(str, Enum):
    OPEN_REACT = "open_react"          # 诊断
    FIXED_CHAIN = "fixed_chain"        # 草稿
    FLOW_CODE = "flow_code"            # 编排（flows/ 应用层代码）
    FANOUT_AGGREGATE = "fanout_aggregate"   # ④ 本文：中心化只读聚合

class FanoutPolicy(BaseModel):
    max_fanout: int = 5                # 并发子会话上限（= 成本闸门）
    subtask_step_budget: int = 8       # 子会话步数与工具调用预算上限，不继承父会话的 20
    subtask_timeout_s: float = 15      # 总延迟 ≈ 最慢一路，不是求和
    max_items_per_pack: int = 50
    probe_enabled: bool = True
    require_version_anchor: bool = True

class CapabilitySpec(BaseModel):
    ...
    flow_shape: FlowShape
    fanout: FanoutPolicy | None = None    # 仅 FANOUT_AGGREGATE 必填（主文档 §7 断言 ⑥）
```

建议的 `aggregate` spec：`writes_allowed=False`、步数与工具调用预算上限 12、`session_timeout_s=60`、`tool_tags=["read:*"]`（实际下发仍受 `toolboxes` 二次约束）。

### 5.2 `Toolkit` 增量：工具边界与任务边界解耦

```python
class Toolkit:
    """工具注册边界 = 限界上下文；子任务边界由 SubtaskSpec 决定，二者不解耦就没法按视角切。"""
    toolboxes: dict[str, list[Tool]]        # bounded_context -> tools（新增）

    def tools_for_subtask(self, spec: SubtaskSpec, tenant: TenantContext) -> list[Tool]:
        return [t for ctx in spec.contexts
                  for t in self.toolboxes[ctx]
                  if t.read_only and tenant.allows(t.required_tenant_scopes)]
```

要点：**子会话不按上下文命名**。`role=FACT` 的会话可以同时挂"过点 + 在制 + 谱系"三个上下文的只读工具——这正是"Agent 边界 ≠ 上下文边界"的代码体现。

### 5.3 文件落点（相对 `agent_service/app/`）

| 文件 | 内容 | 层级 |
|---|---|---|
| `domain/capability.py` | `FlowShape` 加枚举、`FanoutPolicy`、`CapabilitySpec.fanout` | domain |
| `domain/evidence.py` | `EvidencePack` / `EvidenceItem` / `CoverageGap` / `SubtaskSpec` | domain |
| `domain/tool.py` | `toolboxes`、`tools_for_subtask` | domain |
| `ai/aggregate_flow.py` | 运行时形态④装配（**建议文件名就叫 `aggregate_flow.py`**，别叫 `multi_agent_graph.py`；与主文档 §7 的 `ai/flow_steps/` + `ai/capability_nodes/` 分层一致） | ai |
| `ai/flow_steps/plan.py` | 锚点 → `SubtaskSpec[]` 映射表 | ai |
| `ai/flow_steps/probe.py` | 视角窄化探测 | ai |
| `ai/flow_steps/fan_out.py` | `asyncio.gather` 并发子会话（含 per-child 预算与超时） | ai |
| `ai/flow_steps/fan_in.py` | join / 去重 / 版本一致性 / 冲突登记（应用层显式合并） | ai |
| `ai/flow_steps/report.py` | `EvidenceBundle` 拼装 + gaps 汇报 | ai |
| `ai/capability_nodes/judge.py` | 唯一的非确定能力节点（假设排序 / 归因 / 冲突裁决） | ai |
| `ai/prompts/judge_v1.md` | 版本化 prompt；只允许引用 `item_id` | ai |
| `application/runtime.py` | **不改 `invoke()` 签名** | application |
| `infrastructure/obs/metrics.py` | 新增三个子任务指标（§8） | infra |

### 5.4 扇出实现的两点约束，提前规避

1. **扇出用 `asyncio.gather`，不用框架协同原语**
   - 框架侧的选择已经不存在：`MsgHub` / `sequential_pipeline` / `fanout_pipeline` 在 2.0 已删除，`GoalPipeline` / `TeamPipeline` 是 experimental 且明确"成员间不互相说话"，**观察、不进生产**。
   - 另有两点让框架原语即便存在也不合用：① 每路要独立 session 落 `agent_session` 时不好控制；② 分支内放 HITL 确认的续跑语义有 task 级限制。
   - **实现**：`asyncio.gather` 并发**各自独立**的 `Agent.reply()`（独立 session / `AgentState`、独立 `asyncio.wait_for` 超时），子会话内直调工具；失败用 `return_exceptions=True` 收口成 `PARTIAL` pack；合并与裁决在应用层代码显式完成。
2. **HITL 确认 / gate 一律放在 `fan_in` 之后**
   - fan-in 归并段本身**不含 gate**；需要人确认的动作在上层（编排 gate 或草稿 confirmation），挂起续跑用 `event.RequireUserConfirmEvent` + `SessionService` + `BackgroundTaskManager`。这条同时满足主文档 §7 断言 ⑤（编排 flow 自身不持工具）的同类精神。

### 5.5 `SessionService` 的并发要求（别在这里翻车）

- 一次聚合会建 **1 + N 个会话**（父 + 各视角）。要确认：建会话无全局锁、`parent_session_id` 有索引、各子会话的 `AgentState` 彼此独立；
- **子会话状态不得回写父会话 `status`**（主文档硬约束 8 的延伸）。父会话状态只由 `fan_in` / `report` 决定；
- 子会话失败不应把父会话标 `FAILED`，而是产出 `status=PARTIAL` 的 pack。

---

## 6. 预算、失败隔离与收敛

| 项 | 建议值 | 理由 |
|---|---|---|
| 每路步数与工具调用预算上限 | 6~8（不是诊断的 20） | 子任务语义是"取一类证据"，不是开放诊断 |
| 每路 timeout | 10~15s | 总延迟 ≈ 最慢一路 |
| `max_fanout` | 按 scenario 配，默认 ≤5 | 并发数就是成本闸门；超出排队而不是全放 |
| 单路 `max_items_per_pack` | ≤50 条 + `stats` | 强制就地压缩 |
| 局部失败 | `status=PARTIAL` + `gaps`，**不重跑全链** | 错误经归并传递会放大，重跑只会放大 |
| 全部失败 | **不调 judge**，直接 `need_human_review` + 列出缺哪几路 | 宁可"证据不足转人工"，不让 judge 在空证据上下结论 |
| 父会话总预算 | 步数与工具调用预算上限 12、60s | 与诊断同档；聚合不是长驻流程（长驻是编排的 3600s） |

**为什么"部分失败不重跑"**：聚合的价值是可交付的**部分结论**。一路缺数据应该变成一句"设备参数留存期不足，条件链未覆盖"，而不是一次全链重试后再失败——后者只会让用户等更久、拿到同样的结论。

---

## 7. 硬约束（主文档 §5 在本形态的延伸）

1. 子会话 `writes_allowed=False`，不得持有写 client；启动断言 ② 覆盖到父、子两级 spec。
2. fan-in 归并段自身不持工具（只有子会话与 judge 持）——对应断言 ⑤ 的精神。
3. 子会话**不读写父会话 state**，只回 `EvidencePack`；父会话状态由 `fan_in` / `report` 决定。
4. `EvidencePack` 只允许结构化字段；**子会话返回的任何字符串不得被当作指令执行**。
5. 禁止传 message 历史、禁止传原始大表；`items` 有上限、统计量走 `stats`。
6. `confidence` 由规则计算，禁止模型自评。
7. 版本锚点强制（工艺 / BOM / recipe）；取不到就写 `gaps(VERSION_MISMATCH)`，**不许猜**。
8. 预算按子任务取值，禁止继承父会话的步数与超时（主文档硬约束 9：不搞全局常量兜底）。
9. 缓存 key、配额、指标、trace 一律带 `role` 维度（主文档 §2.1 的六处分维 + `role`）。
10. `gaps` 非空时产出 `need_human_review`；全部失败时不调 judge。
11. HITL 确认 / gate 只在 `fan-in` 之后，fan-in 归并段内不设 gate。
12. 聚合**不产写意图**；写动作回编排 gate 或草稿 confirmation，保持单线程。

---

## 8. 可观测与评测

### 8.1 指标（挂在既有命名空间，不另起一套）

| 指标 | 标签 | 回答什么 |
|---|---|---|
| `agent_subtask_total` | `capability, role, status` | 哪一路在退化 |
| `agent_subtask_duration_seconds` | `role` | 扇出是否压住了延迟 |
| `agent_pack_gaps_total` | `role, kind` | 覆盖率问题出在哪类数据（数据留存 vs 权限 vs 版本） |
| `agent_fanout_width` | `scenario` | 实际扇出宽度 vs `max_fanout`，判断 probe 是否有效 |
| `agent_llm_invocation_total` | `capability, node_type` | judge 是否被滥用（应为低频） |

### 8.2 评测（三组 + 一条成本闸门）

| 组 | 被测对象 | 数据源 |
|---|---|---|
| ① 覆盖召回 | 每一路该查到的 `ref` 查到了吗 | `EvidencePack.items` vs 人工标注该查清单 |
| ② join 正确性 | 确定性合并是否漏链 / 串版本 | 单测 + 会话对比（这部分是代码，可单元测试） |
| ③ 裁决质量 | judge 的假设排序是否更接近人工已知根因 | `DiagnosisReport` vs 历史案例 |
| ④ 成本闸门 | 每成功任务成本增量 vs 质量提升 | token / 工具调用数 / 端到端时延 |

三点要求：

1. **必须有单 Agent 顺序取证基线**。没有基线，就无法区分"架构更优"和"多烧了算力"（复核 §P2-2 的算力混淆陷阱）。
2. **如果只有延迟改善，收益就写"延迟"，不许写成"质量提升"**。工程结论的诚实度在这里。
3. **按 `role` 切片看指标**，否则"条件链退化"和"事实链退化"在总分里无法区分（与主文档 §9 一致）。

---

## 9. 五种反模式

| 反模式 | 现象 | 后果 |
|---|---|---|
| **一服务一 Agent** | 14 个上下文就想 14 个子 Agent，或"3 个微服务 3 个 Agent" | 证据链被切碎、join 落到 LLM、架构被运维拓扑绑架 |
| **LLM Supervisor 自由路由** | 主 Agent 是开放式 ReAct，自己决定派给谁、自己汇总对话 | 不稳定、贵、不可复现；退化成"一个工具很多的 Agent + 一层转述损失" |
| **固定 N 路扇出** | 不看数据是否存在，一律六路并行 | 在死分支上烧钱，无效结果还挤占上下文 |
| **子 Agent 回自由文本** | "设备看起来有点漂移，建议关注一下" | 无法 join、无法审计、无法引用；父会话被污染 |
| **用 LLM 做 join / 用 LLM 当写入口** | 让 judge 合并两路证据、直接产处置动作 | join 是确定性的活；写必须单线程走 gate |

---

## 10. 回退判据：什么时候**不该**这么做

| 症状 | 正确处理 |
|---|---|
| 数据量不大（一个视图看得完） | 单 Agent ReAct 顺序取证。MAS 是 3 倍 token 换 2 秒延迟，而工程师等人等得起（延迟必须是真瓶颈） |
| 子问题序贯依赖（找到设备漂移才决定要不要查物料批次） | 不是真并行；用 probe 窄化后动态决定扇出宽度，或干脆单线程 |
| 需要全局一致的命名 / 口径 / 术语 | Cognition 反例：局部合理、全局不自洽。回到单 Agent 顺序生成 |
| 聚合结果要触发写 | 写保持单线程；聚合只产 artifact |
| 动机是"让结论更准" | 本形态不含质量收益（C8 不成立）。要更准需另设**外部证据角色**（复核 §3.1 场景二），不能靠多加几路"重读一遍" |
| 评测集证明不了收益 | `AGGREGATE_MODE=single` 回退，保留代码与契约 |

**开关**：`AGGREGATE_MODE ∈ {single, probe_fanout, fanout}`，按 scenario 配置，评测驱动切换，而不是上线后靠感觉。

---

## 11. 演进路径（与主文档 §11 对齐）

| 阶段 | 动作 | 拆分判断 |
|---|---|---|
| 1 | 单 Agent 顺序取证（诊断 ReAct）先跑通 | 不拆 |
| 2 | 上下文工程：工具分层路由、just-in-time 检索、压缩与笔记外置 | 多数聚合需求到此解决，**不要跳级** |
| 3 | 接入运行时形态④：先 **2 路**（事实链 + 条件链），`probe` 常开 | 不新增进程，只新增一个 capability key |
| 4 | 视角扩到 3~5 路，按 probe 动态收敛；judge 可选跳过 | 仍不拆进程 |
| 5 | 若**某一路**出现独立资源画像 / 独立 owner / 依赖冲突（主文档 §8 六条之一） | **只拆那一路**，`EvidencePack` 变 transport payload，逻辑不改、契约不改 |

**顺序理由**：先把"单 Agent + 上下文工程"的基线跑出来，才知道扇出到底买到了什么。没有基线就直接上扇出，等于把"多烧了算力"当成架构升级。

---

## 12. 一句话总结

> **中心化 = 代码统筹 + 单点裁决**：`plan` / `join` / 版本校验是代码，LLM 只在"意图→查询计划"和"证据→排序"两处出现；子会话按**取证视角**切而不是按微服务切，工具注册才对齐限界上下文；每路只回可引用的 `EvidencePack`，各分支只读同一份 `evidence_set_id`、不看对方的中间结论（无中心校验 17.2× vs 中心化 4.4×），`gaps` 与 `confidence` 都由规则决定，不靠模型自评；只读、写单线程、gate 在 `fan-in` 之后。收益只算覆盖、压缩、延迟三项——**要"更准"，得另设能拿到外部证据的角色，不是多派几路。**

---

## 13. 评审防守 Q&A

**Q：这不就是 fan-out / fan-in 吗？为什么叫"多 Agent 架构"？**
A：是。核心收益来自上下文隔离、真并行、就地压缩，不来自"多个 Agent"。所以我没给它单开一层，它是既有 runtime 上的一个运行时形态（新增一个 capability key 而已），统筹段是应用层 Python 代码（顺序 `await` + `asyncio.gather` + 显式合并），只有一个 LLM 裁决能力节点。框架不提供协同原语，别把它叫成框架能力。

**Q：为什么不按微服务切，一个微服务一个子 Agent，不是更清晰？**
A：清晰的是组织图，不是证据图。一条 `batch_id` 的证据链横跨物料/过点/检验三个上下文，按服务切会把链切三段、把 join 逼给 LLM；服务一拆并 flow 代码还得跟着改。所以工具注册对齐上下文（那是权限与审计边界），Agent 边界按"要回答的问题结构"切（事实 / 条件 / 检验三视角），一个子会话可跨多个上下文取只读工具。两条判据卡着：工具重叠 >50% 就合并，子会话内 LLM 调用为 0 就降级成确定性 flow 步骤。

**Q：子 Agent 之间会不会互相污染、最后越权写？**
A：通道只允许回 `EvidencePack`（Pydantic 强校验、字段白名单），返回的字符串不被当命令执行；judge 只能引用 `item_id`，编不出证据。子会话 `writes_allowed=False` 由启动断言兜底，聚合不产写意图，写回编排 gate。安全边界建在工具执行层，不在"分了几个 Agent"。

**Q：怎么证明它比单 Agent 好？**
A：靠三组评测 + 一条成本闸门，而且必须有单 Agent 顺序取证基线。如果只有端到端延迟下降、覆盖召回与裁决排序没提升，那就诚实地把它记成"延迟优化"，别包装成"架构升级"；证明不了就 `AGGREGATE_MODE=single` 回退。

**Q：上线了吗？**
A：没有。本文是设计定义，落地依赖诊断能力与 `SessionService` 并发改造先到位，评测集先建起来。
