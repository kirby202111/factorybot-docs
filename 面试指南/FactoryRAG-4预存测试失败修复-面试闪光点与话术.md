# FactoryRAG 4 预存测试失败修复 · 面试闪光点与话术（放松断言反挖 flaky 根因 / 三层根因穿透 / docstring 与实现契约背离 / 考古证非回归）

> **定位**：本文是对 FactoryRAG 4 个**预存测试失败**清理的面试纵深展开，接续 [httpx 资源泄漏修复](FactoryRAG-httpx资源泄漏修复-面试闪光点与话术.md) §7 遗留里的"4 个预存测试失败 | 独立待办线"。与 httpx（资源生命周期）、[agentic 集成测试补全](FactoryRAG-agentic集成测试补全-面试闪光点与话术.md)（测真实路径）不同，这次是**测试债务清理 + 缺陷发现**：4 个失败一直被当"无关噪音"搁置（记忆里都写了"与本次无关"），认真查发现它们是 3 类不同性质的问题混在一起，而修其中一个**反而挖出一个潜伏的 flaky 根因**。守口径纪律：4 个里只有 1 个是真生产 bug（正则）、1 个是测试基建 bug（FakeEmbedder flaky），另 2 个是测试期望/断言问题--不把测试调整说成修 prod bug；flaky 是修 #1/2 时撞上的、不是预先规划，诚实讲。
>
> **核心矛盾**：4 个失败被前一个失败的断言**挡着**，深层问题一直没暴露。`test_recall_relevance` 的查询循环里 query 6 先挂（ESD 词面命中"电子制造"进 top-20，exclude 过严），测试到不了 query 7；而 query 7 的路径恰好踩中 `FakeEmbedder` 的非确定性--一个 docstring 自称"确定性"、实现却用按进程随机的 `hash()` 的 flaky bug。我按用户选的方案把 query 6 的 exclude 放宽到 top-5，query 6 过了、query 7 浮出来，flaky 才显形。**放松断言通常是在掩盖问题，这次反而挖出了更深的**。三个判断贯穿全文：**放松断言反挖根因**、**三层根因穿透（跟证据不跟假设）**、**读 docstring 不只读代码**。

---

## 0. 口径纪律（先读这一条）

| 类别 | 能不能讲 | 怎么说 |
|------|---------|--------|
| 已做的（SN/WO/BATCH 正则 / exclude top-5 / 删 v3 断言 / FakeEmbedder hashlib） | ✅ 直接讲 | 有 diff 有测试可查 |
| 4 个失败的定性（1 真 prod bug + 1 测试基建 bug + 2 测试期望/断言） | ✅ 直接讲，区分清楚 | 不把测试调整说成修 prod bug |
| flaky 是撞上的 | ✅ 直接讲 | 修 #1/2 放松 query 6 才让 query 7 浮出，不是预先规划 |
| 考古结论（非回归） | ✅ 直接讲 | worktree 在 5ae9fd0 同样 4 挂，证生而失败 |
| "彻底消灭 flaky" / "生产 embedder 已验证" | ❌ 禁止 | toy FakeEmbedder 改了，生产用真 bge-m3，没上线 |
| "4 个失败都是我修好的 prod bug" | ❌ 禁止 | 只 1 个真 prod bug（正则）+ 1 个测试基建，另 2 个是测试调整 |
| 残留（toy embedder / 生产 bge-m3 未对齐 / 3 个测试调整是判断非硬错） | ⚠️ 主动交代 | 诚实是加分项 |

一句话：**定性讲准（4 个不同性质）、根因讲透（三层）、flaky 撞上的不揽功、考古证非回归、残留不藏，价值在"放松断言反挖根因 + 契约审查"不在"修了几个测试"**。

---

## 1. 30 秒电梯陈述

"FactoryRAG 有 4 个预存测试失败一直被当无关噪音搁置。我先做考古--worktree 切到版本通用化之前的 5ae9fd0，同样 4 挂，证是'生而失败'、非回归。逐个查根因：1 个真生产 bug（SN 正则 `[A-Z0-9]+` 遇内部 `-` 截断，SN-2024-001->SN-2024）；2 个测试期望/断言问题（exclude 检查 top-20 太严、`"v3" in action` 受 120 字截断影响太脆）。按用户选的方案修：正则补 `(?:[-_][A-Z0-9]+)*`、exclude 放宽到 top-5、删 v3 断言。结果放松 query 6 的 exclude 后，query 7 浮出来--`test_recall_relevance[hybrid]` 单跑过、全套挂。我用 PYTHONHASHSEED=0 一锁，全套 96/96 绿，证是 hash-seed 抖动不是污染。根因：`FakeEmbedder` docstring 自称'确定性 hash 词袋 embedder'，实现却用 `hash(tok)`--Python str 的 hash 按 PYTHONHASHSEED 按进程随机。这个 flaky 一直被 query 6 的失败挡着没暴露，放松 query 6 才让它显形。改 `hashlib.sha256` 后跨进程稳定，全套默认随机 seed 连跑 3 次绿。残留：toy embedder 改了、生产用真 bge-m3 没对齐；3 个测试调整是判断不是硬错。"

**三个抓手**：放松断言反挖根因 / 三层根因穿透 / docstring 与实现契约背离。

---

## 2. 闪光点详解（按主线）

### A. 考古证非回归（不甩锅给版本通用化）

#### A.1 worktree 在 5ae9fd0 实证"生而失败"

**识别过程**：4 个失败里 `test_seed_resolver_regex`、`test_recall_relevance`、`test_a_enriches` 涉及的源码（seed_resolver / chunk_filter / retrieval_service / chunk）在 `f230dd1 route_version通用化` 那轮大改过。第一反应可能是"是不是版本通用化改挂的"。但我没下结论，用 `git worktree add --detach 5ae9fd0` 切到通用化**之前**的提交跑这 4 个测试--同样 4 挂。证是"生而失败"，与 f230dd1 无关（与记忆里"4 个预存测试失败与本次无关"一致）。

**为什么是亮点**：**不把失败甩给最近的改动**。一个测试挂了、相关源码最近改过，很容易直觉归因"是那次改的"。但"相关源码改过"是推断不是证据。worktree 在前一个提交复现同样失败，把"非回归"从推断变成实证。而且用 `worktree` 而不是 `git checkout`--因为工作树有用户未提交的改动（`factorybot/优化与待办清单.md`），`checkout` 会被拒，`worktree add --detach` 不动主工作树、干净。面试官问"你怎么确定不是版本通用化改挂的"，答"worktree 切到通用化前的 5ae9fd0 跑，同样 4 挂，证生而失败。没用 checkout 是因为主工作树有用户未提交改动，worktree 不动它"。

**防守话术**："4 个失败涉及的源码在版本通用化那轮改过，容易归因到那。我没下结论，worktree 切到通用化前的 5ae9fd0 跑，同样 4 挂，证生而失败、非回归。用 worktree 不用 checkout 是因为主工作树有用户未提交改动，worktree 不动它。"

---

### B. 三层根因穿透（跟证据不跟假设）

#### B.1 surface -> middle -> root，每层一个证据

**三层**：
- **surface（表层）**：`test_recall_relevance` 挂在 query 6 的 exclude--`standard-esd` 出现在 top-20。证据：失败堆栈 `assert excl not in doc_ids`。表层归因：exclude 期望过严（ESD 文档词面含"电子制造作业区域"，命中查询"电子制造行业标准"被召回，合理）。
- **middle（中层）**：按用户方案把 query 6 exclude 放宽到 top-5，query 6 过了，但**冒出新失败**--query 7 的 exclude（`standard-ipc-a610` 出现在 top-5），且只在全套跑挂、单跑过。证据：单跑 3/3 过、全套 3/3 挂。中层归因：不是 query 7 期望问题，是结果在变（flaky 或污染）。
- **root（根因）**：`PYTHONHASHSEED=0` 一锁全套 96/96 绿，证是 hash-seed 抖动。追到 `FakeEmbedder._embed_one_sync` 用 `hash(tok)`，str 的 hash 按 PYTHONHASHSEED 按进程随机。证据：`PYTHONHASHSEED=0` 全绿 + docstring 自称"确定性"。

**为什么是亮点**：**每层跟证据不跟假设**。表层失败修了不要收工--修完冒出新失败，说明表层失败挡住了更深的。很多人修完 query 6 看到 query 7 挂会以为"又是一个 exclude 期望问题"，继续放宽 query 7 的 exclude--那就把 flaky 用更宽的断言糊过去了，根因永远没找到。我到中层先停下判断"结果在变"（单跑过全套挂），再锁定 hash-seed，才追到 FakeEmbedder。面试官问"你怎么没在 query 7 也放宽 exclude 了事"，答"query 7 单跑过全套挂，说明结果在变不是期望问题，放宽 exclude 是糊;我先锁 PYTHONHASHSEED 判断是 flaky 还是污染，再追根因"。

**防守话术**："三层：表层 query 6 exclude 过严（ESD 词面命中'电子制造'）；放松后中层 query 7 浮出且单跑过全套挂（结果在变）；锁 PYTHONHASHSEED=0 全绿，根因是 FakeEmbedder 用 hash() 按进程随机。每层一个证据，没在 query 7 继续放宽 exclude 糊过去。"

---

### C. 放松断言反挖根因（核心亮点）

#### C.1 query 6 的失败挡住 query 7，放松 query 6 才让 query 7 浮出

**机制**：`test_recall_relevance` 的查询循环里，query 6（"电子制造行业标准"）的 exclude 断言先挂，pytest 在 query 6 就 fail 了，**循环到不了 query 7**。而 query 7（"防静电接地点的要求"）的 hybrid 检索恰好踩中 FakeEmbedder 的非确定性--某些 hash seed 下 `standard-ipc-a610` 会进 top-5。query 6 不修，query 7 的 flaky 路径永远不执行，flaky 永远不暴露。我把 query 6 的 exclude 放宽到 top-5（ESD 在 top-20 但不在 top-5，合理），query 6 过了，query 7 才被跑到，flaky 显形。

**为什么是亮点**：**放松断言通常是在掩盖问题，这次反挖出更深的**。一般直觉是"断言放宽=降低标准=可能放过 bug"。但这里 query 6 的 exclude top-20 本来就是过严的期望（ESD 词面命中"电子制造"被召回是合理的），放宽到 top-5 是修正期望、不是放水。而修正这个过严期望，移除了挡住 query 7 的路障，让一个潜伏的 flaky 浮出来。**"过严的断言"不只是误报，它还能挡住后面的真问题**--一个测试函数里前一个断言挂了，后面的断言根本不执行。这是"测试断言顺序掩盖缺陷"的典型。面试官问"放松断言不怕放过 bug 吗"，答"query 6 的 exclude top-20 是过严期望不是真 bug--ESD 词面含'电子制造'被召回合理，top hit 是 IPC-A610 已被另一断言守住。放宽到 top-5 是修正期望。而且正因放宽了 query 6，挡住的 query 7 浮出来，挖出 FakeEmbedder flaky--放松反挖根因"。

**防守话术**："query 6 的 exclude top-20 过严（ESD 含'电子制造'被召回合理），放宽到 top-5 是修正期望不是放水--top hit 是 IPC-A610 已被守住。而 query 6 一挂循环就停，query 7 的 flaky 路径从不执行。放宽 query 6 移走路障，query 7 浮出，挖出 FakeEmbedder flaky。过严断言不只误报，还挡后面的真问题。"

---

### D. docstring 与实现契约背离（读契约不只读代码）

#### D.1 "确定性 hash 词袋 embedder" 用了随机的 hash()

**识别过程**：追到 `FakeEmbedder._embed_one_sync` 用 `hash(tok) % self.DIM`。正要改 hashlib 时，回头看类 docstring 第一行：`"""确定性 hash 词袋 embedder（满足 EmbeddingPort）。"""`--**作者明确写了"确定性"**。但 Python 对 str 的 `hash()` 默认按 `PYTHONHASHSEED` 按进程随机化，跨进程不一致。**docstring 宣称确定性，实现却是随机**--契约与实现背离。改 `hashlib.sha256` 不是"加个特性"，是"让实现追上早就写好的契约"。

**为什么是亮点**：**读 docstring 不只读代码**。如果只看 `hash(tok)` 这行，可能觉得"能跑就行，干嘛改"。但 docstring 是作者写下的**意图契约**--"确定性"是他要的属性。实现没满足契约，是个 latent bug，只是被 query 6 挡着没暴露。识别出"docstring 说确定性、实现用随机 hash"的背离，比"hash 改 hashlib"更有价值--前者是契约审查，后者是机械替换。而且这个背离能解释**为什么 flaky 没被早发现**：大家都信了 docstring 的"确定性"，没人去验证 `hash()` 真的确定。面试官问"你怎么想到查 docstring"，答"改 hash 前回头看类注释，发现写着'确定性'--那就不是'加确定性'，是'实现没满足已声明的契约'，性质不同"。

**防守话术**："`FakeEmbedder` docstring 第一行写'确定性 hash 词袋 embedder'，但 `_embed_one_sync` 用 `hash(tok)`--Python str hash 按 PYTHONHASHSEED 按进程随机。契约说确定性、实现是随机。改 hashlib 不是加特性，是让实现追上早写好的契约。大家都信了 docstring 的'确定性'，没人验证 hash() 真的确定，所以 flaky 没被早发现。"

---

### E. 单跑过 / 全套挂：污染 vs flaky 的诊断（不从小样本下结论）

#### E.1 3/3 vs 3/3 看着像污染，PYTHONHASHSEED=0 证是 hash-seed 抖动

**诊断过程**：query 7 浮出后，`test_recall_relevance[hybrid]` 单跑 3 次都过、全套跑 3 次都挂。3/3 vs 3/3 是**系统性**的，第一反应是**测试污染**--某个先跑的测试改了共享状态（jieba 词典、全局变量）影响 hybrid 检索。但"全套挂"也可能是 hash-seed 抖动（全套和单跑是不同进程、不同随机 seed），3/3 vs 3/3 是小样本巧合。两种解释都符合现象，怎么区分？`PYTHONHASHSEED=0` 锁定 seed 跑全套--如果还挂，是污染（锁定 seed 救不了）；如果绿了，是 hash-seed 抖动（锁定到好 seed 就过）。结果 `PYTHONHASHSEED=0` 全套 96/96 绿，证是 hash-seed 抖动，3/3 vs 3/3 是巧合。

**为什么是亮点**：**不从小样本下结论，用控制变量证伪**。3/3 vs 3/3 看着像铁证污染，但小样本的"系统性"可能是巧合。直接归因污染会去 hunt 一个不存在的污染源，浪费且找不到。`PYTHONHASHSEED=0` 是控制变量--锁定 hash seed 这个变量，看现象是否消失。消失了 -> 是 hash seed；没消失 -> 是污染。一个实验区分两种假设。面试官问"单跑过全套挂不是污染吗"，答"看着像，但 3/3 vs 3/3 是小样本可能是巧合。PYTHONHASHSEED=0 锁 seed 跑全套--还挂才是污染，绿了就是 hash-seed 抖动。结果绿了，证是抖动不是污染。不从小样本下结论"。

**防守话术**："单跑 3/3 过、全套 3/3 挂，看着像污染。但小样本的'系统性'可能是巧合--全套和单跑是不同进程不同 hash seed。PYTHONHASHSEED=0 锁 seed 跑全套：还挂是污染、绿了是抖动。结果 96/96 绿，证是 hash-seed 抖动。控制变量区分假设，不从小样本下结论。"

---

### F. 4 个失败各自定性（诚实区分，不混为一谈）

#### F.1 1 真 prod bug + 1 测试基建 bug + 2 测试期望/断言

**定性**：
1. `test_seed_resolver_regex`：**真生产 bug**。`SN_PATTERN`/`WO_PATTERN` 的 `[A-Z0-9]+` 遇内部 `-` 截断，`SN-2024-001` -> `SN-2024`。A 路线 seed 解析错，任何多段 SN/WO 都中招。补 `(?:[-_][A-Z0-9]+)*`。`BATCH_PATTERN` 同病同修（测试数据 `B7777` 无分隔符没暴露，但有潜在同病）。
2. `test_recall_relevance[bm25/hybrid]`：**测试期望过严**（非 prod bug）。GENERAL 宽查询"电子制造行业标准"词面命中含"电子制造作业区域"的 ESD，被召回 top-20 合理；exclude 检查从 top-20 放宽到 top-5（ESD 在 top-20 但 bm25 pos 18 / hybrid pos 8，都不在 top-5；top hit 是 IPC-A610 已守住）。
3. `test_a_enriches_suggested_action_from_b`：**脆性断言**（非 prod bug）。`quoted_text = h.text[:120]` 截断，v3 标记在步骤3"按 v3 温度曲线执行"、落 120 字外；enrichment 用 citations[0]（步骤块）不是 citations[1]（标题块"route v3"）。删 `"v3" in action` 断言--v3-vs-v4 已被 `"route v4" not in` + `250 not in`（v4 峰值温度）覆盖。
4. FakeEmbedder flaky：**测试基建 bug**。`hash()` -> `hashlib.sha256`。

**为什么是亮点**：**不把 4 个混为一谈说成"修了 4 个 bug"**。只有 #1 是真生产 bug，#4 是测试基建 bug，#2/#3 是测试期望/断言调整。区分清楚反而显功力：测试失败不等于 prod bug，可能是期望写错了、断言太脆、或测试基建本身有问题。把测试调整说成修 prod bug 是虚报。面试官问"你修了几个 bug"，答"1 个真生产 bug（SN 正则）+ 1 个测试基建 bug（FakeEmbedder flaky），另 2 个是测试期望过严和脆性断言的调整，不是 prod bug。不混为一谈"。

**防守话术**："4 个不同性质：SN 正则是真生产 bug（多段 SN 截断）；FakeEmbedder 是测试基建 bug（hash 随机）；exclude top-20 是测试期望过严（ESD 词面命中合理）；v3 断言是脆性（120 字截断）。只 1 个真 prod bug，不把测试调整说成修 prod bug。"

---

### G. 范围纪律：修法选择保住语义，不改 prod 行为

#### G.1 exclude 用 top-5 不是删；v3 断言删而不是加长 quoted_text

**选择**：
- **#2 exclude**：选"放宽到 top-5"而非"删 exclude"或"改查询"。top-5 是有意义的排名区（rerank 后的 top_n），exclude 检查"不进 top-5"= "不是首选答案"，比"不在 top-20 候选池"合理；又比"删 exclude"保留了精度语义。改查询（如改成"焊点质量验收标准"）也能让 ESD 不匹配，但那是改测试意图，没保住"GENERAL 宽查询"的场景。
- **#3 v3 断言**：选"删断言"而非"加长 quoted_text 到 200"。加长 quoted_text 是改 prod 行为（citation 变长），为迁就一个脆性断言改 prod 不值。删断言--v3-vs-v4 正确性已被 v4-absent 断言守住，`"v3" in action` 是冗余且受截断影响的。

**为什么是亮点**：**修法跟问题边界对齐，不为迁就测试改 prod**。exclude 放宽到 top-5 保住精度语义；v3 断言删掉而非改 prod 的 quoted_text 长度。每处选最小代价、保住原意图的修法。面试官问"v3 断言为什么不加长 quoted_text 让它过"，答"quoted_text[:120] 是 prod 刻意的截断（citation 简洁），为迁就脆性断言改 prod 不值。v3-vs-v4 已被 v4-absent 守住，删断言即可"。

**防守话术**："exclude 放宽到 top-5 保住精度语义（不进有意义排名区），不删 exclude 也不改查询；v3 断言删掉不加大 quoted_text--截断是 prod 刻意的，为脆性断言改 prod 不值，v3-vs-v4 已被 v4-absent 守住。修法跟问题边界对齐。"

---

## 3. 核心应答话术（高频问题，口语化背熟）

### 话术 1：你这次最核心的发现是什么

"最核心是**放松断言反挖出 flaky 根因**。4 个预存失败里 query 6 的 exclude 先挂，循环到不了 query 7。我修 query 6（exclude 放宽到 top-5）后，query 7 浮出来--单跑过全套挂。锁 PYTHONHASHSEED=0 全绿，根因是 FakeEmbedder 用 `hash()` 按进程随机，docstring 却写着'确定性'。query 6 的过严断言挡住了 query 7 的 flaky 路径，修 query 6 才让它显形。**放松断言通常掩盖问题，这次反挖出更深的**。"

### 话术 2：4 个失败都是 bug 吗

"不是，4 个不同性质。1 个真生产 bug--SN/WO 正则遇内部 `-` 截断（SN-2024-001->SN-2024）；1 个测试基建 bug--FakeEmbedder 用 hash() flaky；2 个测试调整--exclude top-20 过严、v3 断言受 120 字截断影响太脆。只 1 个真 prod bug，不把测试调整说成修 prod bug。"

### 话术 3：你怎么确定不是版本通用化改挂的

"worktree 切到通用化前的 5ae9fd0 跑这 4 个，同样 4 挂，证生而失败、非回归。没用 checkout 是因为主工作树有用户未提交改动，worktree 不动它。'相关源码最近改过'是推断，worktree 复现是实证。"

### 话术 4：单跑过全套挂不是测试污染吗

"看着像，3/3 vs 3/3 嘛。但小样本的系统性可能是巧合--全套和单跑是不同进程不同 hash seed。PYTHONHASHSEED=0 锁 seed 跑全套：还挂是污染、绿了是抖动。结果 96/96 绿，证是 hash-seed 抖动。控制变量区分假设，不从小样本下结论。"

### 话术 5：FakeEmbedder 用 hash() 这个坑你怎么发现的

"修 query 6 后 query 7 浮出，单跑过全套挂，锁 PYTHONHASHSEED=0 全绿，知道是 hash-seed 抖动。追到 FakeEmbedder 用 `hash(tok)`，str 的 hash 按 PYTHONHASHSEED 按进程随机。改 hashlib 前回头看 docstring，第一行写着'确定性 hash 词袋 embedder'--契约早声明了确定性，实现没满足。改 hashlib 是让实现追上契约，不是加特性。"

### 话术 6：放松 exclude 不是放过 bug 吗

"query 6 的 exclude top-20 是过严期望不是真 bug--ESD 文档词面含'电子制造作业区域'，命中查询'电子制造行业标准'被召回合理；top hit 是 IPC-A610 已被另一断言守住。放宽到 top-5 是修正期望（ESD 不进有意义排名区），不是放水。而且正因放宽了 query 6，挡住的 query 7 浮出来挖出 flaky。"

### 话术 7：toy embedder 改了，生产呢

"诚实讲：FakeEmbedder 是测试用 toy（hash 词袋），生产用真 bge-m3，没上线对齐。我改的是测试基建的确定性，不是生产 embedder。生产 bge-m3 本身是确定性的（模型推理不依赖 PYTHONHASHSEED），不存在这个 flaky。toy 改 hashlib 让测试跨进程稳定，与生产行为更一致。"

---

## 4. 深度问答（技术深挖）

**Q：为什么 Python 的 hash() 对 str 是随机的？**
A：安全防护。Python 3 默认启用 str hash 随机化（`PYTHONHASHSEED` 随机），防 hash 冲突 DoS 攻击--攻击者构造大量 hash 冲突的 key 让 dict 退化成 O(n)。代价是 str hash 跨进程不一致。对业务逻辑不该依赖 str hash 的稳定性。`FakeEmbedder` 把 `hash(tok)` 当确定性维度映射，踩了这个坑。正确做法是用 `hashlib`（SHA-256 等，进程无关）做确定性映射。

**Q：hashlib.sha256 取 4 字节做维度，会不会碰撞比 hash() 多？**
A：取 4 字节 = 32 位，模 1024 维。SHA-256 的 4 字节截断分布足够均匀，碰撞概率与 `hash() % 1024` 同量级。且 FakeEmbedder 是词袋累加（同 token 多次出现累加词频），偶发碰撞把两个 token 映射到同维不影响整体相似度排序。这是 toy embedder，目的是驱动 RRF 融合、不是精确检索，碰撞容忍度高。

**Q：query 7 为什么某些 seed 下 ipc-a610 进 top-5？**
A：query 7"防静电接地点的要求"应召回 ESD（含"防静电""接地"），不该召回 IPC-A610（焊点验收标准）。BM25 是词面匹配，IPC-A610 不含这些词，BM25 不会召回它。但 hybrid = BM25 + 稠密（FakeEmbedder cosine），稠密路用 hash 词袋 embedding，某些 hash seed 下 IPC-A610 的 embedding 与 query 的 cosine 相似度偏高，经 RRF 融合挤进 top-5。换 seed 就换 embedding，IPC-A610 可能掉出 top-5。这就是 flaky 的具体机制。

**Q：为什么不直接删 exclude 检查？**
A：exclude 有意义--验证"不相关文档不被首选"。删了就失去精度验证。放宽到 top-5 保住语义：不相关文档可以进候选池（top-20），但不该进有意义排名区（top-5，rerank 后给用户的）。这是"召回"与"排序"的分层--召回宽松、排序严格。exclude 作用于排序层（top-5）比作用于召回层（top-20）合理。

**Q：v3 断言删了，怎么还能验证 A 锁 v3 -> B 拉 v3 SOP？**
A：靠另两个断言：`"route v4" not in action`（v4 文档标题标记不出现）+ `"250" not in action`（v4 峰值温度 250℃ 不出现）。如果 B 误拉 v4 SOP，这两个标记必出现之一。加上测试显式传 v3 ROUTE 锚点（seed SN-2024-001 锁 v3），B 的版本过滤硬保证只回 v3 chunk。所以 v3-vs-v4 正确性被守住，`"v3" in action` 是冗余的（且受 120 字截断影响）。

**Q：考古用 worktree 不怕残留吗？**
A：`git worktree add --detach` 创建独立工作树，跑完 `git worktree remove --force` 删掉，不污染主工作树。比 `git stash + checkout` 安全--stash 要暂存主工作树改动（包括用户未提交的），checkout 会拒绝有未提交改动；worktree 不动主工作树。考古完 worktree 一删，主工作树原样。

**Q：4 个失败一直没人修，为什么？**
A：因为它们被记成"预存失败、与本次无关"就搁置了。每次跑测试看到 4 红，知道是预存的、不是自己引入的，就跳过。这是"预存失败"的陷阱--一旦标记为已知噪音，就不再有人查根因。我这次是用户问"什么原因、能修吗"才认真查，一查发现 1 个真 prod bug + 1 个 flaky 一直藏着。所以"预存失败"不该长期容忍，要么修要么标 xfail 注明原因，不能让红的测试长期裸奔。

---

## 5. 压力追问（陷阱题，考诚实）

**Q：你说放松断言挖出 flaky，但你要是没放松、直接删 exclude，不就什么都发现不了？这不算你的功劳吧？**
A：确实，flaky 是修 #1/2 时撞上的，不是预先规划"我去找 flaky"。我不揽这个功--诚实讲是撞上的。但撞上之后**没糊过去**是关键：query 7 浮出时，最省事的是继续放宽 query 7 的 exclude 让它绿，那样 flaky 永远藏着。我没那么干，停下来判断"单跑过全套挂=结果在变"，锁 PYTHONHASHSEED 追到 FakeEmbedder。**功劳不在撞上，在撞上后没糊、追到根因**。

**Q：3/4 是测试调整，你这轮价值在哪？就改了 1 个正则？**
A：价值三层。一是**正则 bug 是真生产问题**--多段 SN/WO 截断影响 A 路线 seed 解析，不修任何 SN-2024-001 都解析错。二是**FakeEmbedder flaky 是潜伏炸弹**--不修它会成偶发 CI 失败，将来某次 seed 不利就挂，排查极耗时间。三是**4 个失败的定性本身**--区分出"真 bug / 期望过严 / 脆性断言 / 基建 flaky"，把被当噪音搁置的 4 个红测清理成 0 红，测试套件重新可信。价值不在行数，在让 96 个测试从"4 红 + 1 潜伏 flaky"变"0 红 + 稳定"。

**Q：FakeEmbedder 是 toy，改它有意义吗？生产用 bge-m3 又不受影响。**
A：有意义。toy embedder 是所有 hybrid 测试的基建，它 flaky = 所有 hybrid 测试都潜在 flaky（test_recall_relevance[hybrid]、test_end_to_end_doc_answer、test_cross_route_b...）。不修，hybrid 测试套件不可信--今天绿明天红，最终大家会忽略 hybrid 测试的失败（"又是那个 flaky"）。修了让 toy 行为与生产一致（都确定性），hybrid 测试可信。生产 bge-m3 本身确定性，不受影响，但 toy 对齐生产是测试基建该有的纪律。

**Q：你用 worktree 考古，是不是小题大做？直接 stash 跑不行吗？**
A：stash 跑不了--主工作树有用户未提交的 `factorybot/优化与待办清单.md`，`git stash` 要么连它一起 stash（动用户的东西）、要么 checkout 时被拒。worktree 不动主工作树，干净。而且 worktree 能切到任意提交跑完整源码，stash 只能 stash 当前改动、不能换提交。考古要换提交，worktree 是对的工具，不是小题大做。

**Q：4 个失败里 3 个是测试期望/断言问题，是不是测试本来写错了？**
A：部分是。exclude top-20 是期望过严（GENERAL 宽查询召回相关文档合理）；v3 断言是脆性（没考虑 120 字截断）。这两个是测试设计时对检索行为/截断的预期不切实际。但"测试写错了"不等于"不用修"--红的测试要么修期望、要么改代码、要么标 xfail，不能挂着。我把期望修正到切合实际行为，测试重新有意义。

---

## 6. 指标卡片（背下来）

### 卡片 A：改动规模（最硬，直接给）

| 维度 | 数值 | 出处 |
|------|------|------|
| 改动文件 | 4（1 源码 + 3 测试） | git diff --stat |
| 净改动 | +14/-7 | git diff --stat |
| 修复位置 | seed_resolver.py（正则） / _mock_rag_infra.py（hashlib） / test_mock_data_rag.py（exclude top-5） / test_mock_data_trace.py（删 v3 断言） | 各文件 |
| 测试 | 96 收集 / 96 过 / 0 失败（修前 92 过 4 失败 + 潜伏 flaky） | pytest 实跑 |
| flaky 稳定性 | 默认随机 seed 连跑 3 次全绿（修前全套 3/3 挂） | 本次 |
| 真 prod bug | 1（SN/WO/BATCH 正则截断） | [seed_resolver.py](../FactoryRAG/app/routes/traceability/application/seed_resolver.py) |
| 测试基建 bug | 1（FakeEmbedder hash() flaky） | [_mock_rag_infra.py](../FactoryRAG/tests/_mock_rag_infra.py) |
| 考古验证 | worktree 5ae9fd0 证非回归 | 本次 |
| flaky 诊断 | PYTHONHASHSEED=0 锁定证 hash-seed 抖动 | 本次 |

### 卡片 B：缺陷与修复（定性 + 处理）

| 缺陷 | 性质 | 后果 | 修复 |
|------|------|------|------|
| SN/WO/BATCH 正则 `[A-Z0-9]+` 遇内部 `-` 截断 | 真 prod bug | SN-2024-001->SN-2024，A 路线 seed 解析错 | 加 `(?:[-_][A-Z0-9]+)*` |
| exclude_doc_ids 检查 top-20 | 测试期望过严 | ESD 词面命中"电子制造"被召回，exclude 误报 | 放宽到 top-5 |
| `"v3" in action` 断言 | 脆性断言 | quoted_text[:120] 截断使 v3 标记落外 | 删断言（v3-vs-v4 由 v4-absent 覆盖） |
| FakeEmbedder 用 `hash(tok)` | 测试基建 bug | PYTHONHASHSEED 按进程随机，hybrid 召回 flaky | 改 `hashlib.sha256` |

### 卡片 C：能力表述（讲做了什么）

| 能力 | 表述 |
|------|------|
| 测试债务清理 | 区分"真 bug / 期望过严 / 脆性断言 / 基建 flaky"，4 个不同性质不混为一谈 |
| 根因穿透 | 三层（surface query 6 -> middle query 7 -> root FakeEmbedder），跟证据不跟假设 |
| 缺陷发现 | 放松断言反挖潜伏 flaky（被前一个失败挡住的路径） |
| 契约审查 | 读 docstring 不只读代码，识破"确定性"宣称与 hash() 实现背离 |
| 诊断方法 | PYTHONHASHSEED=0 控制变量区分污染 vs flaky；worktree 考古证非回归 |
| 诚实口径 | flaky 是撞上的不揽功、3/4 是测试调整非 prod bug、残留主动交代 |

---

## 7. 红线与遗留（面试别翻车）

**红线**：
- ❌ "修了 4 个生产 bug" -- 只 1 个真 prod bug（正则）+ 1 个测试基建，另 2 个是测试调整。
- ❌ "flaky 是我主动发现的" -- 是修 #1/2 放松 query 6 撞上的，诚实讲。
- ❌ "生产 embedder 已修" -- 改的是 toy FakeEmbedder，生产用 bge-m3 没动。
- ❌ "全套稳定=生产稳定" -- 96 个测试绿，但没上线、toy embedder 与生产 bge-m3 未对齐。
- ❌ "3/3 vs 3/3 铁证污染" -- 小样本巧合，PYTHONHASHSEED=0 证是 hash-seed 抖动。
- ✅ 正确讲法："FactoryRAG 4 个预存测试失败：考古 worktree 在 5ae9fd0 证生而失败非回归。定性 1 真 prod bug（SN 正则截断）+ 1 测试基建 bug（FakeEmbedder hash() flaky）+ 2 测试调整（exclude 过严 / v3 断言脆性）。修 #1/2 放松 exclude 反挖出潜伏 flaky--query 6 失败挡住 query 7，放松后才浮出；PYTHONHASHSEED=0 证 hash-seed 抖动，根因 FakeEmbedder docstring 自称确定性却用随机 hash()，改 hashlib。全套 96/96 默认随机 seed 连跑 3 次绿。残留：toy embedder 改了、生产 bge-m3 未对齐。"

**遗留项清单（主动交代，反加分）**：

| 遗留 | 现状 | 倾向 |
|------|------|------|
| 生产 embedder 对齐 | toy FakeEmbedder 改 hashlib，生产 bge-m3 没动 | 生产本就确定性，不需改；toy 对齐是测试纪律 |
| 3 个测试调整是判断非硬错 | exclude top-5 / 删 v3 断言是行为预期判断 | 合理但可被质疑"放宽标准" |
| BATCH 正则潜在同病 | 测试数据 B7777 无分隔符未触发 | 已同修（补 `(?:[-_][0-9A-Z]+)*`） |
| "预存失败"长期容忍 | 4 红被当噪音搁置致 flaky 潜伏 | 教训：预存失败要么修要么 xfail，不能裸奔 |

被问"还有什么没做"时，老实列出遗留 + 倾向，体现**知道自己不知道什么**。

---

## 8. 一句话定位（收尾用）

"这次的价值不在修了几个测试，而在**识破'预存失败'的陷阱--4 个被当噪音搁置的红测里藏着 1 个真生产 bug（SN 正则截断）和 1 个潜伏 flaky（FakeEmbedder 用随机 hash() 却自称确定性）；修 query 6 的过严 exclude 反让被它挡住的 query 7 浮出，三层根因穿透追到 FakeEmbedder；用 PYTHONHASHSEED=0 控制变量区分 flaky 与污染、用 worktree 考古证非回归**--做了的讲透、4 个定性不混为一谈（只 1 真 prod bug）、flaky 撞上的不揽功、残留不藏，与 MES 测试纪律同构：预存失败要么修要么 xfail、不能让红的测试长期裸奔、docstring 写下的契约实现必须兑现。
