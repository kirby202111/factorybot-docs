# Agent 成本优化接入 · 面试闪光点与话术（悬空组件落地 / 护栏读全文 LLM 读摘要 / 降本不牺牲证据链）

> **定位**：本文是对 factorybot（MES Agent 服务）清单 #25 一条 P0 的面试纵深展开。cost 子系统 7 个降本组件（ModelRouter/EvalGate/CacheControl/ResultCompactor/EarlyStopDetector/PhaseToolBinder/ToolResultCache）设计完备、有边界单测、文档宣传，但 `app/` 下零调用--"设计+文档+零上线"。这次把其中最干净的 ResultCompactor 接入 ReAct 主流程。守口径纪律：做了什么如实讲（思路 D 接入 + #15 告警化），不确定的交还用户拍板（分阶段方向、#15 裁剪强度），残留诚实交代（6 组件仍悬空、real 模式诊断质量未验证）。
>
> **核心矛盾**：接入 ResultCompactor 最大的陷阱是"在 ToolNode 压缩 tool 消息"--会破坏诊断护栏 `_guard_no_evidence`。护栏读 `state.messages` 里 tool 消息的全文 `data` 找 BLOCK/FAIL 证据；`query_traceability_graph` 的字段白名单不含 `nodes`，压缩后 `nodes` 被裁，护栏漏判追溯图里的 BLOCK 节点，把"有不良证据"误判成"证据不足"。**降本（省 token）和证据链完整性（护栏读全文）是冲突的，必须解耦**。一个判断贯穿全文：**护栏读全文、LLM 读摘要**--压缩只作用于喂 LLM 的副本，state.messages 全文不动。

---

## 0. 口径纪律（先读这一条）

| 类别 | 能不能讲 | 怎么说 |
|------|---------|--------|
| 已做（ResultCompactor 接入思路 D / #15 告警化 / DI 透传 / 测试） | ✅ 直接讲 | 有代码有测试可验证 |
| 测试（73 passed，新增 8 用例含护栏安全断言） | ✅ 直接给 | pytest 实跑 |
| 决策点（分阶段方向 / #15 裁剪强度） | ✅ 直接讲 | 交还用户拍板，有权衡 |
| "cost 子系统全部上线" / "real 模式验证省 token" | ❌ 禁止 | 6 组件仍悬空，real 未验证 |
| 残留（6 组件悬空 / EvalGate 无数据源 / real 诊断质量未测 / 白名单未补全） | ⚠️ 主动交代 | 诚实是加分项 |
| "压缩后诊断质量不变" | ⚠️ 限定讲 | mock 验证不破坏护栏，real 对根因推理的影响未测 |

一句话：**接入讲透思路 D、决策点摆明谁拍的板、6 组件悬空不藏，价值在"降本不牺牲证据链"的解耦判断不在"接了几个组件"**。

---

## 1. 30 秒电梯陈述

"我在 MES Agent 服务把一个悬空的降本组件接进了主流程。cost 子系统 7 个组件--模型路由、评测门禁、结果压缩、早停、prompt 缓存等--设计完备有单测，但 app 下零调用，是'设计+文档+零上线'。这次接入最干净的 ResultCompactor（工具结果回灌 LLM 前压缩省 token）。最大的坑是：护栏 _guard_no_evidence 读 tool 消息全文找 BLOCK/FAIL 证据，如果在 ToolNode 压缩，query_traceability_graph 白名单不含 nodes，压缩后 nodes 被裁，护栏会把'有不良'误判成'证据不足'。所以我把压缩点放在 model_node 喂 LLM 前--state.messages 全文不动给护栏和 trace，喂 LLM 的副本压缩，护栏读全文、LLM 读摘要，降本和证据链解耦。顺带修了 #15：无白名单工具告警+透传，不替领域拍板裁哪些字段。DI 注入式透传 9 个文件。测试 73 全过，新增 8 个用例（7 压缩单测 + 1 个 #15 告警），其中 test_defect_still_detected_from_full_messages 锁了'压缩裁掉 nodes 后护栏仍从原文检出 BLOCK'这个安全属性。6 个组件仍悬空我标注了阻塞依赖，交还用户分阶段推进。"

**三个抓手**：悬空识别+分阶段 / 护栏读全文 LLM 读摘要 / 降本不牺牲证据链。

---

## 2. 闪光点详解（按主线）

### A. 悬空识别 + 分阶段决策（不一刀切）

#### A.1 7 组件设计完备却零调用

**识别过程**：清单 #25 标注 cost 子系统整体悬空。我核实：7 个组件都有边界单测（[test_cost.py](../factorybot/tests/test_cost.py) 169 行覆盖很全），逻辑完备，但 `app/` 下只有 container 实例化 ModelRouter+EvalGate，且 ModelRouter 只调 `validate_on_startup`（启动打 warn），`route()` 零调用；`ai/` 目录完全不 import cost；[tool_node.py:92](../factorybot/app/infrastructure/ai/tool_node.py) 注释"可由 ResultCompactor 压缩"从未执行。整套是"设计+文档+零上线"。

**为什么是亮点**：能区分"有测试"和"被调用"。7 个组件单测全绿，容易让人以为"已上线"。但单测验的是组件自身逻辑，不验"是否接入调用链"。识别"零调用"要 grep 调用方，不是看测试绿。多数人看到组件有测试就放心，看不到"测试绿但没人用"的悬空。

#### A.2 分阶段：A/B/C 三选一不一刀切

清单建议三个方向：A 接入主流程 / B 文档标注未上线 / C 删除代码+下掉设计文档。我没自行选，而是先评估各组件接入的阻塞依赖，发现"接入"不是全有全无的二选一：
- ResultCompactor：低风险、立即省 token、无阻塞
- ModelRouter.route()：被 LLM 单例->多实例架构改造 + provider 模型映射 + EvalGate 评测数据源三重阻塞
- CacheControl：强依赖 anthropic provider（mock/openai/deepseek 不可用）
- EarlyStop：需给 ReAct 加 state 证据计数通道
- PhaseToolBinder/ToolResultCache：默认关闭/灰度，语义风险

所以"接入"本身就是分阶段的。我把这个判断 + A/B/C 方向交给用户拍板，用户选"分阶段接入"，第一阶段先接 ResultCompactor。

**为什么是亮点**：**不把"接入"当成全有全无的单选**。识别出 7 个组件接入难度差异巨大（ResultCompactor 无阻塞 vs ModelRouter 三重阻塞），把"分阶段"作为更务实的选项呈现。面试官问"你怎么决定接哪个"，答"我评估了每个组件的阻塞依赖，ResultCompactor 无阻塞先接，ModelRouter 被 EvalGate 数据源阻塞留后续，不是按顺序接是按就绪度接"。

**防守话术**："7 个组件接入难度差很多。ResultCompactor 无依赖先接，ModelRouter 要改 LLM 单例架构还要 EvalGate 评测数据，CacheControl 要 anthropic provider，硬接是空转。所以分阶段，按就绪度不是按顺序。方向 A/B/C 我交还用户拍板，用户选分阶段接入。"

---

### B. 思路 D：护栏读全文、LLM 读摘要（核心设计）

#### B.1 在 ToolNode 压缩会破坏 _guard_no_evidence

**识别过程**：ResultCompactor 的 docstring 写"工具结果回灌前压缩"，字面理解是在 ToolNode 把 tool 消息的 data 压缩。但 [_guard_no_evidence](../factorybot/app/infrastructure/ai/graph_builder.py)（graph_builder.py）在 finalize 时扫描 `state.messages` 里 tool 消息的 `data` 找不良证据：追溯图的 nodes 里 `properties.decision==BLOCK`、过点记录 `decision==BLOCK`、测试 `raw_verdict==FAIL`、不良数>0。

`query_traceability_graph` 的 FIELD_WHITELIST 是 `["serial_no","subgraph_ref","version","version_kind","version_ref_id"]`--**不含 nodes**。如果在 ToolNode 压缩，nodes 被裁，`_guard_no_evidence` 扫不到 nodes 里的 BLOCK 节点，把"有不良证据"误判成"证据不足"，强制改成 `needs_human_review=true`。**降本把诊断护栏弄瞎了**。

**为什么是亮点**：这是"字面执行 docstring vs 理解数据流"的区别。"回灌前压缩"字面是 ToolNode，但 ToolNode 写入的 `state.messages` 是护栏的数据源。压缩和护栏共用同一份 data，冲突。识别这个冲突要追 `_guard_no_evidence` 读什么（state.messages 全文），不是只看 ResultCompactor 的接口。多数人会按 docstring 在 ToolNode 接，接完测试绿（因为压缩逻辑对），但护栏悄悄失效--测试绿但诊断质量退化。

#### B.2 压缩点放 model_node，state.messages 全文不动

**处理**（思路 D）：压缩点放在 [react_graph.py](../factorybot/app/infrastructure/ai/react_graph.py) 的 `model_node`--构造喂 LLM 的 messages 时，对 history 里 `role==tool` 的消息解析 content，压缩 `data`，重组序列化；`trace_id` 顶层保留不动。**state.messages 始终存全文**（ToolNode 不改），护栏和 trace 读全文不受影响；只有喂 LLM 的临时副本被压缩。

模块级 `_compact_tool_history(history, compactor)`：compactor 为 None 时原样返回（向后兼容）；对 tool 消息解析 payload，compact data，重组；非 tool 消息和解析失败的原样返回。

**为什么是亮点**：**让降本和证据链解耦**。护栏读全文（state.messages）、LLM 读摘要（临时副本），两份数据各司其职。压缩再狠也不碰护栏的数据源。这比"在 ToolNode 压缩 + 调整白名单保留 nodes"高明--后者要白名单感知护栏需求（耦合），且 nodes 可能很大保留它没省到。思路 D 让 ResultCompactor 保持纯逻辑（只管压缩），护栏保持独立（只读全文），符合 SOLID 的单一职责。面试官问"为什么不调白名单加 nodes"，答"加 nodes 要 ResultCompactor 感知护栏需求，耦合；且 nodes 大保留没省到；思路 D 让两者读不同副本，彻底解耦"。

**防守话术**："护栏 _guard_no_evidence 读 state.messages 的 tool 消息全文找 BLOCK。在 ToolNode 压缩会把 nodes 裁掉，护栏瞎了。我把压缩放 model_node 喂 LLM 前，state.messages 全文不动给护栏和 trace，喂 LLM 的副本压缩。护栏读全文、LLM 读摘要，降本不碰证据链。"

---

### C. 三路解耦的安全验证

压缩接入影响三条链路，逐一验证不受影响：

1. **护栏 _guard_no_evidence**：读 state.messages 全文。思路 D 不写回 state，全文不动。新增 `test_defect_still_detected_from_full_messages`：构造追溯图含 BLOCK 节点的 tool 消息，压缩后副本的 nodes 确实被裁，但原文 history 仍含 nodes，`_has_defect_evidence(history)` 仍 True。锁死"压缩不污染护栏数据源"。
2. **MockChatModel**：`_diagnosis` 从喂它的 tool 消息收 `trace_id`（[mock_chat_model.py:67](../factorybot/app/infrastructure/ai/mock_chat_model.py)），不读 data。压缩保留 trace_id 顶层（payload 顶层不动），mock 仍能收集 trace_id。端到端诊断/编排测试全过。
3. **ObservableChatModel**：纯透传包装（`await self._inner.ainvoke(messages, tools)`），不解析 tool 消息内容做分支。压缩版 messages 透传给真实 LLM。`_est_tokens` 用 `len(content)` 估 token，压缩后估值反而更准。

**为什么是亮点**：**接入前先识别会影响哪些链路，逐一验证**，不是接完跑测试碰运气。三条链路读 tool 消息的不同部分（护栏读 data 全文、mock 读 trace_id、观测透传），思路 D 让三者各取所需。尤其护栏那条--如果不验证，"压缩后 nodes 被裁导致护栏漏判"这个 bug 会在 real 模式诊断时才暴露。我显式写了断言锁它。

**防守话术**："接入前我追了三条读 tool 消息的链路：护栏读 data 全文、MockChatModel 读 trace_id、ObservableChatModel 透传。思路 D 让护栏读 state.messages 全文不动、mock 的 trace_id 顶层保留、观测透传无感。每条都验证，还写了断言锁'压缩裁 nodes 后护栏仍检出 BLOCK'。不是接完碰运气。"

---

### D. #15 不替领域拍板（无白名单告警+透传）

**识别**：#15 指无 FIELD_WHITELIST 的工具整包透传给 LLM，可能撑爆上下文。ResultCompactor 对无白名单工具是 `out = dict(view)` 整包返回。

**处理决策**：两个选项--(a) 无白名单时强制裁剪（保留标量顶层键，丢嵌套 dict）；(b) 无白名单时告警+维持透传。我选 (b)。理由：无白名单工具"保留哪些字段"是领域决策（影响模型看到什么证据 vs 省 token），第一阶段不替领域拍板。告警让缺口可见（#15 建议明确列"或无白名单时告警"），运维/开发看到 `cost.result_compactor.no_whitelist` warning 知道哪个工具没配白名单在烧 token。

**为什么是亮点**：**工程纪律--不借接入之名夹带领域决策**。无白名单裁剪强度（留标量？留列表截断？丢嵌套？）直接影响诊断质量，是领域专家的决策，不是接入工程师的。强行裁剪可能砍掉模型需要的证据。告警+透传是最安全的过渡：有白名单的 3 个工具（pass_records/test_results/traceability_graph）立即省 token，无白名单的工具维持现状只告警，把"补白名单"留给领域迭代。这与"去重不夹带行为变更"同构的纪律。

**防守话术**："#15 无白名单工具整包透传。强制裁剪要决定留哪些字段，那是领域决策影响诊断质量，我不替领域拍板。选告警+透传：有白名单的 3 个工具立即省 token，无白名单的维持现状打 warning 让缺口可见。补白名单留给领域迭代。"

---

### E. DI 注入式装配（架构一致性）

**处理**：ResultCompactor 通过 `build_react_graph` 的 `result_compactor` 参数注入，[container.py](../factorybot/app/container.py) 装配单例，经 `build_diagnosis_graph` / `build_agent_registry` 透传到 4 个 agent + diagnosis_service。共 9 个文件加参数透传。

**备选**（未选）：`build_react_graph` 内部默认 `ResultCompactor()`（3 文件改动），代价是失去 DI/可配置性、与 container 已有的 cost 装配区（eval_gate/model_router）不一致。

**为什么是亮点**：**选注入式是因为 container 已经有 cost 装配区**（eval_gate/model_router 在 container 实例化），ResultCompactor 也该在那里装配，保持一致。虽然 9 文件透传比 3 文件内部构造重，但都是机械加参数，低风险，且符合项目既有 DI 风格。CLAUDE.md 明确要求 SOLID + DI + 低耦合。面试官问"为什么不用内部构造省事"，答"container 已有 cost 装配区，内部构造会让 ResultCompactor 成为唯一在 build_react_graph 内 new 的 cost 组件，不一致；且失去可配置/可关闭"。

**防守话术**："选 DI 注入式，9 文件透传。因为 container 已有 cost 装配区（eval_gate/model_router），ResultCompactor 也该在那装配保持一致。内部构造省事但不可配置、风格不统一。透传是机械加参数低风险。"

---

### F. 诚实：6 组件阻塞 + real 未验证

**主动交代**：
- 6 组件仍悬空（ModelRouter/CacheControl/EarlyStop/PhaseToolBinder/ToolResultCache/EvalGate），阻塞依赖已在 [cost/__init__.py](../factorybot/app/infrastructure/cost/__init__.py) 和清单 #25 标注。
- real 模式诊断质量未验证：压缩后模型看到的是字段摘要，real LLM 对根因推理的影响没测（mock 下 MockChatModel 不依赖 data 内容推理）。
- 逐工具 FIELD_WHITELIST 补全未做（领域决策）。
- 压缩省 token 量未度量（`_est_tokens` 估值反映但不精确，metric 留 P1 可观测块）。

**为什么是亮点**：**知道自己不知道什么**。接入一个组件就说"cost 子系统上线了"是吹牛。诚实标 1/7，把剩余 6 个的阻塞依赖写清楚（ModelRouter 待 EvalGate 数据源、CacheControl 待 anthropic provider），让"后续怎么推进"可见。real 诊断质量未测是关键残留--压缩可能让模型证据不足影响根因准确率，这个风险我标出来不藏。

**防守话术**："只接了 1/7。其余 6 个有阻塞：ModelRouter 待 EvalGate 评测数据源、CacheControl 待 anthropic provider、EarlyStop 待 state 通道。real 模式压缩对诊断质量的影响没测--mock 下 MockChatModel 不靠 data 内容推理，real LLM 看摘要可能影响根因，这个风险我标了。不吹全部上线。"

---

## 3. 核心应答话术（高频问题，口语化背熟）

### 话术 1：你这次最核心的判断是什么

"最核心是**护栏读全文、LLM 读摘要**。接入 ResultCompactor 最大的坑是 _guard_no_evidence 护栏读 tool 消息全文找 BLOCK 证据，如果在 ToolNode 压缩，query_traceability_graph 白名单不含 nodes，压缩后 nodes 被裁，护栏把'有不良'误判成'证据不足'。所以我把压缩放 model_node 喂 LLM 前，state.messages 全文不动给护栏和 trace，喂 LLM 的副本压缩。降本和证据链解耦，这是最大的判断。"

### 话术 2：为什么不在 ToolNode 压缩，docstring 不是说回灌前压缩吗

"docstring 说'回灌前压缩'，字面是 ToolNode，但 ToolNode 写入的 state.messages 是护栏的数据源。压缩和护栏共用同一份 data 冲突。护栏读全文找 BLOCK，压缩要裁 data 省 token，在 ToolNode 压缩就是把护栏的数据源裁了。所以压缩点放 model_node，护栏读 state.messages 全文、LLM 读压缩副本，两份数据各司其职。docstring 的'回灌前'我理解为'喂 LLM 前'，不是'写消息时'。"

### 话术 3：你怎么保证压缩不破坏诊断

"接入前追了三条读 tool 消息的链路逐一验证：护栏读 data 全文（思路 D 不写回 state，全文不动）、MockChatModel 读 trace_id（顶层保留）、ObservableChatModel 透传（无感）。还写了 test_defect_still_detected_from_full_messages：压缩后副本的 nodes 确实被裁，但原文 history 仍含 nodes，_has_defect_evidence 仍检出 BLOCK。锁死'压缩不污染护栏数据源'。但诚实讲，这只验了不破坏护栏，real LLM 看摘要对根因推理的影响没测，是残留。"

### 话术 4：7 个组件只接了 1 个，其余呢

"分阶段。ResultCompactor 无阻塞先接。其余 6 个有依赖：ModelRouter 要改 LLM 单例->多实例架构还要 EvalGate 评测数据源（现在 _results 恒空 passed 恒 False，门禁是摆设）；CacheControl 强依赖 anthropic provider，mock/openai/deepseek 不可用；EarlyStop 要给 ReAct 加 state 证据计数通道；PhaseToolBinder/ToolResultCache 默认关闭/灰度。阻塞依赖我标在 cost/__init__.py 和清单 #25。方向 A/B/C 交还用户拍板，用户选分阶段接入。后续待 EvalGate 数据源和 provider 目标确定再起第二阶段。"

### 话术 5：#15 无白名单工具你怎么处理的

"无白名单工具告警+透传，不强制裁剪。强制裁剪要决定留哪些字段，那是领域决策影响诊断质量，我不替领域拍板。选告警：有白名单的 3 个工具（pass_records/test_results/traceability_graph）立即省 token，无白名单的维持现状打 warning 让缺口可见。补白名单留给领域迭代。#15 建议里'或无白名单时告警'就是这选项。"

---

## 4. 深度问答（技术深挖）

**Q：压缩放 model_node，每步都重新压缩所有历史 tool 消息，性能不亏吗？**
A：亏很小。history 里 tool 消息数 = 已执行工具调用数，受 recursion_limit（诊断 20、编排 40）约束，每步重压缩的是常数条消息，JSON 解析+序列化开销微秒级，相对 LLM 调用（百毫秒~秒级）可忽略。且压缩省的 token 直接降 LLM 成本和延迟，净赚。真要优化可缓存压缩结果（按 content 哈希），但当前没必要。

**Q：state.messages 存全文，checkpointer 持久化时全文落库，没省到存储？**
A：对，state.messages 全文落库（编排场景有 checkpointer）。但降本的目标是 **LLM token 成本**（喂 LLM 的副本压缩了），不是存储。存储成本远低于 LLM token 成本（MySQL 一行 vs LLM 每千 token 计费）。且全文落库是证据链持久化的要求（审计/回溯要看全文），不能为省存储裁。思路 D 精准降的是 LLM token，不动证据链存储。

**Q：query_traceability_graph 压缩后模型看不到 nodes 详情，怎么推理根因？**
A：模型看到白名单字段（serial_no/subgraph_ref/version）+ trace_id。nodes 详情通过 trace_id 查 tool_call_trace（全文落库）。这是 ResultCompactor 的设计哲学"模型看摘要、trace 落全文"。但这是 trade-off：压缩越狠越省 token 但模型证据越少。FIELD_WHITELIST 是领域逐工具定的（保留推理所需字段），我没改它。real 模式这个 trade-off 对根因准确率的影响没测，是残留。

**Q：为什么 ResultCompactor 装在 container 而不是 build_react_graph 内部 new？**
A：container 已有 cost 装配区（eval_gate/model_router 在那实例化），ResultCompactor 也该在那，保持一致。内部 new 会让它成为唯一在 build_react_graph 内构造的 cost 组件，风格不统一；且失去可配置（truncate 参数）和可关闭（传 None）。9 文件透传是机械加参数，低风险。符合项目 DI 风格和 CLAUDE.md 的 SOLID 要求。

**Q：分阶段接入，你怎么决定哪个组件先接？**
A：按就绪度不是按顺序。评估每个组件的阻塞依赖：ResultCompactor 无外部依赖（纯逻辑）先接；ModelRouter 要改 LLM 单例架构 + provider 映射 + EvalGate 数据源，三重阻塞留后；CacheControl 要 anthropic provider；EarlyStop 要 state 通道。先接无阻塞的见效，有阻塞的等依赖就绪。这比按 1-7 顺序接务实。

**Q：_compact_tool_history 为什么做成模块级函数不是闭包？**
A：为了可测。model_node 是 build_react_graph 内的闭包，难直接测。`_compact_tool_history` 提到模块级，参数化 (history, compactor)，能直接单测压缩行为、trace_id 保留、原文不被 mutate、护栏安全。测试不依赖装配整个图。可测性是设计考虑。

---

## 5. 压力追问（陷阱题，考诚实）

**Q：你说压缩不破坏护栏，但 real 模式 LLM 看摘要可能影响根因准确率，这不是拿诊断质量换 token 吗？**
A：诚实讲，这是真实 trade-off，我没测 real 影响是残留。但有几点缓解：一是 FIELD_WHITELIST 是领域逐工具定的，保留的是推理关键字段（pass_records 留 decision、test_results 留 raw_verdict），不是乱裁；二是模型看摘要+trace_id，要详情能查 trace 全文；三是 _guard_no_evidence 护栏读全文不受压缩影响，证据不足会强制转人工不会硬出结论。但 real LLM 在摘要下能否准确推理我没验证。生产前要补 real 诊断质量对比测试。**不吹"压缩不影响诊断"，标了残留**。

**Q：6 个组件还悬着，这次只接 1 个，是不是避重就轻挑软柿子捏？**
A：不是挑软柿子，是按就绪度分阶段。ResultCompactor 无阻塞、低风险、立即省 token，先接见效。其余 6 个不是不接是接不了/不该现在接：ModelRouter 接了是空转（EvalGate 无数据源门禁是摆设）；CacheControl 接了只对 anthropic 有用（当前 provider 可能不匹配）；EarlyStop 要改 state 通道。硬接是制造"假上线"（接了但不生效或破坏行为）。分阶段是诚实推进，不是避重就轻。第二阶段待 EvalGate 数据源和 provider 目标确定再起。

**Q：你在 model_node 压缩，但 ObservableChatModel 的 tracing 记的是压缩版 messages，trace 不就丢全文了吗？**
A：没丢。LLM call trace 记压缩版（反而省 trace 体积），但工具结果的全文在 tool_call_trace_repo（ToolNode 的 save_ok 存的是 view_dict 全文，不经过压缩）。证据链全文由 tool_call_trace 保全，不是 LLM call trace。两套 trace 各司其职：tool_call_trace 存工具结果全文（证据链），LLM call trace 存模型交互（含压缩版输入）。审计要全文查 tool_call_trace。

**Q：#15 你选告警+透传，那无白名单工具还是整包透传烧 token，#15 不是没修吗？**
A：修了一半。#15 两个建议：定义默认白名单（裁剪）或无白名单时告警。我选告警选项。有白名单的 3 个工具（pass_records/test_results/traceability_graph）接入后立即压缩省 token，#15 对它们解决了。无白名单工具告警让缺口可见（运维知道哪个工具在烧 token），但没强制裁剪--因为裁哪些字段是领域决策。强制裁剪我留后续（领域补白名单）。所以 #15 是"告警化让缺口可见 + 有白名单工具生效"，不是"全部裁剪"。诚实地讲是无白名单工具的裁剪留后续。

**Q：你交还用户拍板分阶段，是不是自己没主见不敢定？**
A：有主见但不替用户定。我有推荐（分阶段先接 ResultCompactor），但 A/B/C 方向（接入/标注/删除）影响项目走向，是用户的决策不是纯技术对错。#15 裁剪强度影响诊断质量是领域决策。把推荐+权衡摆给用户，用户定我执行。有推荐但不替用户拍板，是技术上有判断、责任上知边界。

---

## 6. 指标卡片（背下来）

### 卡片 A：改动规模（最硬，直接给）

| 维度 | 数值 | 出处 |
|------|------|------|
| 改动文件 | 13（改 11 / 增 2） | git diff --stat |
| 接入组件 | 1/7（ResultCompactor） | cost/ |
| 测试 | 73 passed（新增 8 用例） | pytest 实跑 |
| 新增压缩单测 | 7（含护栏安全断言） | [test_react_graph_compaction.py](../factorybot/tests/test_react_graph_compaction.py) |
| #15 告警测试 | 1（无白名单 warning） | [test_cost.py](../factorybot/tests/test_cost.py) |
| #15 修复 | 无白名单告警+透传 | [result_compactor.py](../factorybot/app/infrastructure/cost/result_compactor.py) |
| DI 透传文件 | 9（graph_builder/4 agent/diagnosis_service/agents __init__） | - |
| 交还用户决策点 | 2（分阶段方向 / #15 裁剪强度） | 本次对话 |
| 仍悬空组件 | 6 | [cost/__init__.py](../factorybot/app/infrastructure/cost/__init__.py) 标注 |

### 卡片 B：接入判断与风险

| 判断/风险 | 处理 |
|------|------|
| 在 ToolNode 压缩会破坏 _guard_no_evidence（裁掉 nodes） | 思路 D：压缩放 model_node，state.messages 全文不动 |
| 护栏读全文 vs LLM 读摘要 冲突 | 解耦：护栏读 state.messages，LLM 读压缩副本 |
| MockChatModel 读 trace_id | 压缩保留 trace_id 顶层 |
| ObservableChatModel 透传 | 无感，token 估值更准 |
| #15 无白名单工具烧 token | 告警+透传，裁剪留领域决策 |
| DI vs 内部构造 | 选 DI 注入式，与 container cost 装配区一致 |
| 6 组件悬空 | 标注阻塞依赖，分阶段后续 |

### 卡片 C：能力表述（讲做了什么）

| 能力 | 表述 |
|------|------|
| 悬空识别 | 区分"有测试"与"被调用"，识别 7 组件零调用 |
| 数据流分析 | 追 _guard_no_evidence 读 state.messages 全文，识破 ToolNode 压缩的破坏 |
| 解耦设计 | 护栏读全文、LLM 读摘要，降本不牺牲证据链 |
| 安全验证 | 三路解耦（护栏/mock/观测）逐一验证 + 断言锁安全属性 |
| 工程纪律 | 不替领域拍板（#15）、DI 一致性、分阶段按就绪度 |
| 诚实 | 1/7 标注、6 组件阻塞、real 诊断质量未测主动交代 |

---

## 7. 红线与遗留（面试别翻车）

**红线**：
- ❌ "cost 子系统上线了" / "降本已生效" -- 只接 1/7，6 组件仍悬空。
- ❌ "压缩不影响诊断质量" -- mock 验证不破坏护栏，real LLM 看摘要对根因的影响未测。
- ❌ "real 模式验证过省 token" -- mock 测试，real 未跑。
- ❌ "分阶段方向我自己定的" -- 交还用户拍板。
- ✅ 正确讲法："我把悬空的 ResultCompactor 接入 ReAct，核心是思路 D--压缩放 model_node 喂 LLM 前，state.messages 全文给护栏和 trace，LLM 读压缩副本，降本不破坏 _guard_no_evidence 证据链。顺带 #15 无白名单告警化。DI 注入式透传 9 文件，73 测试过，新增用例锁了护栏安全属性。6 组件仍悬空标了阻塞依赖，分阶段交还用户。real 诊断质量未测是残留。"

**遗留项清单（主动交代，反加分）**：

| 遗留 | 现状 | 倾向 |
|------|------|------|
| ModelRouter.route() 接入 | 悬空，route() 零调用 | 待 LLM 单例->多实例 + EvalGate 数据源 |
| EvalGate 评测数据源 | _results 恒空，门禁摆设 | 待建评测流程 |
| CacheControl 接入 | 悬空 | 待 anthropic provider 目标确定 |
| EarlyStopDetector 接入 | 悬空 | 待 ReAct state 证据计数通道 |
| PhaseToolBinder/ToolResultCache | 默认关闭/灰度 | 待启用决策 |
| real 模式诊断质量 | 压缩对根因准确率影响未测 | 生产前补 real 对比测试 |
| 逐工具 FIELD_WHITELIST 补全 | 仅 3 个工具有白名单 | 领域决策，逐工具补 |
| 压缩省 token 量度量 | _est_tokens 估值反映 | 待 P1 可观测块加 metric |

被问"还有什么没做"时，老实列出遗留 + 倾向，体现**知道自己不知道什么**，比假装完成得分高。

---

## 8. 一句话定位（收尾用）

"这次的价值不在接了几个组件，而在**识破'在 ToolNode 压缩会破坏诊断护栏'的陷阱--护栏 _guard_no_evidence 读 tool 消息全文找 BLOCK，压缩要裁 data 省 token，两者冲突；用思路 D 让护栏读 state.messages 全文、LLM 读压缩副本，降本不牺牲证据链完整性**--做了的讲透、决策点摆明谁拍的板、6 组件悬空不藏，与 MES 追溯的'证据链不可断'同构：省 token 不能以弄瞎护栏为代价。"
