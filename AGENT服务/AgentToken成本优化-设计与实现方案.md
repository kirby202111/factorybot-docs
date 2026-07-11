# Agent Token 成本优化 -- 设计与实现方案（Python 技术栈，L1/L2/L3 共用）

> 本文是 [AGENT服务引入路线.md](AGENT服务引入路线.md) §4「可观测兜底」的**降本侧展开**，与 [Agent可观测性-设计与实现方案.md](Agent可观测性-设计与实现方案.md) 配对：
> - **可观测性那篇解决"测得清"**：`agent_token_total` 计数、`agent_cost_usd_total` 估算、token/会话 P95 漂移告警（§8.2 / §11.3 / §19）。
> - **本篇解决"降得下"**：在计量之上，把"钱花在哪、怎么省、省了不碰红线不降质量"系统化。
> **与 L3 的关系**：L3 的"编排代码层 + 4 类 agent 能力"（[L3实现方案](L3编排型Agent/L3编排型Agent-实现方案.md) §0）已体现了最大的降本杠杆--**懂什么时候不用 AI**。本篇把这条杠杆系统化，并补齐 L3 没展开的提示词层 / 缓存层 / 模型层 / 观测闭环降本手段。
> **口径纪律**：本篇讲的是**设计规划**，不是说"已落地降本 X%"。所有量化收益标记 🔴 待实测，绝不编业务效果数字。降本的前提是**不碰 MES 红线**（只读、不进过点、版本一致性、confirmation gate、证据不可空）--任何降本手段若牺牲这些，一律不做。

---

## 0. 先界定：降本不是"换便宜模型"

通用做法是把模型换成更便宜的。但本 MES Agent 的成本结构里，**模型单价只是其中一个变量**，而且不是最该先动的。一次 L1 诊断会话的钱花在四块：

1. **重复的系统提示 + 工具定义**：ReAct 每一步都把 system prompt + 全部工具描述重发给模型，N 步就发 N 次。这块占比最大、最浪费、最容易省。
2. **滚雪球式的历史上下文**：第 k 步的输入包含前 k-1 步的全部工具结果。工具返回的 JSON 越大，历史涨得越快。
3. **无效的步数**：模型反复查同一上下文、查了不用、该收口不收口--每多一步就是一次"系统提示+历史"的完整重发。
4. **模型规格错配**：能用小模型的步骤用了大模型；能用代码做的步骤用了模型。

一句话：**降本的优先级是"少调 > 少发 > 调便宜的"，不是反过来**。换便宜模型是最后一步，且必须过评测门禁（§10）才能换--MES 不允许为省钱牺牲根因准确率。

---

## 1. 定位与边界

### 1.1 降本的红线（一开口就要讲）

| 红线 | 说明 | 降本时怎么守 |
|------|------|-------------|
| **只读 / 不旁路写** | L1 只读，L2 草稿不落库，L3 写过 gate | 降本手段不改写路径；缓存只缓存只读结果 |
| **不进过点主事务** | 过点 P99 ≤200ms（[领域总览.md](../领域模型/领域总览.md) §4.1） | 降本不在过点路径上挂任何同步 LLM 调用 |
| **版本一致性** | 查工艺必须带 `route_version`（§5.1） | 缓存 key 必须含 `route_version`，禁命中错版本 |
| **证据不可空** | 每条 hypothesis 至少引用 1 条 trace | 工具结果摘要只喂给模型，**trace 落库仍存全文**，证据链不丢 |
| **质量不退** | 根因准确率 / 草稿采纳率不因降本下降 | 换模型 / 改提示词 / 加截断 必须过 `EvalRunner` 回归（§10） |

### 1.2 与可观测性的关系（测 vs 降）

- 可观测性是**度量基础设施**：它告诉你"花了多少、谁花的、有没有漂移"。
- 本篇是**优化动作**：基于度量结果决定"动哪个杠杆、动了之后省了多少、质量退没退"。
- 两者闭环：**测 -> 归因 -> 降 -> 再测**。没有可观测性，降本是盲降；没有降本动作，可观测性只是账单。

### 1.3 不覆盖

- 不重新设计可观测底座（事实源在 [可观测性方案](Agent可观测性-设计与实现方案.md)）。
- 不替 L1/L2/L3 各自的业务流程做选型（事实源在各自实现方案）。
- 不涉及模型供应商商务议价 / 预留额度--那是采购侧，不是架构侧。

---

## 2. 成本模型：钱花在哪

### 2.1 一次 ReAct 会话的 token 构成

L1 / L3-agent 是 ReAct 多步循环；L2 是固定步骤编排（取证据 -> 检索 -> 一次综合生成）。两者的成本结构不同，必须分开看。

**ReAct 会话（L1 诊断 / L3 的 A/B/C/D 能力）**：

```
总输入 token ≈ Σ_{step=1..N} [ system_prompt + tool_definitions + history_{1..step-1} ]
总输出 token ≈ Σ_{step=1..N} output_step            （tool_calls + 最终报告）
总费用       ≈ Σ (input_step × in_price + output_step × out_price)
```

关键观察：
- `system_prompt + tool_definitions` 这块**每步都重发**。N 步会话里它是 N 次的输入成本--这是最大且最可省的一块。
- `history` 是**累积**的：第 k 步要把前 k-1 步的工具结果全带上。工具结果越大，这块涨得越快，且**呈二次放大**（第 k 步的 history 包含前面所有步的结果）。
- N（步数）是乘数：每多一步，不只是多一次输出，还多一次"系统提示+历史"的完整重发。

**L2 草拟（固定步骤）**：

```
总输入 ≈ system_prompt + 取证结果 + 检索到的历史文档（这块可能很大）+ 草稿 schema
总输出 ≈ 一份草稿（结构化）
```

L2 的成本集中在**单次大输入 + 单次大输出**，没有 ReAct 的滚雪球问题。所以 L2 的降本杠杆是"**喂给模型的历史文档要裁剪**"和"**用结构化输出约束输出长度**"，而不是省步数。

### 2.2 降本杠杆对照表（先看总表，后面逐层展开）

| 层 | 杠杆 | 砍的是哪块 | 现有文档是否已提 | 收益预期 🔴 |
|----|------|-----------|----------------|------------|
| **架构层** | 代码节点不调 LLM | 整次调用归零 | L3 §0 已有，系统化见 §4 | 最大（换线 PASS 时 LLM=0） |
| **架构层** | 入口路由前置分流 | 整次 Agent 调用归零 | L0 收口提了路由，降本视角见 §4 | 大 |
| **模型层** | 分层模型路由 / cascading | 每token单价 | 模型可插拔提了，分级路由见 §5 | 中 |
| **提示词层** | **prompt caching** | 重复的系统提示+工具定义 | **未提，本篇核心** §6.1 | 大（多步 Agent 杀手锏） |
| **提示词层** | 工具描述精简 + 动态工具集裁剪 | tool_definitions | capability 裁剪提了，阶段裁剪见 §6.2 | 中 |
| **循环层** | 工具结果摘要 / 截断 / 字段裁剪 | history 累积 | **未提** §7.1 | 大（多步累积场景） |
| **循环层** | 并行工具调用减少往返 | 步数 N | **未提** §7.2 | 中 |
| **循环层** | 早停 / 收敛检测 + recursion_limit | 步数 N | recursion_limit 已有，早停见 §7.3 | 中 |
| **缓存层** | 工具结果缓存（版本化） | 整次工具调用+其结果进历史 | L1 §9 提了 redis，见 §8.1 | 中 |
| **缓存层** | 语义缓存（最终答案） | 整次会话归零 | **未提，MES 有风险** §8.2 | 中（谨慎） |
| **输出层** | 结构化输出 + max_tokens | 输出 token | Pydantic/Enum 已有，见 §9 | 小 |
| **观测闭环** | 成本归因 + 漂移 + 评测门禁 | 持续优化 | 计量有，优化闭环见 §10 | 持续 |

> 收益预期列全部 🔴 待实测，仅表示杠杆量级方向，不是承诺数字。

---

## 3. 降本杠杆分层模型

按"**少调 > 少发 > 调便宜**"的优先级排五层，下层为上层兜底：

| 层 | 名称 | 核心问题 | 优先级 |
|----|------|---------|--------|
| **L0** | 架构层 | 这一步到底要不要调 LLM？ | 最高--不调 = 0 成本 |
| **L1** | 模型层 | 调的话用哪个规格的模型？ | 高--单价直接打折 |
| **L2** | 提示词层 | 每步重发的固定块能不能省？ | 高--多步循环的稳定节省 |
| **L3** | 推理循环层 | 步数和每步的上下文能不能压？ | 中--按场景收益差异大 |
| **L4** | 缓存层 | 这次的调用能不能整个跳过？ | 中--命中即归零，但有正确性风险 |
| 闭环 | 观测驱动 | 省了之后怎么验证没退步、怎么持续找下一桶？ | 持续 |

下面逐层展开实现细节与代码骨架。所有代码骨架与现有 [L1 §7](L1诊断型Agent/L1诊断型Agent-实现方案.md) / [可观测性 §8.4](Agent可观测性-设计与实现方案.md) 的抽象对齐：`ObservableChatModel`、`ToolDescriptor` / `ToolRegistry`、`ToolNode`、`ObservabilityPort`、`llm_factory`、`prompt_version`、`TenantContext`、`route_version`。

---

## 4. 架构层（L0）：能不调 LLM 就不调

这是优先级最高、MES 场景下最该讲的杠杆。**一次调用都不发生，成本就是零**，比任何 prompt 优化都狠。

### 4.1 代码节点不调 LLM（L3 已落地，系统化重述）

[L3 §0](L3编排型Agent/L3编排型Agent-实现方案.md) 的核心判断："输入是否开放 / 是否需推理生成 / 分支是否难穷举"，三问皆否走代码节点。换线 5 步里编排、结构化比对、barrier、gate、写落库全是代码节点，**换线全程 PASS 时 `l3_llm_invocation_total=0`**。

降本视角的补充：
- 代码节点不只是"防越界"，它同时是**最大的降本手段**--把它单列出来强调，体现你懂"架构降本 > 提示词降本"。
- 可观测验证：`l3_node_total{node_type=CODE}` 占比 + `l3_llm_invocation_total` 是 L3 的成本健康指标（[可观测性 §5.2](Agent可观测性-设计与实现方案.md)）。占比越高、LLM 调用越趋近 0，成本越低。
- **反向约束**：不要为了"降本"把本该 agent 做的开放推理硬塞进代码节点（决策树）。L3 §11 Q&A 已讲清"决策树永远落后于现场"--降本不能以牺牲根因覆盖率为代价。

### 4.2 入口路由前置分流（L0 收口的降本视角）

[引入路线 §2.1](AGENT服务引入路线.md) 的 L0 收口型问答 Agent 是"一个入口路由到 RAG 工具"。降本视角下，这个路由层是**省掉整个 Agent 多步循环**的关键：

```text
用户问题
  ↓
[Router]  便宜分类器（小模型 / 规则 / 向量相似度）
  ├─ 简单事实查询     -> RAG 直答（路线 A/B/C，单次检索 + 单次生成，不进 ReAct）  ← 省掉多步循环
  ├─ 跨上下文诊断     -> L1 诊断 Agent（ReAct 多步）
  └─ 草稿 / 编排      -> L2 / L3
```

- **收益**：大量"查一下某工单状态""这条 SOP 怎么写"的简单问题，本不需要 ReAct 多步推理。Router 把它们直送 RAG 单次回答，省掉"系统提示+工具定义"的 N 次重发。
- **Router 用便宜的**：分类器用小模型（如 Haiku / DeepSeek / 本地小模型）甚至规则 + embedding 相似度，单次成本远低于跑一遍 ReAct。
- **MES 约束**：Router 不做写动作、不进过点；它只是个只读分发器。误路由最坏是"简单问题走了 Agent 多绕几步"或"复杂问题直答不够深"--前者浪费钱，后者降体验，都不碰红线。

### 4.3 离线预处理：能预计算的不放实时路径

[RAG 路线 D 防错即时辅助](../RAG服务/防错即时辅助%20RAG/防错即时辅助%20RAG-实现方案.md) 的思路：过点时刻靠**预计算 + 缓存**命中即推，不调 LLM。同理，L2 的 SOP 草拟可由 `ProcessRouteActivated` 事件**异步触发**（[L1 §7.6](L1诊断型Agent/L1诊断型Agent-实现方案.md) / [L2 §2.1](L2草稿型Agent/L2草稿型Agent-实现方案.md)），不在工艺切换的实时关键路径上--既不进过点主事务，也把 LLM 调用挪到非实时窗口，可错峰、可限流、可重试。

---

## 5. 模型层（L1）：分层模型路由 / Cascading

### 5.1 思路

现有 `llm_factory` 已支持模型可插拔（[L1 §2.1](L1诊断型Agent/L1诊断型Agent-实现方案.md)）。降本视角进一步：**不是全会话用一个模型，而是按"能力 / 阶段"分派不同规格模型**。

| 任务 | 模型规格 | 理由 |
|------|---------|------|
| 入口 Router 分类 | 小 / 便宜 | 二分类/多分类，小模型够用 |
| L3 代码节点旁的 agent A 根因推理 | 中 | 开放但结构化（输出根因假设 + 处置卡） |
| L1 跨上下文 5M1E 诊断 | 中-大 | 需要跨源关联推理 |
| L2 / L3-D SOP / 8D 草拟 | 大 | 开放生成，质量要求高 |
| 工具调用的"下一步查什么"决策 | 中 | ReAct 的 tool selection |
| 结构化报告收口 | 中 | 按 schema 收口，Enum 约束 |

### 5.2 实现：ModelRouter + 评测门禁

模型路由不是拍脑袋配的，是**评测背书**的：某能力只有过 `EvalRunner` 证明小模型质量不退（准确率 / 校准误差达标），才允许从大模型降级到小模型。

```python
# app/infrastructure/ai/model_router.py
from dataclasses import dataclass

@dataclass(frozen=True)
class ModelChoice:
    model_name: str
    prompt_version: str      # 模型与提示词绑定版本，评测回归的基准

class ModelRouter:
    """按 capability / phase 选模型。选型表由评测门禁背书，非硬编码拍脑袋。"""

    def __init__(self, routing_table: dict, eval_gate: EvalGate) -> None:
        # routing_table: {(capability, phase): ModelChoice}
        self._table = routing_table
        self._eval_gate = eval_gate

    def for_capability(self, capability: str, phase: str = "default") -> ModelChoice:
        choice = self._table.get((capability, phase))
        if choice is None:
            choice = self._table[(capability, "default")]
        # 启动断言：该 (capability, model) 组合必须过评测门禁，否则拒绝降级
        if not self._eval_gate.passed(capability, choice.model_name, choice.prompt_version):
            raise ModelDowngradeBlocked(
                f"{capability} 降级到 {choice.model_name} 未过评测门禁，禁止启用"
            )
        return choice
```

```python
# app/infrastructure/obs/eval/gate.py
class EvalGate:
    """评测门禁：换模型/换提示词前查回归结果，质量退步则不放行。"""

    def __init__(self, eval_run_repo: EvalRunRepo, thresholds: dict) -> None:
        self._repo = eval_run_repo
        self._thresholds = thresholds   # {capability: {accuracy_min, ece_max, ...}}

    def passed(self, capability: str, model: str, prompt_version: str) -> bool:
        latest = self._repo.latest(capability, model, prompt_version)
        if latest is None:
            return False  # 没跑过评测 = 不放行
        th = self._thresholds[capability]
        return (latest.accuracy_score >= th["accuracy_min"]
                and latest.ece <= th["ece_max"])
```

- **核心纪律**：降级模型 = 改 `routing_table`，但 `ModelRouter` 启动时对每条记录查 `EvalGate.passed`，没过评测的直接抛异常拒绝启动。**省钱不能绕过质量门禁**--这条是 MES 场景降本与通用降本的本质区别。
- `ModelChoice` 绑定 `prompt_version`：换模型往往要同步调提示词，三者（模型 / 提示词 / 评测）一起版本化回归。

### 5.3 cascading（级联）：先小后大

更激进的策略：先让小模型试，置信度够就直接用，不够再升级到大模型重跑。

```text
[小模型诊断] -> confidence >= 阈值? -> 用小模型结果（省）
                          └─ 否 -> [大模型重跑同一会话] -> 用大模型结果（贵但准）
```

- 适合**置信度标定**（[可观测性 §9.2](Agent可观测性-设计与实现方案.md)）已成熟后做：标定曲线告诉你小模型在哪些置信度区间可信。
- **风险**：重跑 = 双倍调用，若小模型置信度长期不达标，反而更贵。cascading 的收益取决于小模型"够用"的命中率。🔴 命中率阈值待线上标定后定，初期不建议上 cascading，先做静态模型路由（§5.2）。

---

## 6. 提示词层（L2）：把每步重发的固定块省下来

ReAct 每步重发的 `system_prompt + tool_definitions` 是最大且最稳定的浪费。这层是**多步 Agent 降本的杀手锏**，现有文档完全没提。

### 6.1 Prompt Caching（核心杠杆）

#### 6.1.1 原理

Anthropic / 部分国产模型的 prompt caching：把一段稳定前缀（system prompt + 工具定义 + few-shot）标记为可缓存，首次写入按 1.25×（5 分钟 TTL）或 2×（1 小时 TTL）计费，**后续命中按 ~0.1× 计费**。

对 ReAct 循环：system prompt + 工具定义在整会话内**完全不变**，天然适合缓存。N 步会话里，这块从"发 N 次全价"变成"写 1 次 + 读 N-1 次缓存价"，输入成本大幅下降。

#### 6.1.2 实现：在 ObservableChatModel 上加缓存标记

现有 `ObservableChatModel`（[可观测性 §8.4](Agent可观测性-设计与实现方案.md)）是所有 provider 调用的统一包装层。缓存标记加在这里，provider 无关、业务无感：

```python
# app/infrastructure/obs/llm_obs.py（扩展现有 ObservableChatModel）
class ObservableChatModel(BaseChatModel):
    def __init__(self, inner, obs, model_name, prompt_version,
                 cache_control: CacheControl | None = None) -> None:
        self._inner = inner
        self._obs = obs
        self._model = model_name
        self._prompt_version = prompt_version
        self._cache_control = cache_control   # None = 不启用缓存

    async def _agenerate(self, messages, **kw):
        # 1. 注入 cache_control 到稳定的系统前缀块
        if self._cache_control:
            messages = self._cache_control.apply(messages)
        t0 = time.perf_counter()
        resp = await self._inner._agenerate(messages, **kw)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        # 2. 观测：分别记录 cache 命中/未命中的 token，归因降本收益
        usage = resp.usage_metadata
        self._obs.llm_called(
            model=self._model, prompt_version=self._prompt_version,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_read_tokens=getattr(usage, "cache_read_tokens", 0),    # 新增
            cache_write_tokens=getattr(usage, "cache_creation_tokens", 0),  # 新增
            latency_ms=latency_ms, finish_reason=resp.response.finish_reason,
        )
        return resp
```

```python
# app/infrastructure/ai/cache_control.py
class CacheControl:
    """把 system prompt + 工具定义标记为可缓存前缀。provider 适配在此收敛。"""

    def __init__(self, enabled: bool, ttl: str = "5m") -> None:
        self._enabled = enabled
        self._ttl = ttl

    def apply(self, messages: list) -> list:
        if not self._enabled:
            return messages
        # Anthropic 风格：在第一条 system / tools 块上挂 cache_control
        out = list(messages)
        for block in out:
            if block.get("role") in ("system", "tools"):
                block = dict(block)
                block["cache_control"] = {"type": "ephemeral", "ttl": self._ttl}
        return out
```

#### 6.1.3 MES 约束与注意点

- **缓存块的内容必须真的稳定**：system prompt、工具定义、few-shot 示例可以缓存；但**会话相关的动态上下文（用户问题、工具结果）不能进缓存块**，否则命中错数据。`CacheControl.apply` 只标记 `system` / `tools` 块。
- **工具定义变了 = 缓存失效**：工具集裁剪（§6.2）若每步动态变，会破坏缓存命中。**取舍**：要么固定工具集换缓存命中（推荐），要么动态裁剪换省工具描述 token。一般 ReAct 会话内固定工具集 + 缓存命中收益更大。动态裁剪只在工具集极大（如 L3 全 14 上下文）时才考虑。
- **prompt_version 变 = 缓存自然失效**：缓存按内容 hash，改提示词后旧缓存不再命中，无需手动清理。
- **provider 适配**：不是所有 provider 都支持 caching。`CacheControl.enabled=False` 时降级为不缓存，业务无感。观测层记录 `cache_read_tokens` 占比，量化收益（§11）。
- **TTL 选择**：5 分钟 TTL 适合单会话内（一次诊断通常 <5 分钟）；1 小时 TTL 适合跨会话复用同一 system prompt（如同一 capability 的高频调用）。🔴 TTL 策略待按会话时长分布定。

### 6.2 工具描述精简 + 动态工具集裁剪

#### 6.2.1 精简 description

`ToolDescriptor.description`（[L1 §4.2](L1诊断型Agent/L1诊断型Agent-实现方案.md)）是**每次都发给模型**的。冗长描述 = 每步都烧 token。纪律：
- description 只写"这个工具查什么、何时该用、入参约束"，不写实现细节、不写废话。
- 工具入参用 Pydantic `args_schema` 自动生成 JSON Schema，**不要在 description 里重复参数说明**（schema 已经表达了）。

#### 6.2.2 按 capability 裁剪（L3 已有）

[L3 §4.1](L3编排型Agent/L3编排型Agent-实现方案.md) 已按 `capability` 裁剪工具集：`tools_for(capability)` 只返回该能力的工具。RootCauseAgent 看不到 FaultImpactAgent 的工具。这直接减少 `tool_definitions` 体积。

#### 6.2.3 按推理阶段裁剪（进阶，谨慎）

更激进：在同一 capability 内，按推理阶段暴露不同工具子集。例如 L1 诊断第一阶段只暴露"查过点"工具，模型拿到 `routeVersion` 后才暴露"查工艺"工具。

```python
# app/infrastructure/ai/phase_toolset.py
class PhaseToolBinder:
    """按推理阶段动态裁剪工具集。仅当工具集极大时启用，否则破坏缓存命中得不偿失。"""

    def __init__(self, registry: ToolRegistry, phase_rules: dict) -> None:
        # phase_rules: {phase: [tool_name]}，由编排层显式声明阶段切换
        self._registry = registry
        self._rules = phase_rules

    def tools_for_phase(self, capability: str, phase: str, tenant: TenantContext):
        allowed = self._rules.get(phase, None)
        full = self._registry.tools_for(capability, tenant)
        if allowed is None:
            return full   # 未声明阶段 = 不裁剪
        return [t for t in full if t.name in allowed]
```

- **取舍（重要）**：阶段裁剪让工具定义每阶段不同 -> **破坏 prompt caching 命中**（§6.1.3）。只有当工具集大到"工具描述 token > 缓存损失"时才值得。L1（9 个工具）不值得，L3 全 14 上下文可能值得。🔴 是否启用按 token 实测决定，默认关闭。
- **替代方案**：多数情况下，固定工具集 + prompt caching 比动态裁剪更省。先用 §6.1，把 token 量出来再决定要不要 §6.2.3。

### 6.3 few-shot 按需注入

few-shot 示例很贵（占输入 token 大头之一），且会被缓存（若放在 system 块）。纪律：
- 只在模型表现不稳的场景给 few-shot（如 5M1E 分类边界模糊的 case）。
- few-shot 进缓存块（随 system prompt 一起缓存），不要每步重发。
- 示例数量按评测收敛--加到第 N 个示例准确率不再涨就停，不为"感觉更稳"堆示例。

---

## 7. 推理循环层（L3）：压步数、压每步上下文

### 7.1 工具结果摘要 / 截断 / 字段裁剪（多步累积场景的核心）

#### 7.1.1 问题

ReAct 第 k 步的输入要带前 k-1 步的全部工具结果。若工具返回大 JSON（如 `query_pass_records` 返回 100 条过点记录、`query_device_params` 返回长时序），history 二次放大。模型其实只需要其中的关键字段。

#### 7.1.2 实现：ResultCompactor（喂模型摘要，落 trace 全文）

关键设计：**模型看到的是摘要，`tool_call_trace` 落的是全文**--证据链不丢，红线不碰。

```python
# app/infrastructure/ai/result_compactor.py
class ResultCompactor:
    """工具结果回灌前的压缩：字段裁剪 + 列表截断 + 摘要。喂模型的是压缩版，trace 落全文。"""

    def __init__(self, rules: dict) -> None:
        # rules: {tool_name: {keep_fields: [...], max_list: 20, summarize: bool}}
        self._rules = rules

    def compact_for_llm(self, tool_name: str, view: BaseModel) -> dict:
        rule = self._rules.get(tool_name)
        if rule is None:
            return view.model_dump()   # 无规则 = 不压缩
        data = view.model_dump()
        # 1. 字段裁剪：只保留模型推理需要的字段
        if rule.get("keep_fields"):
            data = self._project(data, rule["keep_fields"])
        # 2. 列表截断：超长列表截断 + 标注 "...另有 N 条未展示"
        data = self._truncate_lists(data, rule.get("max_list", 20))
        return data
```

```python
# app/infrastructure/ai/tool_node.py（修订现有 ToolNode：落全文，喂摘要）
class ToolNode:
    def __init__(self, registry, trace_repo, obs: ObservabilityPort,
                 compactor: ResultCompactor) -> None:
        self._registry = registry
        self._trace_repo = trace_repo
        self._obs = obs
        self._compactor = compactor

    async def __call__(self, state: AgentState) -> AgentState:
        for call in state["pending_tool_calls"]:
            # ... 权限校验、执行（同现有实现）...
            view = await tool.handler(**args.model_dump(), tenant=obs_ctx.tenant)
            # 1. trace 落全文（证据链完整，红线）
            await self._trace_repo.save_ok(call["name"], tool.bounded_context,
                                           args, view, latency_ms, obs_ctx)
            # 2. 喂模型的是压缩版（降本）
            compacted = self._compactor.compact_for_llm(tool.name, view)
            results.append(self._ok_result(call, compacted))
        state["tool_results"] = results
        return state
```

#### 7.1.3 MES 约束

- **证据链红线**：`tool_call_trace.output_payload` 必须存**完整 view**，不是压缩版。工程师 UI 回溯证据（[可观测性 §7.3](Agent可观测性-设计与实现方案.md)）要看全文。压缩只作用于"喂模型"这一路。
- **裁剪规则要评测**：裁掉的字段不能是根因推理必需的。`keep_fields` 规则变更 -> 过 `EvalRunner` 验证准确率不退（§10）。
- **截断要标注**：截断列表时在结果里加 `"_truncated": true, "_omitted_count": 80`，让模型知道数据不全，必要时换更窄查询条件重查--避免模型基于"看起来只有 20 条"的错误前提推理。

### 7.2 并行工具调用减少往返

#### 7.2.1 原理

ReAct 每次往返（model -> tool -> model）都要重发整个上下文。若模型一次产出**多个独立 tool call**，LangGraph 的 `ToolNode` 并行执行后一次性回灌，就把"3 次往返"压成"1 次往返"--省掉 2 次"系统提示+历史"的重发。

L1 诊断里"查同批次锡膏 + 查贴片机参数"是独立的，本就可并行（[L1 §5.1](L1诊断型Agent/L1诊断型Agent-实现方案.md) 第 3 步同时查两个）。

#### 7.2.2 实现

- LangGraph 的 `ToolNode` 已原生支持一次执行多个 tool call 并行（`asyncio.gather`）。**只需在 system prompt 里明确鼓励**："对相互独立的查询，一次性发起多个工具调用，不要串行等待。"
- 这是**纯提示词侧 + 框架已支持**的改动，零基础设施成本。
- 观测验证：`agent_tool_call_total` 不变，但 `agent_llm_invocation_total`（往返次数）下降--用这个比值量化并行度收益。

#### 7.2.3 约束

- 只对**独立**的工具调用并行。有依赖的（先查过点拿 routeVersion，再查工艺）必须串行--模型需自行判断依赖，提示词里讲清"有数据依赖时仍串行"。
- 并行调用受 `ToolNode` 的权限过滤与 `route_version` 校验约束不变，红线不因并行而松动。

### 7.3 早停 / 收敛检测

#### 7.3.1 recursion_limit（已有，硬兜底）

`recursion_limit=20`（[L1 §5.1](L1诊断型Agent/L1诊断型Agent-实现方案.md)）是硬上限，从根上限死单会话 token 上限。这是"防失控"的兜底，不是"省着用"的优化。

#### 7.3.2 早停：够了就收口

优化侧：模型已有足够证据时主动收口，不要继续探索。手段：
- **system prompt 明确收口条件**："当你已收集到能形成至少 3 条带证据的 5M1E 假设时，停止调用工具，输出报告。不要为追求穷尽而反复查询。"
- **结构化收口信号**：模型每步可在输出里带 `enough_evidence: bool`，编排层检测到 `True` 则强制收口（不依赖模型自觉）。
- **冗余检测**：连续 2 步查询同一上下文 / 查询结果未被任何假设引用 -> 判定冗余，提前收口或转人工。

```python
# app/infrastructure/ai/early_stop.py
class EarlyStopDetector:
    """检测冗余探索，提前收口，避免烧步数。"""

    def __init__(self, max_redundant: int = 2) -> None:
        self._max_redundant = max_redundant

    def should_stop(self, state: AgentState) -> bool:
        # 连续查同一工具 N 次 -> 疑似打转
        recent = state["tool_results"][-self._max_redundant * 2:]
        if len({r["tool"] for r in recent}) == 1 and len(recent) >= self._max_redundant:
            return True
        # 模型自评证据充分
        if state.get("enough_evidence"):
            return True
        return False
```

- **约束**：早停不能误伤--若证据不足就早停，根因报告质量下降。`EarlyStopDetector` 触发后不是直接出报告，而是**转人工**（`needs_human_review`），与可观测兜底（[§12.1](Agent可观测性-设计与实现方案.md)）一致--宁可让人判，不硬答。

---

## 8. 缓存层（L4）：命中即归零，但 MES 有正确性风险

### 8.1 工具结果缓存（版本化，安全）

#### 8.1.1 思路

[L1 §9 阶段五](L1诊断型Agent/L1诊断型Agent-实现方案.md) 已提"工具结果 redis 缓存去重"。降本视角细化：**只缓存变更慢的数据**，且 **cache key 必须含版本/时间维度**。

| 工具 | 可缓存性 | cache key 维度 | TTL |
|------|---------|---------------|-----|
| `query_process_route(route_id, route_version)` | **高**（工艺版本不可变） | `route_id + route_version + tenant` | 长（版本不可变，可长期） |
| `query_material_batch(batch_no)` | 中（批次台账变更慢） | `batch_no + tenant` | 中（🔴 待定，如 10min） |
| `query_pass_records(serial_no)` | **低**（过点记录持续追加） | `serial_no + tenant + max_ts` | 短或不缓存 |
| `query_device_params(asset_id, time_range)` | 中（历史参数不可变，当前参数变） | `asset_id + time_range + tenant` | 历史窗口可长缓存 |

#### 8.1.2 实现

```python
# app/infrastructure/redis_/tool_cache.py
class ToolResultCache:
    """工具结果缓存。cache key 含 route_version 等版本维度，禁命中错版本。"""

    def __init__(self, redis: Redis, ttl_policy: dict) -> None:
        self._redis = redis
        self._ttl = ttl_policy   # {tool_name: seconds}

    def _key(self, tool_name: str, args: dict, tenant: TenantContext) -> str:
        # args 里 route_version 等版本字段天然进 key，保证版本隔离
        return f"tc:{tenant.tenant_id}:{tool_name}:{hash(stable_json(args))}"

    async def get_or_compute(self, tool_name, args, tenant, compute_fn):
        if tool_name not in self._ttl:
            return await compute_fn()    # 不在缓存策略内 = 不缓存
        key = self._key(tool_name, args, tenant)
        hit = await self._redis.get(key)
        if hit:
            self._obs.cache_hit(tool_name)   # 命中即归零 LLM 侧成本
            return json.loads(hit)
        view = await compute_fn()
        await self._redis.setex(key, self._ttl[tool_name], json.dumps(view.dump()))
        return view
```

#### 8.1.3 MES 约束（版本一致性红线）

- **`route_version` 必须进 key**：工艺查询的缓存 key 含 `route_version`，不同版本不串台。这是 §5.1 版本一致性红线在缓存层的延伸。
- **持续追加的数据慎缓存**：过点记录、不良记录会持续追加，缓存易脏。这类工具要么不缓存，要么用 `max_ts`（缓存时记录的最大时间戳）进 key，查询时若要更新数据则 miss。
- **缓存失效与领域事件**：订阅 `ProcessRouteActivated`（新工艺版本）等事件主动失效相关缓存--但工艺版本不可变，新版本是新 key，无需失效旧 key。物料批次台账变更若发事件，可订阅失效。🔴 失效策略按工具逐个定。

### 8.2 语义缓存（最终答案缓存，谨慎）

#### 8.2.1 思路

相似问题命中缓存，直接返回历史报告，不调模型。通用场景很香，但 **MES 场景下危险**。

#### 8.2.2 MES 风险（必须讲清）

MES 追溯的"正确答案"**会随后续数据变化**：
- 工程师今天问"批次 B-77 焊接不良根因"，Agent 基于当时已有的过点 / 不良记录给出假设 A。
- 明天该批次又追加了 50 条新不良记录、或锡膏供应商补登了批次信息，**正确根因可能变成假设 B**。
- 若语义缓存命中昨天的答案 A，就给了过时结论--而 MES 根因直接影响返工 / 隔离决策，过时结论可能误导处置。

#### 8.2.3 受限使用策略

语义缓存不是不能用，是**只能在对"答案时变性"不敏感的场景用**：

| 场景 | 语义缓存 | 理由 |
|------|---------|------|
| "RR-100 工艺路线 v3 的焊接站参数是多少" | ✅ 可 | 工艺版本不可变，答案稳定 |
| "8D 报告标准模板长什么样" | ✅ 可 | 模板稳定 |
| "批次 B-77 焊接不良根因" | ❌ 禁 | 追溯答案随数据变化 |
| "SN-001 的过点轨迹" | ❌ 禁 | 过点记录持续追加 |

- **实现**：语义缓存的 cache key 除问题 embedding 相似度外，必须叠加**数据时变性标签** + **版本维度**。时变性问题（追溯 / 根因 / 实时状态）一律不进语义缓存。
- **保守默认**：🔴 初期**默认关闭语义缓存**，只开工具结果缓存（§8.1）。等可观测数据证明某些稳定问题高频重复，再按场景灰度开启。
- 这条体现 MES 领域判断力：通用降本教程会推荐语义缓存，但 MES 的追溯时变性让它危险--讲清这个区别，比堆技术得分高。

---

## 9. 输出层：结构化输出 + max_tokens

输出 token 通常占总成本的小头（输入是大头），但这层改动成本最低，顺手做。

### 9.1 结构化输出约束（已有，强化）

[L1 §5.3](L1诊断型Agent/L1诊断型Agent-实现方案.md) 已用 Pydantic + Enum 强约束输出。降本视角补充：
- 结构化输出（`with_structured_output`）让模型不写自由文本铺垫，直接出字段--输出 token 更少。
- Enum 约束（`FiveM1ECategory`）让类别字段只出枚举值，不啰嗦解释。
- `evidence` 字段只存 `trace_id` 列表（短），不存证据全文（长）--全文在 trace 里。

### 9.2 max_tokens 上限

- 每次 LLM 调用设 `max_tokens` 上限，防模型发散长篇。
- 但**收口步的报告生成不能卡太死**--报告被 `finish_reason=length` 截断会丢假设。`max_tokens` 按步类型分档：tool selection 步小（如 512），报告生成步大（如 2048）。🔴 档位待实测。
- 观测：`finish_reason=length` 频繁出现 -> 告警（[可观测性 §8.1](Agent可观测性-设计与实现方案.md) 已有），既是质量信号也是"max_tokens 设太小"的信号。

---

## 10. 观测驱动的降本闭环

降本不是一次性动作，是**测 -> 归因 -> 降 -> 再测**的持续闭环。可观测性是闭环的地基。

### 10.1 成本归因（找烧钱大户）

在 [可观测性 §8.2](Agent可观测性-设计与实现方案.md) 的 `agent_token_total` / `agent_cost_usd_total` 基础上，按更多维度归因：

| 归因维度 | 找什么 | 优化动作 |
|---------|-------|---------|
| `capability`（L3 A/B/C/D） | 哪个能力最烧钱 | 针对性优化该能力的提示词 / 工具集 |
| `scenario`（换线/客诉/...） | 哪个场景步数最多 | 检查该场景能否更多走代码节点 |
| `step_no` 分布 | 哪几步最贵 | 看是否冗余探索（§7.3） |
| `cache_read_tokens / prompt_tokens` | 缓存命中率 | 命中率低 -> 检查缓存块是否被动态工具集破坏（§6.2.3） |
| `model` | 大模型调用占比 | 占比高 -> 评估能否降级（§5，过评测门禁） |
| `tenant` | 哪条产线用得多 | 给管理层看 + 限流（§10.4） |

### 10.2 成本漂移检测（已有，强化解读）

[可观测性 §11.3](Agent可观测性-设计与实现方案.md) 的"token/会话 P95 超基线 1.5× 告警"。降本视角的根因排查清单：
- 提示词变长了？（`prompt_version` 变更未配套精简）
- 模型啰嗦了？（completion token 占比升 -> 加 max_tokens / 强化结构化）
- 反复重试？（`agent_llm_schema_error_total` 升 -> schema 不稳）
- 工具结果变大了？（下游 DTO 字段膨胀 -> 强化 §7.1 裁剪）

### 10.3 评测门禁（降本的质量护栏）

任何降本动作（换模型 / 改提示词 / 加截断 / 改缓存策略）**必须过 `EvalRunner` 回归**（[可观测性 §10](Agent可观测性-设计与实现方案.md)）：

```
降本变更 -> 跑评测集 -> 对比新旧：accuracy 不退 + ECE 不退 + token 下降
  ├─ 通过 -> 上线（token 下降记入降本账）
  └─ 不通过 -> 不上线（质量红线 > 成本）
```

- 评测指标里**新增"工具调用冗余"**（[可观测性 §10.4](Agent可观测性-设计与实现方案.md) 已有：实际/最少必要）--这条直接驱动 §7.3 早停优化。
- **成本-质量权衡曲线**：离线跑不同模型 / 不同截断强度，画"准确率 vs token 成本"曲线，找帕累托最优点。🔴 曲线待评测数据积累后出。

### 10.4 限流（防失控烧钱）

- **每会话**：`recursion_limit`（已有）限步数 = 限单会话 token 上限。
- **每租户**：redis 信号量限并发会话数（[L1 §9 阶段五](L1诊断型Agent/L1诊断型Agent-实现方案.md) 已提）。
- **每日预算**：🔴 可选，按租户设日 token 预算，超限降级（如关闭主动巡检触发、Router 强制走 RAG 直答）。这是"硬省钱"开关，防止异常情况烧爆。

---

## 11. 数据模型与指标扩展

### 11.1 `llm_call_log` 扩展字段

在 [可观测性 §8.1](Agent可观测性-设计与实现方案.md) 的 `llm_call_log` 上加缓存命中等字段，支撑降本归因：

```sql
llm_call_log
  -- 现有字段：call_id / session_id / step_no / model / prompt_version
  --          prompt_token_count / completion_token_count / latency_ms / ...
  + cache_read_token_count    INT     -- 缓存命中读取的 token（降本收益来源）
  + cache_write_token_count   INT     -- 缓存写入的 token
  + compacted_input_tokens    INT     -- 经 ResultCompactor 压缩后的输入 token（对比未压缩）
  + capability                VARCHAR -- L3 能力 / L1='diagnosis' / L2='draft'
  + phase                     VARCHAR -- 推理阶段（支撑 §6.2.3 阶段裁剪分析）
```

### 11.2 新增指标

| 指标 | 类型 | label | 含义 |
|------|------|-------|------|
| `agent_cache_read_token_total` | Counter | model, capability | prompt cache 命中读取 token |
| `agent_cache_write_token_total` | Counter | model, capability | prompt cache 写入 token |
| `agent_cache_hit_ratio` | Gauge | model, capability | 缓存命中率（read / (read+write+miss)） |
| `agent_tool_cache_hit_total` | Counter | tool | 工具结果缓存命中次数 |
| `agent_tool_result_compacted_tokens` | Histogram | tool | 工具结果压缩前后 token 差 |
| `agent_parallel_tool_calls_total` | Counter | capability | 单步并行 tool call 数（量化 §7.2） |
| `agent_early_stop_total` | Counter | capability, reason | 早停触发次数 |
| `agent_cost_saved_usd_total` | Counter | lever | 按 lever（cache/model_router/compaction/...）归因的估算节省 |

> `agent_cost_saved_usd_total` 按 lever 归因：缓存命中省的 = `cache_read_tokens × (in_price - cache_read_price)`；模型路由省的 = `(大模型价 - 小模型价) × tokens`。这让"哪条杠杆省了多少"可量化，不是笼统的"降本了"。

### 11.3 SLI / SLO 补充

| SLI | SLO 目标 | 说明 |
|-----|---------|------|
| prompt cache 命中率 | ≥80%（多步会话） | 低于此说明缓存块被破坏或会话过短 |
| token/会话 P95 | 不超基线 | 漂移告警（已有） |
| 工具调用冗余比 | ≤1.5 | 越接近 1 越好（已有评测指标） |
| 降本变更后准确率 | 不退 | 评测门禁硬约束 |

---

## 12. 实现步骤（分阶段）

### 阶段一：度量打底 + 架构层（先测后降）

1. 扩展 `llm_call_log` 字段（§11.1），`ObservableChatModel` 记 `cache_read/write_tokens` / `capability` / `phase`。
2. 上线 §11.2 指标，按 capability / scenario 归因成本，**先量出钱花在哪**（不量清楚不动手降）。
3. 确认 L3 代码节点占比指标（`l3_node_total{node_type=CODE}`）已观测，验证"换线 PASS 时 LLM=0"。
4. Router 前置分流试点（§4.2）：把简单事实查询从 L1 Agent 分流到 RAG 直答，量 LLM 调用下降。

### 阶段二：提示词层（最大稳定收益）

5. `CacheControl` + `ObservableChatModel` 注入 prompt caching（§6.1），先在 L1 诊断会话试点，量 `agent_cache_hit_ratio` 与输入 token 下降。
6. 工具描述精简（§6.2.1）：逐个 review `ToolDescriptor.description`，删冗余、去与 schema 重复的参数说明。
7. 按需评估阶段裁剪（§6.2.3）：仅在 token 实测显示工具定义占比过高时启用，默认关闭。

### 阶段三：循环层 + 缓存层

8. `ResultCompactor` 接入 `ToolNode`（§7.1）：trace 落全文、模型喂摘要，先对大返回工具（过点记录 / 设备遥测）配规则。
9. system prompt 鼓励并行 tool call（§7.2），量 `agent_llm_invocation_total` 下降。
10. `EarlyStopDetector`（§7.3）：冗余检测 + 模型自评收口，触发转人工不硬答。
11. `ToolResultCache`（§8.1）：先缓存工艺路线（版本不可变，最安全），再灰度批次台账。

### 阶段四：模型层 + 闭环

12. `ModelRouter` + `EvalGate`（§5.2）：选型表 + 评测门禁，先对 Router 分类能力降级小模型（风险最低）。
13. 评测集回归所有降本变更（§10.3），画成本-质量曲线。
14. 语义缓存默认关闭（§8.2），仅对工艺参数查询等稳定场景灰度。
15. 每日预算限流（§10.4，可选），按租户硬省钱开关。

---

## 13. 约束落地检查清单

- [ ] 降本不碰只读 / 不旁路写 / 不进过点 / 版本一致性 / 证据不可空任一红线。
- [ ] 工具结果缓存 cache key 含 `route_version` 等版本维度，禁命中错版本。
- [ ] `ResultCompactor` 喂模型摘要，`tool_call_trace` 落完整 view，证据链不丢。
- [ ] 截断列表标注 `_truncated` / `_omitted_count`，不让模型基于"数据不全"错误推理。
- [ ] prompt caching 只标记 `system` / `tools` 稳定块，动态上下文不进缓存块。
- [ ] 模型降级（`ModelRouter`）每条记录过 `EvalGate`，未过评测的降级启动即拒。
- [ ] 任何降本变更（模型 / 提示词 / 截断 / 缓存）过 `EvalRunner` 回归，准确率 / ECE 不退才上线。
- [ ] 早停触发转人工（`needs_human_review`），不直接出报告硬答。
- [ ] 语义缓存默认关闭；开启仅限时变性不敏感场景，追溯 / 根因 / 实时状态禁用。
- [ ] `llm_call_log` 记 `cache_read/write_tokens` / `capability` / `phase`，降本收益按 lever 可归因。
- [ ] `recursion_limit` / 每租户并发上限 / 日预算（可选）形成"步数 + 并发 + 预算"三闸门。
- [ ] 量化收益标 🔴 待实测，不编"已降本 X%"的业务效果数字。

---

## 14. 面试防守 Q&A

**Q：Agent 这么烧 token，你怎么管成本？**
A：分清"测得清"和"降得下"。测得清靠可观测性那篇--`agent_token_total` 计数、按 capability / scenario 归因、token/会话 P95 漂移告警。降得下是本篇，优先级是"少调 > 少发 > 调便宜的"。最大杠杆是架构层--L3 代码节点不调 LLM，换线全程 PASS 时 LLM 调用为 0；其次是 prompt caching，把 ReAct 每步重发的 system prompt + 工具定义缓存，命中价约 1/10；再是工具结果摘要压缩喂模型、并行 tool call 减少往返、工具结果版本化缓存。换便宜模型是最后一步，且必须过评测门禁--MES 不允许为省钱牺牲根因准确率。

**Q：为什么不直接换便宜模型？**
A：因为模型单价只是成本结构的一个变量。一次 ReAct 诊断的钱主要花在"每步重发系统提示+工具定义"和"滚雪球的历史上下文"上，这两块换便宜模型只打折不归零。而代码节点不调 LLM 是直接归零，prompt caching 把重复块降到 1/10，这两个杠杆比换模型狠得多。而且换模型有质量风险--根因准确率一退，误导返工处置的代价远超省的 token。所以顺序是先砍调用次数、再砍每次发的量、最后才动模型规格，且换模型必须过 `EvalGate`。

**Q：prompt caching 在 MES 场景有什么坑？**
A：两个。一是缓存块必须真稳定--system prompt、工具定义、few-shot 可以缓存，但用户问题和工具结果这些动态上下文不能进缓存块，否则命中错数据。二是工具集动态裁剪会破坏缓存命中--如果每步暴露不同工具子集，缓存块内容变了就 miss。所以多数情况固定工具集 + 缓存命中比动态裁剪更省，只有工具集极大（如 L3 全 14 上下文）且 token 实测显示工具定义占比过高时，才考虑阶段裁剪。另外 prompt_version 变了缓存自然失效，不用手动清。

**Q：工具结果那么大，怎么不撑爆上下文？**
A：用 `ResultCompactor` 在回灌模型前做字段裁剪 + 列表截断 + 摘要。关键是**模型看摘要，trace 落全文**--`tool_call_trace.output_payload` 存完整 view，工程师 UI 回溯证据看的是全文，压缩只作用于喂模型这一路，证据链红线不碰。裁剪规则要评测--裁掉的字段不能是根因推理必需的，`keep_fields` 变了过 `EvalRunner`。截断要在结果里标 `_truncated`，让模型知道数据不全，必要时换更窄查询条件重查，不能基于"看起来只有 20 条"的错误前提推理。

**Q：语义缓存为什么在 MES 要谨慎？**
A：因为 MES 追溯的"正确答案"会随后续数据变化。今天问"批次 B-77 焊接不良根因"，基于当时的过点 / 不良记录给假设 A；明天该批次又追加 50 条不良记录、或供应商补登批次信息，正确根因可能变成 B。语义缓存命中昨天的 A 就给了过时结论，而根因直接影响返工 / 隔离决策，过时结论可能误导处置。所以语义缓存只在对答案时变性不敏感的场景用--工艺参数查询（版本不可变）、8D 模板这种稳定的可以；追溯、根因、实时状态这种时变的禁用。初期默认关闭，只开工具结果版本化缓存，等数据证明某些稳定问题高频重复再灰度。

**Q：降本会不会牺牲质量？怎么保证？**
A：靠评测门禁兜底。任何降本动作--换模型、改提示词、加截断、改缓存策略--都必须过 `EvalRunner` 跑评测集回归，对比新旧版本的根因准确率、置信度校准误差（ECE）、工具调用冗余比。准确率 / ECE 不退才上线，退了就不上，质量红线大于成本。`ModelRouter` 启动时对每条模型分派查 `EvalGate.passed`，没过评测的降级直接抛异常拒绝启动。这不是口头保证"不会降质量"，是评测门禁硬拦着。

**Q：怎么知道哪条降本杠杆真的省了钱？**
A：靠成本归因指标。`llm_call_log` 记 `cache_read_tokens` / `capability` / `phase`，`agent_cost_saved_usd_total` 按 lever 归因--缓存命中省的等于 `cache_read_tokens × (in_price - cache_read_price)`，模型路由省的等于 `(大模型价 - 小模型价) × tokens`。这样"哪条杠杆省了多少"可量化。再配合成本漂移检测（token/会话 P95 超基线 1.5× 告警）和成本-质量权衡曲线，形成"测 -> 归因 -> 降 -> 再测"的闭环。不是笼统说"降本了"，是按杠杆量化。

**Q：L3 换线 Agent 说"全程 PASS 时 LLM 调用为 0"，这算降本吗？**
A：算，而且是最大的降本。L3 的"编排代码层 + 4 类 agent 能力"判断标准是"输入是否开放 / 是否需推理生成 / 分支是否难穷举"，三问皆否走代码节点。换线 5 步里编排、结构化比对、barrier、gate、写落库全是代码节点不调 LLM，agent 只在 mismatch 等异常分支触发。换线顺利时 `l3_llm_invocation_total=0`。这不只是防越界，本身就是最大降本--一次调用都不发生，成本就是零，比任何 prompt 优化都狠。可观测上用 `l3_node_total{node_type=CODE}` 占比验证。但反向也要守--不能为了降本把本该 agent 做的开放推理硬塞决策树，决策树永远落后于现场，那是牺牲根因覆盖率换省钱，不值。

---

## 15. 一句话定位

"Agent token 成本降本的优先级是**少调 > 少发 > 调便宜的**：架构层用代码节点把能不调 LLM 的步骤归零（L3 换线 PASS 时 LLM=0）、入口 Router 把简单查询分流出不进 ReAct；提示词层用 prompt caching 把 ReAct 每步重发的系统提示+工具定义降到约 1/10、按 capability 裁剪工具集；循环层用工具结果摘要压缩喂模型（trace 仍落全文保证据链）、并行 tool call 减往返、早停转人工；缓存层用版本化的工具结果缓存、语义缓存因 MES 追溯时变性默认关闭；模型层分层路由但每条降级过 `EvalGate` 评测门禁；最后用按 lever 归因的成本指标闭环'测 -> 归因 -> 降 -> 再测'--全程不碰只读 / 不进过点 / 版本一致性 / 证据不可空 / 质量不退任一红线，省钱不能绕过质量门禁。"
