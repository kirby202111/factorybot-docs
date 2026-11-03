# FactoryAgent —— 研究报告生成子图（Research Report）定义与实现方案

> **本文定位**：定义**图形态⑤ `research_report`**——"复杂研究报告生成"这一类需求的运行时形态。回答五件事：它与 RCA 取证的分工、五段链路里到底哪里需要 LLM、数字与口径如何保证不出错、子 Agent 该在哪一层才出现、以及什么条件下**不该**用这条链路。
> **与既有文档的关系**：[中心化聚合子图](FactoryAgent-中心化聚合子图.md)（下称"聚合篇"）给出图形态④ `fanout_aggregate` 的完整定义，本文的**取证段直接复用它的实现与契约**，只增量定义"取证之后"的分析、叙事、校验三段；[单运行时三能力架构](FactoryAgent-单运行时三能力架构.md) 给能力矩阵、十条硬约束、启动断言与可拆分性；[总体设计与实现方案](FactoryAgent-总体设计与实现方案.md) §7 给包结构。多 Agent 的必要性判据以 [哪些场景真正需要 Multi-Agent 系统](../哪些场景真正需要Multi-Agent系统.md) 与 [Multi-Agent 必要性复核](../Multi-Agent必要性-独立复核与批判.md) 的三条硬判据（C1' / C3' / C8）+ 成本闸门为准，业务场景以 [车间 MES 的 Multi-Agent 真实业务场景详解](../车间MES的Multi-Agent真实业务场景详解.md) 为准。
> **口径纪律**：本文是**设计定义**，不声明"已上线"；文中数字均为**目标示例值**，用于说明该衡量什么，不是行业基准。文中"子 Agent"一律指**同进程内的短命子会话**，不是独立部署的智能体。
> **本文不覆盖**：RCA 的假设生成与红队机制（见聚合篇与场景详解场景二）；写操作与处置决策（保持单线程，回编排 gate 或草稿 confirmation）。

---

## 0. 要点结论

1. **报告不是"又一个 Agent"，是一个能力条目 + 一段图**：取证段复用图形态④的实现与 `EvidencePack` 契约，只增量分析、叙事、校验三段。`AgentRuntime` / `ToolRegistry` / `ToolPolicyGate` / checkpointer / trace 零改动。
2. **报告是覆盖驱动，RCA 是搜索驱动**：报告的章节由模板**枚举**出来，RCA 的分支由现象**搜索**出来。两者的多 Agent 部分是同一套机制，区别在它外面包了什么。
3. **LLM 只出现在三处**：开放文本 → `report_type` 的受限分类、正文叙事、校验环节的合规判断。**取证路径、数字计算、排序、join 全部是代码。**
4. **数字不出自模型**：模板槽位 + `MetricItem` 注入，模型只写叙述骨架。幻觉面从"数字"收窄到"措辞"——数字错了没人能看出来，措辞错了人能一眼看出。
5. **口径是数据，不是代码**：口径注册表 + 版本化 + owner。查不到已注册口径就写 `gaps(UNDEFINED_CALIBER)`，**不许现场编一个**。
6. **报告不能分章并行**：报告是强一致性文档，并行分章必然导致摘要与正文、结论与数字、前后章节口径互不一致。
7. **并发 ≠ 子 Agent**：多个数据源要快，用 `asyncio.gather` 直调 tool（0 token、200ms 级）；子会话是**上下文隔离**手段，与并发正交。
8. **子 Agent 只在四层解法的最后一层**：工具聚合 → 引用式返回 → reducer 节点 → 子会话。前三层不花 LLM token，覆盖 90% 的结构化数据。
9. **唯一的质量收益来自校验**：校验方必须能用**另一条路径**把数字重算一遍（命中 C8）。其余收益只有覆盖、压缩、延迟三项。
10. **评测前五项全自动**：数字一致性、引用覆盖率、口径版本、覆盖完整性、结论方向——意味着每次改动都能跑全量回归。这是报告类能工程化、RCA 不能的根本区别。

---

## 1. 定位：为什么它不是第五个 Agent

### 1.1 与 RCA 取证的分工

| 维度 | RCA 取证（图形态④，假设驱动） | 研究报告（图形态⑤，覆盖驱动） |
|---|---|---|
| 触发 | 事件驱动（不良率突增） | 周期或请求驱动（月报、季度复盘、8D 材料） |
| 终态产物 | 一个判断：根因排序 + 影响面 | 一份文档：叙述 + 数字 + 建议 |
| 分支怎么来 | 从现象**搜索**出 2~4 个假设 | 从模板**枚举**出固定章节与数据槽位 |
| 扇出宽度 | 动态（可能 1 路，可能 4 路） | 基本固定（按数据视角数） |
| 子会话在回答什么 | "是不是锡膏的问题？"（假设） | "这个域的数据是什么？"（视角） |
| 证据用途 | **证伪**（必须收集反证据） | **陈述**（趋势、构成、对比） |
| 子会话可否早退 | 可以（假设被推翻即退出） | **不可以**（必须覆盖完整个时间窗） |
| "没查到"的含义 | 可能是好结果（假设被排除） | 永远是缺陷（章节缺失） |
| LLM 出现位置 | 窄化、取证决策、judge 排序 | 分类、叙事、校验 |
| 正确性判定 | 根因命中 → 需人工标注历史案例 | 数字一致 + 引用覆盖 → **全自动** |
| 回退方案 | 单 Agent 顺序取证 | 模板 + 代码分析（可完全无 LLM） |

**一句话**：RCA 用多 Agent 是为了**找到原因**，报告用多 Agent 是为了**凑齐材料**。前者失败于没找到，后者失败于数字错——两者要防的东西根本不一样。

> 补充：RCA 场景里其实混了两种模式——"根因"部分是假设驱动，"影响面五级清点"部分是覆盖驱动。**报告生成天然同构于后者**，这也是它比 RCA 更容易做对、更容易评测的原因。

### 1.2 它不是"第五种 Agent"，是一段组合图

```text
L1 运行时 Runtime（零改动）
        │
        ▼ 按 capability key 取：spec + graph + tool 切片
L2 能力条目（新增一条）
   diagnosis(open_react)  drafting(fixed_chain)  orchestration(supervisor)
   aggregate(fanout_aggregate)  report(research_report) ← 本文：只读取证 + 分析 + 叙事 + 校验
        │
        ▼
L3 图形态⑤ research_report
   取证段 = 图形态④的实现（原样复用）
   分析/叙事/校验段 = 一段 fixed_chain
```

**关于 `GraphShape` 枚举是否新增值**：语义上它是"④ + fixed_chain"的组合，实现上建议**新增 `RESEARCH_REPORT` 标签**，以满足"`graphs` 启动时按能力装配一次、运行时只按 key 取图"的纪律（聚合篇 §1）。图内部由两个子图拼接，启动断言按子图逐段检查；不新增进程、不新增协议。

### 1.3 为什么"5 个子任务"式的计划编排是反模式

常见的产品叙事是这样：

```text
用户提问 → Plan 模式自动创建 5 个子任务
  ① 调数据平台 API 取同行数据
  ② 知识库检索我方历史策略
  ③ 调数据分析子 Agent 做多维对比
  ④ 调报告生成子 Agent 撰写
  ⑤ 汇总结论生成建议
```

按本文口径逐条判定：

| 示例子任务 | 真实性质 | 判定 |
|---|---|---|
| ① 取数 API | 确定性查询 | **TOOL / CODE**，不是 Agent |
| ② 知识库检索 | 只读检索 | **TOOL**（存量文档走离线索引） |
| ③ 多维对比分析 | 指标计算、同环比、帕累托、分布检验 | **CODE**（口径 + 原语库，见 §5） |
| ④ 撰写报告 | 强一致性长文档 | **单 Agent，且不能并行** |
| ⑤ 汇总结论与建议 | 在结构化结论上做叙述 | **单 Agent**（与 ④ 合并） |

真正需要 Agent 的只有 ①② 的并行只读取证一处，而 ③④⑤ 是**序贯依赖**。

**如果做成 5 个 Agent 串行**：成本上升一个数量级，可靠性反而下降——单步 95% 可靠时，5 步链整体约 77%，10 步约 60%。这是"看起来像多 Agent 架构、实际是劣化的单 Agent 流程"的典型。

**同样禁止"Plan 由 LLM 自由生成"**：同一份数据两次跑出不同结构的报告，在质量与合规场景不可交付（审核人无法比对、无法归档、无法追责）。`plan` 必须是**映射表**（见 §3.2）。

---

## 2. 边界：哪些"报告需求"不该走这条链路

| 症状 | 正确处理 |
|---|---|
| 单表查询 + 固定模板就能出的周报 | 模板 + SQL，不上 Agent |
| 报表/看板（数字为主，无叙述需求） | BI 层，不上 LLM |
| 一次性临时分析、探索性取数 | 单 Agent + 代码工具，跑完即弃 |
| 需要"结论更准"而非"覆盖更全" | 另设**外部证据角色**（红队/对账，C8），不是多加几路 |
| 需要触发写操作（派工、开返工单、隔离） | 写保持单线程；报告只产 artifact |
| 数据就绪度不够（关键绑定缺失、采集断点高） | **先修数据**。取不到证的架构只会输出"证据不足转人工" |

---

## 3. 图

### 3.1 图

```text
invoke("report", payload, tenant, parent_session_id=?)     ← 复用唯一入口
  session=S1(capability=REPORT, report_type=<...>, trace=T1)
  │
  ├─[CODE] classify    自然语言 → report_type + 锚点(产线/工序/时间段/产品族)   ★ 受限分类
  ├─[CODE] plan        ReportSpec → 章节清单 + 必备数据槽位                      ★ 映射表，非 LLM 规划
  │
  ├─═════ 第一段：取证（复用图形态④，不改契约）═════
  │   ├─[CODE] probe        每路一次 count / 覆盖期探测
  │   ├─[CODE] fan_out      只对命中路径并发取证
  │   │     ├─ 路径确定的源  → asyncio.gather 直调 tool（不建 session）
  │   │     ├─ 需要语义压缩的源 → 子会话（role=FACT/CONDITION/MATERIAL/INSPECTION/ACTION/DOC）
  │   │     └─ 每路只回 EvidencePack
  │   ├─[CODE] fan_in       join / 去重 / 版本锚点一致性 / 冲突登记
  │   └─ 产出 EvidenceBundle
  │
  ├─═════ 第二段：分析与叙事（fixed_chain，顺序执行）═════
  │   ├─[CODE] analyze  Metrics   趋势/同环比/帕累托/贡献度分解/SPC 判异/分布对比
  │   │                            ★ 全确定性，产出带 formula_id 的 MetricItem
  │   ├─[LLM ] attribute          仅异常归因与口径歧义（可跳过）
  │   ├─[AGENT] narrate           单 Agent 顺序生成全文 ★ 只能引用 metric_id，不能自己写数字
  │   ├─[AGENT] verify            用外部证据校验（§4.4）★ 唯一的质量收益点
  │   └─[CODE] render             模板 + 数据槽位注入 → ReportArtifact
  │
  └─[CODE] report     ReportArtifact + CoverageGap + need_human_review → 人审定
```

### 3.2 节点清单（`node_type` 可落库验证）

| 节点 | `node_type` | 职责 | 可跳过 |
|---|---|---|---|
| `classify` | CODE | 受限分类：枚举里挑 `report_type` + 填锚点，不自选章节 | 否（调用方直接给类型时可跳） |
| `plan` | CODE | `ReportSpec` → 章节 + 必备数据槽位（映射表） | 否 |
| `probe` | CODE | 每路一次有无/量级/时间窗覆盖探测 | 可（`fanout.probe_enabled=False`） |
| `fan_out` | CODE | 并发取证，路径确定的直调、需压缩的起子会话 | 否 |
| `fan_in` | CODE | join、去重、版本一致性、冲突登记 | 否 |
| `analyze` | CODE | 指标计算与统计检验，产出 `MetricItem[]` | 否 |
| `attribute` | **AGENT** | 异常归因、口径歧义消解 | **可**（无异动时 LLM 调用为 0） |
| `narrate` | **AGENT** | 顺序生成全文，只能引用 `metric_id` | 否 |
| `verify` | **AGENT** | 数字回算、引用溯源、方向一致性、合规条款 | **不允许跳过**（跳过即无质量收益） |
| `render` | CODE | 模板注入、gaps 汇总、`need_human_review` | 否 |

**可验证标志**：`agent_step_total{capability="report",node_type="CODE"}` 正常增长，且 `agent_llm_invocation_total{capability="report",role="MARKET"}` 类纯取数角色应为 0 —— 说明确定性段没有被交给模型。

### 3.3 LLM 只出现三处，其余一律代码

| 位置 | 允许 / 禁止 | 形态 |
|---|---|---|
| 开放文本 → `report_type` + 锚点 | ✅ 允许 | `with_structured_output` 受限分类，枚举内选 |
| 数据异常 → 归因 | ✅ 允许 | 输入只有 `MetricItem[]` 与 `gaps`，输出必须引用 `metric_id` |
| 正文叙事 | ✅ 允许 | 单 Agent 顺序生成，只填模板槽位，不自造数字 |
| 校验环节的合规判断 | ✅ 允许 | 对照条款库/规则库，输出必须回指条款编号 |
| 决定报告有哪些章节 | ❌ 禁止 | 由 `ReportSpec` 模板决定 |
| 计算任何数字 | ❌ 禁止 | 由 `analyze` 节点用代码算 |
| 分章并行撰写 | ❌ 禁止 | 强一致性文档，必须顺序生成 |
| 自评置信度 | ❌ 禁止 | 规则计算（覆盖完整度 + 口径齐备 + 样本量） |
| 生成写意图 | ❌ 禁止 | 报告只产 artifact，写回编排 gate |

---

## 4. 契约

### 4.1 `ReportSpec`（模板，代码资产）

```python
# app/domain/report.py
class SectionSpec(BaseModel):
    section_id: str
    title: str
    required_slots: list[str]        # 必备数据槽位；缺一即判"章节不完整"
    narrative_hint: str              # 叙述骨架（不是 prompt 正文）
    parallel_forbidden: bool = True  # 章节一律顺序生成

class ReportSpec(BaseModel):
    report_type: str                 # QUALITY_QUARTERLY / MONTHLY_RECON / MRB_PACKAGE / 8D
    sections: list[SectionSpec]
    required_calibers: list[str]     # 强制口径 ID + 版本
    verify_rules: list[str]          # 校验规则集 ID
```

### 4.2 `MetricItem`：数字不出自模型

```python
class MetricItem(BaseModel):
    metric_id: str
    value: float
    unit: str
    formula_id: str            # 可回算：同一 formula 重跑必须同值
    source_query_hash: str     # 可溯源：报表数字与明细能对上
    caliber_id: str            # 口径 ID
    caliber_version: str       # 口径版本（如"有效运行时长 v3"）
    coverage: dict[str, Any]   # 时间窗 / 全量 or 采样 / 采样率 / 样本量
```

模板注入而非模型代笔：

```text
错误：模型自己写          "本季度不良率为 3.2%，较上季度下降 1.1 个百分点"
正确：模板槽位 + 注入     "本季度不良率为 {@metric:weld_defect_rate}%，
                          较上季度{@trend_verb:weld_defect_rate}{@delta:weld_defect_rate}个百分点"
```

### 4.3 `NarrativeClaim`：无依据的结论判失败

```python
class NarrativeClaim(BaseModel):
    claim_id: str
    text: str
    metric_ids: list[str]      # ★ 空列表 = 判失败，不许有无依据的结论
    item_ids: list[str]        # ★ 回指证据包条目
    clause_refs: list[str]     # 合规类结论必须回指条款编号
```

`metric_ids` 或 `item_ids` 为空即判失败重试。这条比 prompt 里写十遍"不要编造"有效得多。

### 4.4 `CaliberSpec`：口径是数据，不是代码

```python
class CaliberSpec(BaseModel):
    caliber_id: str            # "effective_runtime"
    version: str               # "v3"
    definition: str            # 业务定义（人读）
    formula: str               # 可执行公式（代码跑）
    owner: str                 # 责任人（治理）
    effective_from: date       # 生效期；历史报告按当时版本重算
    applicable_scope: dict     # 适用产线 / 工序 / 产品族
```

**关键**：新增口径只改注册表、不发版；历史报告一律按**当时生效版本**重算，否则同比对比无意义。

---

## 5. 分析与口径：为什么"代码里没有所需口径"不是否决理由

这是报告类最常见、也最容易被误判成"必须用 LLM 做分析"的地方。要区分三件事：

| 层次 | 是什么 | 归谁 |
|---|---|---|
| **口径** | "什么算不良""有效运行时长怎么算""哪批算在制""归到哪个工序" | **注册表**（数据 + 版本 + owner） |
| **算子** | 趋势、同环比、帕累托、贡献度分解、SPC 判异、分布检验、相关性 | **原语库**（有限、可枚举、可单测） |
| **组合** | 这次看哪个维度、哪个时间窗、按什么分组、用哪个口径 | **LLM**（结构化查询计划） |

### 5.1 算子固定，组合自由

```python
TREND(metric, window, granularity)
PERIOD_COMPARE(metric, current, baseline, dims)
PARETO(metric, dim, top_k)
CONTRIBUTION_DECOMPOSE(metric, dims, method)     # 加法 / 乘法分解
SPC_JUDGE(metric, rule_set)                      # 连续 7 点上升等
DIST_COMPARE(metric_a, metric_b, test)           # KS / 卡方
CORRELATE(metric_a, metric_b, method)
```

LLM 的产出不是结论文本，也不是自由 SQL，而是**结构化分析计划**：

```text
AnalysisPlan = [
  PARETO(metric=weld_defect_rate, dim=defect_type, top_k=5,
         caliber=defect_classify@v2, window=Q3),
  PERIOD_COMPARE(metric=weld_defect_rate, current=Q3, baseline=Q3_last_year,
                 dims=[line, shift]),
  SPC_JUDGE(metric=reflow_temp_peak, rule_set=western_electric),
]
```

该计划**可审计、可重放、可 diff**：同一输入重跑，计划相同、数字必须相同。而"这次想到看缺陷类型而不是工序"，靠的是 LLM 选算子 + 填参数的能力——没有被固化掉。

### 5.2 原语库覆盖不了时：允许 LLM 生成查询，但数字必须出自数据库

四条硬约束，缺一不可：

1. **只读 + 沙箱 + 表白名单**：禁止写、禁止跨租户、禁止系统表；
2. **扫描量上限 + 超时**：避免全表扫拖垮业务库；
3. **强制注入注册口径的 CTE**：口径不来自注册表，查询直接判失败（防口径漂移）；
4. **结果回写 `source_query_hash` + `formula_id`**：校验方能用同一段查询重跑对账。

> **红线**：LLM 站在**查询生成**位，不站在**结果生成**位。数字永远由数据库算出。

### 5.3 真正需要 LLM 做"分析"的三类

不是算数，是判断：

| 场景 | 为什么是判断 | 产出 |
|---|---|---|
| 口径歧义消解 | 报工 8h、设备采集 6.2h、考勤 7.5h，规则表覆盖不了 | 口径候选 + 依据，**转人工确认** |
| 新维度探索 | 用户问了注册表里没有的角度 | `gap(UNDEFINED_CALIBER)` + 建议新增口径 |
| 异常归因 | 数字有了，为什么？ | 假设排序，**必须回指 `metric_id`** |

三类的产出都是**假设 / 候选 / 建议**，不是最终数字。

### 5.4 兜底：挑不到口径就报 gap，不许猜

```python
if caliber_id not in CALIBER_REGISTRY.get_scope(tenant=tenant, line=line):
    gaps.append(CoverageGap(
        kind="UNDEFINED_CALIBER",
        detail="'按班组资质分组的不良率' 无已注册口径，需业务定义",
        ref=f"caliber_catalog@{period}",
    ))
    # 报告照常出，该章节显式声明"此项未覆盖"
```

> **报告里写"口径未定义"，比编一个口径出来好一百倍。** 前者是待办事项，后者是事故。

### 5.5 冷启动：注册表是积累出来的，不是设计出来的

| 阶段 | 口径数量 | 处理 |
|---|---|---|
| 第 1 个月 | 10~15 个（不良率、良率、OEE、换线时长、齐套率…） | 覆盖 80% 常规报告 |
| 遇到覆盖不了 | 记 `gap(UNDEFINED_CALIBER)` | **不阻塞出报告**，只标注未覆盖 |
| 每两周 | 把高频 gap 定义成新口径 | 注册表变厚，不发版 |

这个循环有记忆、单调收敛。而"让 LLM 每次自由发挥"没有记忆——每次编得不一样。

---

## 6. 子 Agent 使用判据（本文最重要的工程约束）

### 6.1 四层解法，按成本从低到高

| 层 | 手法 | 成本 | 覆盖 |
|---|---|---|---|
| ① 工具设计 | 接口返回**聚合结果**而非原始行 | 最低 | 结构化数据 90% |
| ② 引用式返回 | 上下文只放 `ref` + 摘要，用时再取 | 低 | 大表、长文档 |
| ③ Reducer 节点 | tool 返回先过代码压缩，再进上下文 | 低 | 通用 |
| ④ 子会话 | LLM 读原文做语义压缩 | **高 10~100 倍** | 非结构化长文 |

做完 ①②③ 通常就够了。"接口返回数据太多"十有八九是**工具粒度设计错了**，不是"该拆子 Agent 了"。

### 6.2 升级判据：画得出确定的调用序列吗

| 形态 | 采用条件 | LLM 调用 | 建 session |
|---|---|---|---|
| tool 直调 | 单次调用、参数确定、返回小 | 0 | 否 |
| CODE 节点 | 多步但路径固定 | 0 | 否 |
| 子会话 | 路径不确定：要先看目录、要换策略、要多轮改写 | ≥1 | 是 |

**实操**：把这一路的工具调用序列画出来——**画得出且每次一样 → CODE；画不出或每次不同 → 子会话。**

### 6.3 并发 ≠ 子 Agent

多数据源要快，用协程，不需要 Agent：

```python
packs = await asyncio.gather(*[fetch(src) for src in sources],
                            return_exceptions=True)
```

| 方案（6 源、单次 200ms、LLM 首 token 500ms） | 延迟 | token |
|---|---|---|
| 串行直调 | 1.2s | 0 |
| **gather 并发直调** | **~230ms** | **0** |
| 6 路子会话并发 | 2~3s | ~18k |

**子 Agent 比并发直调慢一个数量级，比串行还慢**——扇出本身已并行，但每路内部多了两轮 LLM 推理。所以"为了加快速度"这个动机恰恰指向**不要**用子 Agent。

**并发用协程（gather + `Semaphore` 限流），隔离用会话（session），两者正交。**

### 6.4 相似案例检索不是子会话

存量文档（历史 8D、工艺规程、客诉归档）不随时间变化，正确做法是**离线预计算 + 在线检索**：

```text
【离线，一次性 + 增量】
  存量文档 →[CODE] 章节解析 →[LLM 离线] 抽成结构化 CaseCard →[CODE] 建向量/BM25/元数据索引

【在线，每次查询】
  现象 + 锚点 →[CODE] 元数据粗筛 →[TOOL] 混合检索 + rerank →[LLM] 在 top-k 卡上判断相似点
```

在线约 2k token、200ms、0 子会话；对比运行期 3 路并发子会话约 80k token、10s。**差 40 倍，且离线结果稳定可复现。**

子会话在文档场景里的正确位置是另一个任务：**运行期必须全量读新进来的文档、逐篇抽结构化要素**（未索引的扫描件、当天日志、批量上传附件）。那里 3~5 路并发、按 token 预算分批、只回结构化字段。它的收益是**隔离带来的抽取质量**，不是速度。

### 6.5 用观测指标验证，不靠争论

上线后看 `agent_llm_invocation_total{capability="report", role="<role>"}`：

- **= 0** → 这一路白建了会话，立即降级为 CODE；
- **平均 1 次** → 大概率只是"选了个接口"，改用受限分类器；
- **平均 ≥3 次** → 真的有探索行为，保留子会话。

---

## 7. 上下文治理

### 7.1 阈值

| 指标 | 建议阈值 | 依据 |
|---|---|---|
| 单次 tool 返回 | ≤ 5k token（超出走 reducer） | 上下文退化在远低于窗口上限前就开始 |
| 单个 `EvidencePack` | ≤ 50 条 × 压缩字段 | 强制就地压缩 |
| 父会话累计 | 达 40~50k token 触发压缩/卸载 | 不要等 150k |

### 7.2 显式断开 tool → LLM 直连

多数框架默认把 tool 返回值原样 append 进 messages——这是"上下文爆炸"的根因。正确链路：

```text
LLM → tool_call → 返回 8000 行 → [CODE reducer] → 80 行摘要 → 进 messages
```

reducer 可以是分组聚合、top-k、采样、统计量提取、去重、字段裁剪。**不花 token、结果确定、可单测。**

### 7.3 截断必须显式声明

```python
if len(rows) > max_rows:
    rows = rows[:max_rows]
    pack.gaps.append(CoverageGap(
        kind="TRUNCATED",
        detail=f"返回 {total} 行，已截断至 {max_rows} 行，结论仅基于前 {max_rows} 行",
    ))
```

**静默截断比上下文爆炸更危险**：上下文太长是明面上的成本，而报告只基于部分数据却不自知，会被人签进正式文档——那是事故，不是性能问题。

---

## 8. 落点与实现

### 8.1 `CapabilitySpec` 增量

```python
class GraphShape(str, Enum):
    OPEN_REACT = "open_react"
    FIXED_CHAIN = "fixed_chain"
    SUPERVISOR = "supervisor"
    FANOUT_AGGREGATE = "fanout_aggregate"
    RESEARCH_REPORT = "research_report"       # ⑤ 本文：取证段复用 ④

class ReportPolicy(BaseModel):
    max_sections: int = 12
    max_metrics: int = 80
    analyze_timeout_s: float = 20
    narrative_max_tokens: int = 6000
    verify_required: bool = True              # 启动断言：为 False 直接拒绝装配
    parallel_sections: bool = False           # 固定 False，见 §9 硬约束 4
```

建议的 `report` spec：`writes_allowed=False`、`max_recursion=14`、`session_timeout_s=120`、`tool_tags=["read:*"]`（实际下发仍受 `toolboxes` 二次约束）。

### 8.2 文件落点（相对 `agent_service/app/`）

| 文件 | 内容 | 层级 |
|---|---|---|
| `domain/report.py` | `ReportSpec` / `SectionSpec` / `MetricItem` / `NarrativeClaim` | domain |
| `domain/caliber.py` | `CaliberSpec` + 注册表访问 | domain |
| `domain/evidence.py` | `CoverageGap.kind` 增 `UNDEFINED_CALIBER` / `SECTION_INCOMPLETE` | domain |
| `ai/report_graph.py` | 图形态⑤装配（**建议文件名就叫 `report_graph.py`**） | ai |
| `ai/code_nodes/plan_report.py` | `ReportSpec` → 章节 + 槽位映射表 | ai |
| `ai/code_nodes/analyze.py` | 分析原语库调度，产出 `MetricItem[]` | ai |
| `ai/code_nodes/render.py` | 模板 + 槽位注入 → `ReportArtifact` | ai |
| `ai/agent_nodes/narrate.py` | 单 Agent 顺序叙事 | ai |
| `ai/agent_nodes/verify.py` | 数字回算 / 引用溯源 / 方向一致 / 条款比对 | ai |
| `ai/prompts/narrate_v1.md` | 版本化 prompt；只允许引用 `metric_id` | ai |
| `application/runtime.py` | **不改 `invoke()` 签名** | application |
| `infrastructure/obs/metrics.py` | 新增报告类指标（§10） | infra |

### 8.3 与聚合图的关系

| 项 | 处理 |
|---|---|
| `fan_out` / `fan_in` / `probe` | **原样复用**，不复制代码 |
| `EvidencePack` / `EvidenceItem` | **零改动**，只扩 `CoverageGap.kind` 枚举 |
| `ToolRegistry.toolboxes` | 复用；报告额外需要"面向分析的工具"分组（聚合视图、统计接口） |
| 指标命名空间 | 挂在既有命名空间，不另起一套 |

---

## 9. 预算、失败隔离与降级

| 项 | 建议值 | 理由 |
|---|---|---|
| 取证段扇出 | 按数据源配，默认 ≤5 | 并发数即成本闸门 |
| 分析段 | 单节点串行，超时 20s | 全确定性，慢说明查询设计有问题 |
| 叙事段 | 单 Agent，输出 ≤6k token | 强一致性文档，不能并行 |
| 校验段 | 不允许跳过 | 跳过即无质量收益 |
| 父会话总预算 | `max_recursion=14`、120s | 比诊断宽松（分析链更长），比编排紧 |
| 单源失败 | `PARTIAL` + `gaps`，对应章节标注未覆盖 | 不重跑全链 |
| 关键源全失败 | 不出正文，直接 `need_human_review` + 列出缺哪几章 | 不许在空证据上叙事 |
| 校验不通过 | 回 `narrate` 重试 ≤2 次；仍不通过 → 输出草稿 + 差异清单 | 让人看差异，不让系统硬出 |

---

## 10. 硬约束（聚合篇 §7 在本形态的延伸）

1. 子会话 `writes_allowed=False`，不得持有写 client；断言覆盖父、子两级 spec。
2. 报告图自身不持工具（只有子会话与 `attribute` / `verify` 持）。
3. 子会话不读写父会话 state，只回 `EvidencePack`。
4. **章节顺序生成**，`parallel_sections` 固定为 `False`；启动断言拒绝该值为 `True` 的 spec。
5. **数字只能由 `analyze` 产出**；`narrate` 的输出若出现未绑定 `metric_id` 的数值，直接判失败。
6. **每条结论必须回指 `metric_id` 或 `item_ids`**，空列表即失败重试。
7. 口径必须来自注册表且带版本；查不到写 `gaps(UNDEFINED_CALIBER)`，不许现场编。
8. `verify` 不可关闭；`verify_rules` 必须来自规则库，不能是 prompt 里的自然语言描述。
9. 预算按子任务取值，禁止继承父会话的步数与超时（硬约束 9：不搞全局常量兜底）。
10. 缓存 key、配额、指标、trace 一律带 `role` 与 `report_type` 维度。
11. `gaps` 非空 → 产出 `need_human_review`；关键源全失败 → 不调 `narrate`。
12. 报告**不产写意图**；写动作回编排 gate 或草稿 confirmation。

---

## 11. 可观测与评测

### 11.1 指标

| 指标 | 标签 | 回答什么 |
|---|---|---|
| `agent_report_total` | `report_type, status` | 哪类报告在退化 |
| `agent_report_section_incomplete_total` | `report_type, section_id` | 哪个章节长期缺数据（暴露数据就绪度问题） |
| `agent_metric_recompute_mismatch_total` | `metric_id` | 数字回算不一致（最严重，应趋近 0） |
| `agent_claim_unsupported_total` | `report_type` | 无依据结论的比例 |
| `agent_caliber_gap_total` | `caliber_id` | 口径缺口排序 → 注册表待办 |
| `agent_llm_invocation_total` | `capability, node_type, role` | 子会话是否被滥用（纯取数 role 应为 0） |

### 11.2 评测（前五项全自动，可做全量回归）

| 组 | 检查 | 自动化 |
|---|---|---|
| ① 数字一致性 | 用 `formula_id` 重算，与报告数值比对 | ✅ |
| ② 引用覆盖率 | `NarrativeClaim.metric_ids` 非空率 | ✅ |
| ③ 口径正确性 | 报告口径版本 vs `ReportSpec.required_calibers` | ✅ |
| ④ 覆盖完整性 | 必备槽位填充率 + `gaps` 声明完整性 | ✅ |
| ⑤ 结论方向 | 方向词与数据趋势一致性 | ✅ |
| ⑥ 建议可执行性 | 建议项是否落到责任角色 + 验收指标 | 半自动 |
| ⑦ 与人写报告对比 | 同口径 A/B，人工盲评 | 人工 |

**这五项全自动是报告类能工程化的前提**——每次改 prompt、改模板、改口径都能跑一遍。做不到这点，就只能靠"看起来挺像"。

---

## 12. 反模式

| 反模式 | 现象 | 后果 |
|---|---|---|
| **5 个子 Agent 串行流水线** | 每步一个 Agent，靠 message 传状态 | 成本 ×10、可靠性 ×0.77、并行收益 ≈ 0 |
| **让模型写数字** | prompt 里说"请根据数据计算" | 数字幻觉，且错了没人看得出来 |
| **分章并行撰写** | 摘要、正文、建议各一个 Agent | 全局不自洽（Cognition 反例） |
| **Plan 由 LLM 自由生成** | 每次结构都不一样 | 不可复现、不可比对、不可归档 |
| **为并发付 Agent 的钱** | 6 个数据源起 6 个子会话 | 比串行还慢，token × 一个数量级 |
| **让第二个 Agent 重读草稿做审核** | 审核方与生成方同工具同证据 | 无效，甚至更差 |
| **静默截断** | 超出上限就取前 N 行，不声明 | 结论基于部分数据而不自知，签进正式文档 |
| **口径现场编** | 注册表没有就"按常识"处理 | 同一问题两次跑出不同答案 |

---

## 13. 回退判据

| 症状 | 正确处理 |
|---|---|
| 报告类型固定、数据源单一 | 模板 + SQL + 代码分析，**完全不上 LLM** |
| 数字一致性始终不过 | 先修工具与口径，不要换模型 |
| 只有延迟改善，覆盖与引用覆盖率没提升 | 诚实记为"延迟优化"，`REPORT_MODE=single` |
| 章节长期缺口 >30% | 数据就绪度问题，先修数据 |
| 校验通过率低但人工看是对的 | 校验规则写错了，先修规则，不要关掉 `verify` |

**开关**：`REPORT_MODE ∈ {single, probe_fanout, fanout}`，按 `report_type` 配置，评测驱动切换。

---

## 14. 演进路径

| 阶段 | 动作 | 目的 |
|---|---|---|
| 1 | 单 Agent + 全代码分析 + 模板注入渲染 | **先把"不幻觉"做对**。这一步就能出可交付报告 |
| 2 | 取证段复用图形态④，先 2 路 | 解决上下文污染与覆盖，`single` 做基线 |
| 3 | 加 `verify`（数字回算 + 引用溯源） | 拿到 C8 的质量收益，这是唯一能写进 KPI 的 |
| 4 | 扩到 4~6 路 + `attribute` 归因 | 动态收敛，`probe` 常开 |
| 5 | 多 `report_type` 模板化 | 横向复制到客诉 8D、MRB 材料、月度核对 |

**顺序理由**：阶段 1 就要能出报告，阶段 2 才开始谈多 Agent。反过来——先搭 5 个 Agent 再想怎么让数字对——会得到一套很贵、很难调、审核人不敢签字的东西。

---

## 15. 评审防守 Q&A

**Q：报告生成不是典型的"多 Agent 协作"吗？为什么只有两处需要 Agent？**

A：因为 5 个子任务里 ③④⑤ 是序贯依赖，做成 Agent 只是把一条链切成五段、每段多带一份 system prompt，并多四次概率乘法。真正的并行只在取证段。多 Agent 的价值是"不同的上下文、不同的工具、不同的证据"，不是"多几个角色"。

**Q：数据分析如果交给代码，代码里可能没有实际需要的统计口径怎么办？**

A：区分三层——口径是数据（注册表 + 版本 + owner，新增不发版），算子是原语库（有限可枚举），组合由 LLM 产出**结构化分析计划**（可审计、可重放）。搜索不到口径就写 `gaps(UNDEFINED_CALIBER)` 转人工定义，绝不现场编。原语库真的覆盖不了时，允许 LLM 生成查询，但数字必须由数据库算出。

**Q：多个数据源要快，不用子 Agent 并发调 tool 吗？**

A：并发用 `asyncio.gather`，不需要 Agent。6 源并发直调约 230ms、0 token；6 路子会话并发约 2~3s、18k token——**比串行还慢**。子 Agent 是上下文隔离手段，不是并发手段，两者正交。

**Q：单 Agent 调 tool 会不会因为返回数据太多导致上下文太长？**

A：先问"这些数据为什么需要进上下文"。四层解法：把工具改成返回聚合结果 → 引用式返回 → reducer 节点 → 子会话。前三层不花 token，覆盖结构化数据 90%。另外阈值要设在远低于窗口上限的地方（40~50k 触发压缩），且截断必须显式写 `gaps(TRUNCATED)`。

**Q：怎么保证报告里的数字是对的？**

A：三道闸。① 数字只由 `analyze` 产出，模型不能写；② 模板槽位注入，正文里没有裸数字；③ `verify` 用另一条查询路径重算，不一致即失败。这比"让模型仔细检查"可靠一个数量级。

**Q：上线了吗？**

A：没有。本文是设计定义，落地依赖聚合篇的取证段先跑通、口径注册表先建起来、`ReportSpec` 模板先定出来。

---

## 16. 一句话总结

> **报告 = 覆盖驱动，不是搜索驱动**：章节由模板枚举，数字由代码算，正文由**单 Agent 顺序**写，口径由注册表决定。多 Agent 只出现在取证段（复用图形态④），并发用协程不用子会话，数字只注入不由模型写。唯一的质量收益来自 `verify` 能用另一条路径把数字重算一遍——其余都只是覆盖、压缩和延迟。**报告的可信度不来自数字看起来合理，而来自同一个问题永远得到同一个答案。**
