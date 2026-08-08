# L3 编排型 Agent -- 选型决策卡与场景嵌入点验证

> 本文是 [L3编排型Agent-实现方案.md](L3编排型Agent-实现方案.md) §0「选型判断」的一页速查卡 + 对当前 3 个场景 spec 的逐节点验证记录。决策卡部分用于评审 / 面试速查"什么时候上 agent、什么时候不上";验证部分对着 `factorybot/app/orchestration/scenarios/specs.py` 的实际装配,确认每个 agent 嵌入点都踩在"三问有一"上,并标出待收口的 gap。
> **口径纪律**:本文不编业务效果数字(良率 / 停线百分比等),只讲选型判断与代码事实。验证基于 2026-08-07 的代码状态(commit `69873a0` 去除工艺切换场景后,剩 3 个场景)。**本次修订(2026-08-07)**:核代码后发现 `draft_rework_craft` 输出是指针不是工艺内容、三问皆否,从 D 生成类重分类为 D2 选型,归代码层主路径(见 §1.2 / §2.5)。
> **配套**:选型标准与 4 类痛点深挖见 [L3编排型Agent-痛点操作步骤与解决方案.md](L3编排型Agent-痛点操作步骤与解决方案.md);LangGraph 架构见 [L3编排型Agent-LangGraph架构与流程图.md](L3编排型Agent-LangGraph架构与流程图.md)。

---

## 1. 选型决策卡

### 1.1 判定三问

一个 MES 步骤要不要上编排型 agent,先过三问:

| # | 问题 | "是"的含义 |
|---|------|-----------|
| ① | **输入是否开放?** | 非结构化、需语义理解,不是 `expected == actual` 的结构化判定 |
| ② | **是否需要推理 / 生成?** | 非固定规则,要假设 / 排序 / 生成 |
| ③ | **分支是否难以穷举?** | 决策树永远落后于现场,新情况一来就漏 |

**判定规则:三问皆否 -> 代码节点;三问有一 -> agent 节点。**

> 换线全程 PASS 时 agent 不触发,LLM 调用为 0(`l3_llm_invocation_total=0` 可观测验证)--这是"该用才用"的可验证标志,不是口号。

### 1.2 该上 agent 的 4 类(代码做不了或极复杂)

| 能力 | sub-agent | 真痛点 | 三问命中 | 传统代码为什么难 |
|---|---|---|---|---|
| **A 根因处置** | RootCauseAgent | 换线 mismatch 后人脑判根因 + 电话串 3 系统 + 口头处置无审计 | ②③ | 根因分支组合爆炸 + 跨 4 上下文证据汇聚 + 启发式阈值 |
| **B 隔离范围** | FaultImpactAgent | 故障期间人脑估漂移时间窗 + 凭印象判产品敏感度 -> 漏 / 过隔离 | ②③ | 故障模式 × 漂移窗口 × 敏感度三维动态,规则引擎爆炸 |
| **C 根因追溯** | TraceabilityAgent | 客诉跨 5 界面手工串 + 版本串错 + 5M1E 假设靠人脑排 | ②③ | 跨源语义关联(JOIN 只做字段相等)+ 假设加权排序 |
| **D1 生成类** | DraftAgent(`draft_8d`) | 8D 报告靠工程师从零手写 | ①②③ | 结构化叙事生成(问题描述/根因/纠正措施),代码写不出 |

> **D2 返工工艺--不在生成类,在选型类(本次重分类)**:原把"返工工艺草拟"(`draft_rework_craft`)算进 D 生成类是错配。核代码事实:其输出契约是 `{rework_route, reentry_point}`(`draft_agents.py:42`),mock 实吐 `RR-RW-1 / OP-REFLOW` 两个**指针**(`mock_chat_model.py:183`),不是工艺内容;prompt 自称"开放生成"但输出契约是路由选择。返工路线本身是版本化安全契约(`route_version` 强制 + `ACTIVE` 校验,`process_management.py:26-42`),agent 只输出指向库的 ref、碰不到温度/时间/工序参数。过三问:① 输入半结构化、目标空间(工艺库)结构化 -> 否;② 匹配 route + 选 reentry 工位,非生成 -> 否;③ `(不良模式 -> 返工路线)` 是库内有界映射 -> 否(常见 case)。**三问皆否 -> 代码节点**(已补进 §1.3)。application 层 `ReworkOrderDraftBuilder` 已在草拟返工单(`rework_order.py:38`),orchestration 层的 `draft_rework_craft` 是重叠悬空物。agent 仅在"库未命中的新不良模式"边角做候选推荐(输出仍是结构化选型 + `need_human_review`,参数绝不经 LLM)。详见 §2.5。

**A / B / C 的共同收益**:取证路径由中间结果驱动(不是固定 JOIN),新根因 / 新故障 / 新产品出现时只要取证能查到证据就给假设,**决策树不用改**。D1 是价值最干净的一类--代码完全做不了(8D 叙事生成)。原 D 类里的返工工艺已重分类为 D2 选型,归代码层主路径。

### 1.3 不该上 agent 的(代码层兜底,硬逻辑更快更好)

| 活 | 代码解法 | 为什么不用 agent |
|---|---|---|
| 步骤编排 / 并行 / 汇合 | StateGraph 显式边 | 确定性,LLM 非确定性是负资产 |
| 结构化比对(齐套率 / 钢网号 / 程序版本) | query + compare 代码节点 | `expected == actual` |
| 漏步骤 / 带病生产防错 | barrier 双 PASS 硬校验 | 状态机比 agent 可靠 |
| 写动作越界 | `WriteToolGate` 白名单 + confirmation token | 权限模型,不是能力 |
| 误触放行 | 放行能力从工具集架构层裁掉 + 启动断言 | 风险兜底,靠代码不靠 LLM |
| 版本串错 | ACL `routeVersion` 强制过滤 | 确定性校验 |
| 过点放行 / 拦截(P99 ≤200ms) | 规则引擎 | agent 绝不进过点主事务 |
| 返工工艺选型(原 D 误判为生成) | `不良模式 -> 工艺路线库 query_route(route_version 强制 + ACTIVE 校验) -> (rework_route_ref, reentry_point)` | 输出是指针不是工艺内容,三问皆否;参数(温度/时间/工序)在版本化路线库里由 ACL 守,绝不经 LLM。仅"库未命中的新不良模式"边角才上 agent 做候选推荐(见 §2.5) |

> 把这层划清楚:面试时若把"防错 / 并行 / 审计"都算 agent 卖点,一问"工作流引擎能不能做"就露馅。明确划给代码,agent 只讲 A–C + D1 四类非确定痛点(返工工艺 D2 已归代码),恰恰是"懂什么时候不用 AI"的判断力。

### 1.4 三条红线(写动作的闸门始终在人手里)

1. **不进过点主事务**--放行类工具(`pass_judge` / `force_release` / `release_*`)不注册到任何 capability,启动断言校验。
2. **写不旁路应用服务**--写工具必须声明 `requires_confirmation` + `writes_via`,人确认后走各上下文正常应用服务(过聚合根不变式 + 事务发件箱),agent 不直写原始表。
3. **agent 只草拟不发布**--生成 intent + draft(动作卡),低置信度标 `need_human_review`,不自动路由 / 不自动发布。

---

## 2. 场景嵌入点验证

对着 `factorybot/app/orchestration/scenarios/specs.py` 的 3 个 ScenarioSpec,逐节点过三问,确认每个 agent 嵌入点都踩在"三问有一"上,并标出待收口的 gap。

### 2.1 验证方法

- 对每个 **agent 节点**(`run_agent(...)`)逐一过三问,记录命中数与理由。
- 对"看似该用 agent 却用了代码"的节点,确认三问是否皆否(代码合理)。
- 对占位桩 / 悬空能力,标 ⚠️ gap。

### 2.2 场景① CHANGEOVER(`specs.py:84`)

| 节点 | 性质 | 三问验证 |
|---|---|---|
| plan / first_article / process_switch | 代码 | 确定性查询 |
| tooling_check / kitting_check | 代码 | `expected == actual` 结构化比对 |
| barrier | 代码 | 确定性分流(`barrier.py:33`) |
| draft_release / gate_* | 代码 | 结构化拼装 + gate |
| **root_cause (A)** | **agent** | ①半(结构化 mismatch,但根因需跨 4 上下文语义区分)②**是**(根因假设 + 置信度)③**是**(根因空间开放)-> **2 是,成立 ✓** |

**两个"懂不用"的点,均正确**:

- **kitting FAIL -> suspend 不嵌 agent**:`barrier.py:48` 直接挂起推"催料"卡。缺料是确定的,三问皆否,代码挂起催料即可。
- **draft_release_card 是代码节点**:`query_compare.py:87` 结构化拼装,非 LLM。放行卡不让 LLM 生成(碰过点红线附近)。

### 2.3 场景② FAULT_RESPONSE(`specs.py:127`)

| 节点 | 性质 | 三问验证 |
|---|---|---|
| plan | 代码 | 确定性 |
| draft_repair_order | 代码 | 结构化拼装(`asset_id` / `fault_time` / `status`,`query_compare.py:95`)-> 三问皆否,代码 ✓ |
| **fault_impact (B)** | **agent** | ①半(遥测时序需形态判断)②**是**(故障模式推理 + 漂移窗口 + 敏感度关联)③**是**(三维动态)-> **2 是,成立 ✓** |
| gate_repair / gate_isolation | 代码 gate | gate 是代码 |
| gate_recalibration / gate_restart_first_article | 代码 gate | **复校 / 复产首件两道红线,agent 不碰** ✓ |

**验证结论**:agent 嵌入点只有 B,踩在 ②③ 上;维修单草拟走代码(结构化拼装)合理;复校 / 复产首件 gate 严格留给代码,符合硬边界。

### 2.4 场景③ COMPLAINT_8D(`specs.py:153`)

| 节点 | 性质 | 三问验证 |
|---|---|---|
| plan | 代码 | 确定性 |
| **traceability (C)** | **agent** | ①半(批次号结构化,跨 5 上下文证据需语义关联)②**是**(5M1E 加权排序)③**是**(同证据不同背景排序不同)-> **2 是,成立 ✓** |
| supplier_trace | 代码 | C 做完自适应取证后的并行确定性查在库品 -> 三问皆否,代码 ✓ |
| **isolation_scope** | **代码(占位桩)** | ⚠️ 见 2.5 |
| **draft_8d (D1)** | **agent** | ①**是**(追溯链 + 历史 8D 开放文本)②**是**(自然语言生成)③**是**(代码写不出)-> **3 是,成立(最干净)✓** |
| gate_isolation_8d / gate_8d_publish | 代码 gate | gate 是代码 |

### 2.5 验证发现:1 个 gap + 1 个悬空能力

**⚠️ Gap:`isolation_scope` 是空占位桩,与设计文档矛盾。**

`query_compare.py:107` 恒返 `{"batches": [], "reason": "determined_by_code"}`。但 [痛点方案](L3编排型Agent-痛点操作步骤与解决方案.md) 痛点 C 明确写:客诉场景的隔离范围判定"**同痛点 B 的动态判定**"--即故障模式 × 漂移窗口 × 产品敏感度,三问有两问"是",本该是 agent(复用 B 能力)或至少是有实质逻辑的代码。当前这个桩既不是 agent、也没实现,**隔离集恒为空**,等于 COMPLAINT_8D 的隔离分支是哑的。

两条出路,需拍板:

- **若客诉隔离范围其实可确定**(已知不良批次 -> 按供应商批次号查在库品集合):实现成真正的确定性查询代码节点,三问皆否成立,保持代码。
- **若确实需要动态判定**(关联敏感度 / 历史不良):改成 `run_agent("fault_impact")` 复用 B,不留空桩。

**⚠️ 悬空能力 + 分类错配:`draft_rework_craft` 不该是生成类 agent。**

`agents/__init__.py:42` 注册了 `draft_rework_craft`(原 D 生成类的返工工艺子能力),但 3 个场景 spec 里没有任何入口。上一轮把这条记为"缺场景"(补一个返工场景或标注未启用即可)——**这是症状,不是病因**。核代码后病因是**分类错配**:

1. **输出契约是选型不是生成**:`draft_agents.py:42` 输出 `{rework_route, reentry_point}`,mock 实吐 `RR-RW-1 / OP-REFLOW`(`mock_chat_model.py:183`)两个**指针**——返工路线 ID + 回起工位节点,不是工艺内容。prompt 自称"开放生成",输出契约不认。
2. **安全关键参数不在 agent 手里**:返工路线是版本化安全契约,`route_version` 强制 + `ACTIVE` 校验(`process_management.py:26-42`),温度/时间/工序都在库里由 ACL 守。agent 只输出指向库的 ref,碰不到参数;若真让 LLM 生成参数反而是严谨性事故。
3. **过三问三问皆否**:① 输入半结构化、目标空间(工艺库)结构化 -> 否;② 匹配 route + 选 reentry 工位,非生成 -> 否;③ `(不良模式 -> 返工路线)` 是库内有界映射 -> 否(常见 case)。按 §1.1 规则,**代码节点**。
4. **应用层已有归处**:`ReworkOrderDraftBuilder`(`rework_order.py:38`)已在草拟返工单,输出同样的 `reentry_point / rework_route_ref`。orchestration 层的 `draft_rework_craft` 是重叠悬空物。

**收口路径(替代"补返工场景")**:

- **主路径代码化**:返工工艺选型改成代码节点——`不良模式 -> query_route(路线库, route_version 强制 + ACTIVE 校验) -> (rework_route_ref, reentry_point)`,三问皆否,归 §1.3 代码层。
- **边角才上 agent**:仅"库未命中的新不良模式"调 agent 做候选推荐(推荐最接近的现有路线 + 偏差说明),输出仍是结构化选型,`low confidence + need_human_review`,参数绝不经 LLM。
- **`draft_rework_craft` 处置**:要么删(application 层 builder 已覆盖返工单草拟),要么降级为上面那个边角候选推荐 agent——不能以"生成类"原样留着,避免给人"已覆盖返工"的错觉。
- **D 类收敛**:D1 生成(8D/SOP,代码做不了)+ D2 选型(返工工艺,主路径代码)。原"代码完全做不了"的标签只贴在 D1 上。

### 2.6 验证总结

| 场景 | agent 嵌入点 | 三问结论 | 纪律执行 |
|---|---|---|---|
| CHANGEOVER | root_cause (A) | ②③ 成立 | ✓ kitting 缺料不嵌 agent、放行卡走代码 |
| FAULT_RESPONSE | fault_impact (B) | ②③ 成立 | ✓ 复校 / 复产首件红线留代码 |
| COMPLAINT_8D | traceability (C) + draft_8d (D1) | C:②③ / D1:①②③ 成立 | ⚠️ isolation_scope 空桩、draft_rework_craft 分类错配(见 §2.5) |

**结论:4 个 agent 嵌入点(A / B / C / D1 各一)全部踩在"三问有一"上,选型纪律执行干净--没有任何一个确定性步骤被错配给 LLM。** 上一轮砍掉工艺切换场景是纠偏(`69873a0`);这一轮再把 `draft_rework_craft` 从 D 生成类里剔出来归代码层(选型非生成),是同一方向的纠偏。3 个场景的 agent 嵌入点都站得住。待收口:`isolation_scope` 空桩(功能性 gap,非选型错误)与 `draft_rework_craft` 分类错配(原算生成、实为选型,主路径代码化,见 §2.5)。

---

## 3. 一句话定位

> L3 编排型 Agent = **编排代码层(确定性)+ 只在 4 类非确定决策点(A 根因 / B 隔离范围 / C 根因排序 / D1 生成)嵌入 agent**。判断"该不该用"先过三问--三问皆否就用代码强校验 / 硬逻辑,那更快、更可靠、更可审计,agent 在这里是负资产(过点红线附近甚至是风险);三问有一才上 agent,且写动作过 confirmation gate、走应用服务、不进过点主事务。原 D 类里的返工工艺已重分类为 D2 选型(主路径代码化,三问皆否),不在 agent 嵌入点内。**懂得什么时候不用 AI,比硬塞 agent 值钱得多。**

---

## 附:面试防守 Q&A

**Q:换线防错如果代码(barrier + 应用服务不变式)就能做,为什么还要上 agent?是不是为了用 AI 而用 AI?**
A:该问。判断标准是三问--输入是否开放 / 是否需推理生成 / 分支是否难穷举。换线里步骤顺序、齐套率、钢网号比对、程序版本校验、barrier 未双 PASS 不放行,全是确定规则,代码做 agent 不掺和。agent 只在 mismatch 后推理根因(A)、故障隔离范围(B)、客诉 5M1E 排序(C)、8D 生成草拟(D1)这 4 处赚回成本。原 D 类里的返工工艺已重分类为 D2 选型(输出是指针不是工艺内容,三问皆否),归代码层,不在 agent 嵌入点内--这是"懂不用"的又一次纠偏。当前 3 个场景的 4 个 agent 嵌入点逐个验过,都踩在"三问有一"上;换线全程 PASS 时 LLM 调用为 0。

**Q:这些场景你都上线了吗?**
A:没有。这是设计阶段的引入规划,落地需先等 RAG 追溯能力成型,再做 L1 / L2 验证,L3 是试点形态。本文的"验证"是对着当前代码的静态选型审查,确认 agent 嵌入点没站错位置,不编业务效果数字。

**Q:`isolation_scope` 为什么是空桩?**
A:这是待收口的功能性 gap,不是选型错误。设计文档说客诉隔离范围"同痛点 B 的动态判定"(该是 agent),但当前实现是空代码桩,隔离集恒为空。两条出路:客诉隔离若可确定就实现成确定性查询代码;若需动态判定就复用 B 能力。不管哪条,不能留空桩给人"已覆盖隔离"的错觉。

**Q:返工工艺草拟原本算 D 生成类,为什么又剔出来归代码层?是不是需求没想清楚来回改?**
A:不是来回改,是上一轮分类错配、这一轮按三问纠偏。核代码:`draft_rework_craft` 输出的是 `{rework_route, reentry_point}` 两个**指针**(路线 ID + 回起工位),不是工艺内容;返工路线的温度/时间/工序都在版本化路线库里由 ACL 守(`route_version` 强制 + `ACTIVE` 校验),agent 碰不到参数。过三问:输入半结构化、匹配 route 非生成、`(不良模式 -> 返工路线)` 是库内有界映射--三问皆否,按规则就是代码节点。prompt 自称"开放生成"是贴错标,输出契约不认。而且 application 层 `ReworkOrderDraftBuilder` 已经在草拟返工单,orchestration 层这个 agent 是重叠悬空。真要上 agent,只在"库未命中的新不良模式"边角做候选推荐,输出仍是结构化选型 + `need_human_review`,参数绝不经 LLM--否则就是严谨性事故。这正是"懂什么时候不用 AI":把选型硬塞给生成类,一问"输出是不是自然语言"就露馅。
