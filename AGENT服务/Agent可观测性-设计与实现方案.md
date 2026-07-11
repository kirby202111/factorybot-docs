# Agent 可观测性 —— 设计与实现方案（Python 技术栈，L1/L2/L3 共用底座）

> 本文是 [AGENT服务引入路线.md](AGENT服务引入路线.md) §4「可观测兜底」与 L1/L2/L3 三篇实现方案 §8 的**收敛展开**，输出**可观测性分层模型、链路/指标/日志/评测/漂移的完整设计、数据模型、包结构与代码骨架、部署与约束落地**。
> **定位**：L1 诊断型、L2 草稿型、L3 编排型三个 Agent 共用同一套可观测底座。L1/L2/L3 各自实现方案的 §8 自此收敛为指针，**事实源唯一在本篇**——避免三处简版各说各话。
> **技术栈**：OpenTelemetry Python + `prometheus-client` + 结构化日志（JSON），与三大 MES 服务的 Java/Spring APM 通过 W3C `traceparent` 互通，互不侵入。
> **口径纪律**：可观测本身是**只读旁路**——它只采集、只落只读 trace/指标，绝不进过点主事务（[领域总览.md](../领域模型/领域总览.md) §5.3），绝不旁路任何上下文的写路径。观测数据同样受租户隔离约束。本篇讲的是**设计规划**，不是说"已上线全链路可观测"。

---

## 1. 定位与边界

### 1.1 为什么 Agent 需要独立的可观测性设计

通用 APM（应用性能监控）回答"哪个接口慢、哪个服务挂了"。**Agent 可观测性**要额外回答一个 MES 更在意的问题：

> **这条 AI 给出的根因假设，凭什么？谁查的、查到什么、哪条证据支撑了哪条结论、置信度多少、为何转人工？**

这是**可审计的 AI 决策证据链**——与 MES 的防错理念同构：宁可让人判，所有 AI 推理必须可回溯、可度量、可干预。L1 全程只读、L2 草稿不落库、L3 写动作过 confirmation gate，这些红线要"讲得出也查得清"，靠的就是这套观测。

### 1.2 与通用 APM 的区别

| 维度 | 通用 APM | Agent 可观测性（本篇） |
|------|---------|----------------------|
| 核心问题 | 接口慢不慢、服务挂没挂 | AI 决策对不对、凭什么、要不要转人 |
| 链路单位 | 一次 HTTP/RPC 调用 | 一次会话 = N 步推理 = M 次工具/LLM 调用 |
| 关键产物 | 火焰图、P99 延迟 | **证据链**（假设 -> trace_id -> 工具结果） |
| 质量维度 | 可用性、延迟、错误率 | 上述 + **准确率、置信度校准、漂移、token 成本** |
| 受众 | SRE / 后端 | SRE + 工艺/质量工程师（要看证据）+ AI 负责人（要看评测） |

### 1.3 覆盖范围与不覆盖范围

- **覆盖**：`agent-service`（Python）内部全链路——会话、LangGraph 节点、工具调用、ACL、LLM 调用、置信度、评测、漂移、成本。
- **不覆盖**：下游 Java 服务的内部观测（复用其既有 Spring APM / OTel agent），只通过 `traceparent` 把链路接起来。RAG 服务的观测归 [RAG服务](../RAG服务/) 各路线，本篇只定义 Agent 调 RAG 时的 span 衔接约定。
- **不覆盖**：MES 核心业务表的观测（工单/WIP/过点自身指标），那是 MES 主体的事，Agent 只读它们、不替它们埋点。

### 1.4 与 Java 侧 APM 的关系

- Agent 调 Java 服务的只读 REST 时，OTel 的 `httpx` instrumentation 自动注入 `traceparent` header；Java 侧若已挂 OTel agent（Spring），会**续接**同一 trace，形成跨语言完整链路。
- **不要求 Java 侧为此改造**：只要 Java 服务透传/识别 W3C TraceContext（已是行业默认），链路自然串起。若某 Java 服务尚未接入 OTel，链路在该服务内部断开，但 Agent 侧 span 仍完整——降级可接受，因为 Agent 侧证据链是自洽的。
- 指标/日志各自独立栈（Agent 用 prometheus + Loki/stdout，Java 用其既有栈），只在 trace 层互通。

---

## 2. 设计目标与原则

### 2.1 五可目标

| 目标 | 含义 | 对应章节 |
|------|------|---------|
| **可追溯** | 每条假设/草稿/动作卡可回溯到具体工具调用与下游数据 | §4 链路、§7 证据链 |
| **可度量** | Agent 健康度量化为指标，可告警、可画图 | §5 指标 |
| **可干预** | 低置信/异常/越界自动转人工，兜底动作可观测 | §9 置信度、§12 兜底 |
| **可回归** | 模型/提示词变更前先过评测集，不盲发 | §10 评测 |
| **可成本** | token 与费用可追踪，成本漂移可发现 | §8 LLM 观测、§11 漂移 |

### 2.2 设计原则（OOD/SOLID 落到观测层）

- **SRP**：`Tracing`、`MetricsCollector`、`LlmCallLogger`、`EvalRunner` 各司一职，不混在一个"观测大对象"里。
- **DIP**：业务节点（`ToolNode`、`DraftBuilder`、`supervisor`）依赖观测的抽象接口（`ObservabilityPort`），不直接依赖 OTel/prometheus 具体实现——便于测试时换内存实现。
- **OCP**：新增一个 Agent 层级（如未来 L4）或一个 LLM provider，不改观测核心，只新增 label/指标维度。
- **观测与业务解耦**：观测失败（如 OTel exporter 挂）**不阻断**业务流程——trace 落不下去，会话照样跑完，只是该次无 trace。观测是旁路，不是主路径依赖。

### 2.3 红线

- 观测只采集只读数据，**不写**任何业务表；trace/指标/日志表是独立的观测存储，不与 MES 业务表混用。
- 观测数据携带 `tenant`，落库与查询都按租户隔离——工程师只能看到自己车间/产线的会话证据，A 车间工程师查不到 B 车间的 trace。
- PII / 敏感工艺参数在日志与 LLM prompt 摘要中脱敏（§6.3、§16.2）。
- 观测**不进过点主事务**：过点引擎的 P99 ≤200ms（[领域总览.md](../领域模型/领域总览.md) §4.1）是硬约束，Agent 观测绝不挂在过点路径上——Agent 本身就不进过点主事务，其观测更不会。

---

## 3. 可观测性分层模型

### 3.1 五层模型

把 Agent 可观测性拆成五层，下层为上层供数，各层可独立演进：

| 层 | 名称 | 职责 | 技术 | 受众 |
|----|------|------|------|------|
| **L0** | 基础设施层 | OTel SDK 初始化、context 传播、exporter | OpenTelemetry Python | SRE |
| **L1** | 链路层 | trace/span 串联会话->步骤->工具->ACL->下游 | OTel Tracer | SRE + 工程师 |
| **L2** | 指标层 | 会话/工具/置信度/成本/递归上限等计数与延迟 | prometheus-client | SRE + AI 负责人 |
| **L3** | 业务视图层 | 证据链可读投影（`tool_call_trace`/`draft_trace`/`node_trace`） | MySQL 平铺表 | 工程师（UI 回溯） |
| **L4** | 评测质量层 | 离线评测、置信度标定、漂移检测 | pytest + 评测脚本 + 离线统计 | AI 负责人 |

### 3.2 分层架构图

```text
┌─────────────────────────────────────────────────────────────────────┐
│ agent-service（Python）                                               │
│                                                                       │
│   业务节点                  观测旁路（ObservabilityPort 抽象）          │
│  ┌────────────┐  依赖    ┌──────────────────────────────────────────┐│
│  │ Diagnosis   │────────▶│ L4 评测质量层  EvalRunner / Calibration   ││
│  │ Service     │         │  ──────────────────────────────────────  ││
│  │ DraftBuilder│         │ L3 业务视图层  ToolTraceRepo / 证据链投影  ││
│  │ Supervisor  │         │  ──────────────────────────────────────  ││
│  └─────┬──────┘         │ L2 指标层     MetricsCollector             ││
│        │                │  ──────────────────────────────────────  ││
│   model_node /           │ L1 链路层     Tracing (OTel spans)         ││
│   tool_node              │  ──────────────────────────────────────  ││
│        │                 │ L0 基础设施   OTel SDK / context / exporter││
│        ▼                 └───────────┬────────────────────────────────┘│
│  ┌───────────┐                      │ 采集（异步、不阻断业务）           │
│  │ ACL -> httpx│──────────────────────┼─── traceparent header ───┐     │
│  └───────────┘                      │                          │     │
└─────────────────────────────────────┼──────────────────────────┼─────┘
                                      │                          │
                ┌─────────────────────▼──────┐   ┌───────────────▼──────┐
                │ OTel Collector              │   │ Java MES 服务        │
                │  -> Tempo / Jaeger (trace)   │   │ (续接同一 trace)      │
                │  -> Prometheus (metrics) ◀── │   └──────────────────────┘
                │  -> Loki (logs)              │
                └─────────────────────────────┘
                              │
                ┌─────────────▼──────────────┐
                │ Grafana（trace/指标/日志/证据） │
                └────────────────────────────┘
```

### 3.3 数据流

1. 会话开始：`DiagnosisService` 建 root span，写 `diagnosis_session`，`MetricsCollector.session_started`。
2. 每步推理：`model_node` 调 LLM -> `LlmCallLogger` 落 `llm_call_log` + `agent_llm_*` 指标；产出 tool calls。
3. `tool_node` 执行：权限校验 -> ACL -> 下游 REST（注入 `traceparent`）-> 落 `tool_call_trace` + `agent_tool_*` 指标。
4. 收口：`ReportParser` 解析报告 -> 置信度判定 -> 落 `diagnosis_report`，低置信置 `needs_human_review` + `agent_low_confidence_total`。
5. 离线：`EvalRunner` 跑评测集 -> 落 `eval_run`，拟合置信度标定曲线，检测漂移。

---

## 4. 链路追踪（Tracing）

### 4.1 trace / span 模型

一次会话产出一棵 span 树，根 span 代表会话，子 span 代表每一步的 LLM 调用与工具调用：

```text
[agent.session] session_id=S-001 level=L1 tenant=WS-A  ← root span
  ├─ [agent.step] step_no=1
  │    ├─ [llm.invoke] model=claude-... prompt_version=p_v7  tokens=...
  │    └─ [tool.invoke] tool=query_pass_records ctx=过点执行
  │         └─ [acl.call] -> [http.request] GET /api/pass-records/{sn}  ← traceparent 透传到 Java
  ├─ [agent.step] step_no=2
  │    ├─ [llm.invoke] ...
  │    └─ [tool.invoke] tool=query_process_route route_version=v3 ctx=工艺管理
  │         └─ [acl.call] -> [http.request] GET /api/process-routes/{id}?version=v3
  └─ [agent.report] confidence=0.72 needs_human_review=false
```

### 4.2 span 属性约定

所有 span 统一注入以下 OTel attributes，便于按维度检索/告警：

| 属性 | 说明 | 示例 |
|------|------|------|
| `agent.session_id` | 会话 ID | `S-001` |
| `agent.level` | Agent 层级 | `L1` / `L2` / `L3` |
| `agent.tenant.workshop` | 车间 | `WS-A` |
| `agent.tenant.line` | 产线 | `L-01` |
| `agent.step_no` | 步骤序号 | `1` |
| `agent.tool.name` | 工具名 | `query_process_route` |
| `agent.tool.bounded_context` | 所属限界上下文 | `工艺管理上下文` |
| `agent.tool.route_version` | 工艺版本（工艺类工具必填） | `v3` |
| `agent.llm.model` | 模型标识 | `claude-sonnet-5` |
| `agent.llm.prompt_version` | 提示词版本 | `p_v7` |
| `agent.confidence` | 报告置信度（仅 report span） | `0.72` |
| `agent.status` | 结果状态 | `OK` / `DENIED` / `ERROR` / `TIMEOUT` |

### 4.3 上下文传播（traceparent 到 Java）

- OTel 的 `httpx` instrumentation 在每次出站 REST 调用自动注入 W3C `traceparent` header。
- Java 侧（生产执行服务、制造资源服务）若挂了 OTel agent，会从 `traceparent` 续接 span，链路在 Tempo/Jaeger 里跨语言连成一条。
- ACL 客户端**不手动**拼 `traceparent`——靠 instrumentation 自动注入，避免人为出错；只在 header 里额外带 `X-Tenant-*` 做权限（[L1 §4.3](L1诊断型Agent/L1诊断型Agent-实现方案.md)）。
- Kafka 侧（主动触发场景）：`aiokafka` 消费时从消息 header 取 `traceparent` 续接，或为自发事件新建 root span。

### 4.4 LangGraph 集成

- `model_node` / `tool_node` 外层包 span：进入节点开 span，退出关 span，异常记 `agent.status=ERROR` 事件。
- LangGraph `checkpointer` 的 `thread_id = session_id`，使中断恢复的会话与原 trace 同属一棵树（恢复时续 root span）。
- `recursion_limit` 命中抛 `GraphRecursionError` 时，在 root span 记 `agent.recursion_limit_hit` 事件并标 `agent.status=TIMEOUT`。

### 4.5 证据链 = span 树的业务投影

trace span 树是给 SRE 看的火焰图；业务侧（工程师 UI）看的是它的**平铺投影**——`tool_call_trace` 表。每个 tool span 落一行 `tool_call_trace`，`report.hypotheses[].evidence` 引用对应 `trace_id`。工程师点假设 -> 弹证据 -> 回溯到具体工具入参出参与下游 REST 调用。两者同源，trace 是底，证据链是面。

### 4.6 代码骨架

```python
# app/infrastructure/obs/tracing.py
from contextlib import contextmanager
from opentelemetry import trace

class Tracing:
    """链路层：封装 OTel span 创建，业务节点只调它，不直接碰 OTel API。"""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    @contextmanager
    def session_span(self, obs_ctx: ObservabilityContext):
        with self._tracer.start_as_current_span(
            "agent.session", attributes=obs_ctx.base_attributes()
        ) as span:
            try:
                yield span
            except (GraphRecursionError, asyncio.TimeoutError) as e:
                span.set_attribute("agent.status", "TIMEOUT")
                span.record_exception(e)
                raise

    @contextmanager
    def tool_span(self, obs_ctx: ObservabilityContext, descriptor: ToolDescriptor):
        with self._tracer.start_as_current_span(
            "tool.invoke",
            attributes={
                "agent.tool.name": descriptor.name,
                "agent.tool.bounded_context": descriptor.bounded_context,
                "agent.step_no": obs_ctx.step_no,
            },
        ) as span:
            yield span  # 调用方在 yield 内执行工具，结果回填属性

    @contextmanager
    def llm_span(self, obs_ctx: ObservabilityContext, model: str, prompt_version: str):
        with self._tracer.start_as_current_span(
            "llm.invoke",
            attributes={
                "agent.llm.model": model,
                "agent.llm.prompt_version": prompt_version,
                "agent.step_no": obs_ctx.step_no,
            },
        ) as span:
            yield span
```

```python
# app/infrastructure/obs/context.py
from dataclasses import dataclass

@dataclass(frozen=True)
class ObservabilityContext:
    """随会话流动的观测上下文，注入到每个节点。不可变。"""
    session_id: str
    trace_id: str
    tenant: "TenantContext"
    level: str            # "L1" / "L2" / "L3"
    prompt_version: str
    step_no: int = 0

    def base_attributes(self) -> dict:
        return {
            "agent.session_id": self.session_id,
            "agent.level": self.level,
            "agent.tenant.workshop": self.tenant.workshop,
            "agent.tenant.line": self.tenant.line,
            "agent.llm.prompt_version": self.prompt_version,
        }
```

---

## 5. 指标体系（Metrics）

### 5.1 命名规范与分层

- 前缀 `agent_` 为三层级通用指标；`l2_` / `l3_` 为该层级特有。
- label 维度固定：`level` / `tool` / `status` / `model` / `bounded_context`，新增维度需评审——维度爆炸会撑爆 prometheus。
- Counter 用 `_total` 后缀；Histogram 用 `_seconds` / `_bytes` 后缀，遵循 [Prometheus 命名规范](https://prometheus.io/docs/practices/naming/)。

### 5.2 指标总表

**通用（L1/L2/L3 共用）**

| 指标 | 类型 | label | 含义 |
|------|------|-------|------|
| `agent_session_total` | Counter | level, status | 会话总数（status: RUNNING/DONE/TIMEOUT/FAILED） |
| `agent_session_latency_seconds` | Histogram | level | 整会话延迟 |
| `agent_tool_call_total` | Counter | tool, status | 工具调用次数（status: OK/DENIED/ERROR） |
| `agent_tool_call_latency_seconds` | Histogram | tool | 单工具调用延迟 |
| `agent_tool_denied_total` | Counter | tool | 权限拒绝次数 |
| `agent_tool_error_total` | Counter | tool, bounded_context | 工具失败次数 |
| `agent_recursion_limit_hit_total` | Counter | level | 触发最大步数上限次数 |
| `agent_session_timeout_total` | Counter | level | 整会话超时次数 |
| `agent_low_confidence_total` | Counter | level | 置信度 <0.5 转人工次数 |
| `agent_llm_invocation_total` | Counter | model, level | LLM 调用次数 |
| `agent_llm_latency_seconds` | Histogram | model | 单次 LLM 调用延迟 |
| `agent_token_total` | Counter | model, direction, level | token 用量（direction: prompt/completion） |
| `agent_cost_usd_total` | Counter | model, level | 估算费用（美元） |
| `agent_llm_schema_error_total` | Counter | model | 模型输出 schema 校验失败次数 |

**L2 草稿型新增**

| 指标 | 类型 | label | 含义 |
|------|------|-------|------|
| `l2_draft_total` | Counter | draft_kind, status | 草稿生成数（返工单/8D/SOP） |
| `l2_draft_latency_seconds` | Histogram | draft_kind | 草拟延迟（取证据 + 检索 + LLM 综合） |
| `l2_draft_low_confidence_total` | Counter | draft_kind | confidence <0.5 草稿数 |
| `l2_draft_adoption_total` | Counter | draft_kind | 草稿被工程师采纳下达数 |
| `l2_draft_rejected_total` | Counter | draft_kind, reason | 工程师驳回数（confirmation gate 拒绝） |
| `l2_acl_error_total` | Counter | client | 只读 ACL 调用失败次数 |
| `l2_active_trigger_total` | Counter | trigger | 主动触发草拟次数（SOP） |

**L3 编排型新增**

| 指标 | 类型 | label | 含义 |
|------|------|-------|------|
| `l3_session_total` | Counter | scenario, status | 会话数（换线/首件/异常处置场景） |
| `l3_node_total` | Counter | capability, node_type | 节点执行数（node_type: CODE/AGENT） |
| `l3_llm_invocation_total` | Counter | capability | 真 LLM 调用数（换线全程 PASS 时为 0） |
| `l3_step_latency_seconds` | Histogram | step | 各步耗时（含 gate 等待） |
| `l3_gate_decision_total` | Counter | step, decision | gate 决策数（decision: APPROVED/REJECTED/SKIPPED） |
| `l3_agent_confidence_total` | Histogram | capability, confidence | agent 置信度分布（high/medium/low） |
| `l3_suspended_total` | Counter | scenario | 故障隔离挂起次数 |
| `l3_write_tool_total` | Counter | tool, confirmed | 写工具调用数（confirmed: true/false） |
| `l3_write_rejected_total` | Counter | tool | 未带有效 confirmation token 被拒次数 |

> `l3_llm_invocation_total` 与 `l3_node_total{node_type=CODE}` 的占比是 L3 的核心健康指标——体现"懂什么时候不用 AI"：换线顺利时 LLM 调用应趋近 0，全靠代码节点跑。

### 5.3 埋点位置

| 指标 | 埋点位置 |
|------|---------|
| session_* | `DiagnosisService.diagnose` / `DraftBuilder.build` / `supervisor.run` 入口与出口 |
| tool_* | `ToolNode.__call__`（[L1 §7.2](L1诊断型Agent/L1诊断型Agent-实现方案.md) 已有雏形） |
| llm_* / token_* / cost_* | `llm_factory` 出品的 `BaseChatModel` 包装层（统一拦截，见 §8.4） |
| l2_draft_* | `DraftBuilder` 出口 + confirmation gate 回调 |
| l3_node_* / gate_* | `supervisor` 节点调度器 + 各 gate |

### 5.4 SLI / SLO 与告警

| SLI | SLO 目标 | 告警条件 | 严重度 |
|-----|---------|---------|--------|
| 会话 P99 延迟 | L1 ≤60s / L3 ≤120s | P99 连续 10min 超阈值 | P2 |
| 工具调用错误率 | <1% | 5min 错误率 >5% | P1 |
| LLM schema 失败率 | <0.5% | 5min 失败率 >2% | P2 |
| 递归上限命中率 | <2% | 10min 命中率 >5% | P2 |
| 低置信转人工占比 | 监控不告警（健康兜底） | **突增**（同比 +50%）告警 | P3 |
| token/会话 P95 | 基线 ±20% | 超基线 1.5× 持续 30min | P3（成本漂移） |
| 写工具被拦 | 0 | 任意一次 >0 | P0（红线越界） |

> 低置信转人工**不告警上限**而是告警**突增**——转人工本身是兜底在工作，是健康的；突增说明模型/数据出问题了。这个区别要在告警规则里讲清，否则会误报。

### 5.5 代码骨架

```python
# app/infrastructure/obs/metrics.py
from prometheus_client import Counter, Histogram, Registry

class MetricsCollector:
    """指标层：所有埋点集中在此，业务节点调它，不直接碰 prometheus 原语。"""

    def __init__(self, registry: Registry) -> None:
        self._session_total = Counter(
            "agent_session_total", "Agent sessions", ["level", "status"], registry=registry
        )
        self._tool_total = Counter(
            "agent_tool_call_total", "Tool calls", ["tool", "status"], registry=registry
        )
        self._tool_latency = Histogram(
            "agent_tool_call_latency_seconds", "Tool latency", ["tool"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0), registry=registry,
        )
        self._low_conf = Counter(
            "agent_low_confidence_total", "Low-confidence handoffs", ["level"], registry=registry
        )
        self._llm_tokens = Counter(
            "agent_token_total", "LLM tokens", ["model", "direction", "level"], registry=registry
        )
        self._write_blocked = Counter(
            "l3_write_rejected_total", "Write tool blocked", ["write_tool"], registry=registry
        )

    def session_finished(self, level: str, status: str) -> None:
        self._session_total.labels(level=level, status=status).inc()

    def tool_ok(self, tool: str, latency_s: float) -> None:
        self._tool_total.labels(tool=tool, status="OK").inc()
        self._tool_latency.labels(tool=tool).observe(latency_s)

    def tool_denied(self, tool: str) -> None:
        self._tool_total.labels(tool=tool, status="DENIED").inc()

    def low_confidence(self, level: str) -> None:
        self._low_conf.labels(level=level).inc()

    def llm_tokens(self, model: str, level: str, prompt: int, completion: int) -> None:
        self._llm_tokens.labels(model=model, direction="prompt", level=level).inc(prompt)
        self._llm_tokens.labels(model=model, direction="completion", level=level).inc(completion)

    def write_blocked(self, write_tool: str) -> None:
        self._write_blocked.labels(write_tool=write_tool).inc()
```

---

## 6. 结构化日志（Logging）

### 6.1 日志规范

- **结构化 JSON**，每行一条，字段固定：`ts` / `level` / `logger` / `msg` / `trace_id` / `session_id` / `tenant` / `level_label`（L1/L2/L3）/ `extra`。
- `trace_id` / `session_id` 从 OTel context 自动取（`LoggingContext` 注入），实现**日志与 trace 关联**——在 Grafana 点 trace 能跳到对应日志，反之亦然。
- 禁止散落 `print`；统一走 `structlog` 或 stdlib `logging` + JSON formatter。

### 6.2 分级与采样

| 级别 | 场景 | 采样 |
|------|------|------|
| ERROR | 工具失败、LLM schema 失败、写工具被拦、兜底转人工 | 全量 |
| WARN | 权限拒绝、单工具超时、重试、低置信 | 全量 |
| INFO | 会话开始/结束、报告产出、草稿生成、gate 决策 | 全量 |
| DEBUG | 工具入参出参摘要、prompt 片段 | 采样 10%（🔴 采样率待定） |

- DEBUG 含敏感数据，生产默认关；排障时按 `session_id` 动态开（按会话采样，非全局）。

### 6.3 脱敏

- 工具入参出参与 prompt 摘要进日志/trace 前，经 `Redactor` 脱敏：
  - 序列号保留前 4 后 2（`SN-0012****89`）🔴 规则待定。
  - 物料批次、供应商信息、工艺参数阈值按白名单字段保留，其余打码。
  - 用户 PII（工程师 ID 之外的个人信息）不采集。
- `Redactor` 是纯函数、可单测、可按租户配置不同脱敏策略（ISP）。

### 6.4 日志与 trace 关联

```python
# app/infrastructure/obs/logging.py
import structlog, logging
from opentelemetry import trace

def _inject_context(_, __, event_dict):
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if ctx and ctx.is_valid:
        event_dict["trace_id"] = f"{ctx.trace_id:032x}"
        event_dict["span_id"] = f"{ctx.span_id:016x}"
    return event_dict

structlog.configure(
    processors=[_inject_context, structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger()
```

---

## 7. 业务可观测视图（证据链回溯）

### 7.1 trace 落库表族

trace 落 MySQL 的平铺视图，给工程师 UI 用（trace 后端 Tempo/Jaeger 给 SRE 用，两套并存）：

| 表 | 来源 | 用途 |
|----|------|------|
| `tool_call_trace` | L1 `ToolNode`（[L1 §5.2](L1诊断型Agent/L1诊断型Agent-实现方案.md)） | 工具调用证据 |
| `draft_trace` | L2 `DraftBuilder` | 草稿生成证据 |
| `node_trace` | L3 `supervisor` | 编排节点执行（含 CODE/agent 区分） |
| `llm_call_log` | 通用 `LlmCallLogger` | LLM 调用明细（token/成本/模型） |
| `diagnosis_session` / `diagnosis_report` | L1 | 会话与报告 |

### 7.2 证据链模型

```python
class Hypothesis(BaseModel):
    category: FiveM1ECategory
    rank: int
    statement: str
    evidence: list[str]          # ["trace_id=T-101", "trace_id=T-103"] ← 引用 tool_call_trace
    suggested_action: str
```

- `evidence` 存的是 `tool_call_trace.trace_id` 列表，UI 点开即拉对应行的入参出参。
- 证据**必须非空**：`ReportParser` 校验每条 hypothesis 至少引用 1 条 trace，否则判 schema 失败重试——没有证据的假设不许输出（与 L1 §5.3 的 Pydantic 强约束一致）。
- L2 草稿的 `evidence_refs` 同样引用 `trace_id` + `subgraph_ref`（[L2 §8.3](L2草稿型Agent/L2草稿型Agent-实现方案.md)），L3 动作卡的 `evidence` 引用 `trace_id` 列表 + `agent_hypothesis`。

### 7.3 工程师 UI 回溯交互

```text
报告卡片：焊接不良根因（置信度 0.72）
  └─ 假设1 [Method] rank=1：焊接站温度模板下限偏低
       └─ 证据：trace_id=T-101 (query_process_route v3)  -> 点开：工艺步骤/参数模板
                 trace_id=T-103 (query_device_params)     -> 点开：贴片机当时温度曲线
  └─ 假设2 [Material] rank=2：锡膏批次 B-77 回温超时
       └─ 证据：trace_id=T-104 (query_material_batch)    -> 点开：批次台账
  └─ disclaimer：辅助假设，最终处置需工程师确认
```

- 每条证据可下钻到下游 REST 调用（通过 trace_id 在 Tempo/Jaeger 查完整 span）。
- 置信度 <0.5 的报告**不展示给操作工**，只推工程师，且标红"需复核"。

### 7.4 L3 节点类型区分

L3 的 `node_trace.node_type` 区分 `CODE`（纯 Python 函数节点，不调 LLM）与 `agent`（调 LLM + 工具）。这让可观测时能统计"本次换线调了几次 LLM"（[L3 §8.1](L3编排型Agent/L3编排型Agent-实现方案.md)）——换线顺利时 LLM 调用应趋近 0，这是 L3 健康度的核心信号。

---

## 8. LLM 特有观测

### 8.1 LLM 调用观测

每次 `model_node` 调 LLM 落一行 `llm_call_log`：

| 字段 | 说明 |
|------|------|
| `call_id` (PK) | 调用 ID |
| `session_id` (FK) | 所属会话 |
| `step_no` | 步骤序号 |
| `model` | 模型标识 |
| `prompt_version` | 提示词版本（§8.3） |
| `prompt_token_count` | 输入 token |
| `completion_token_count` | 输出 token |
| `latency_ms` | 调用延迟 |
| `finish_reason` | stop / length / tool_calls / error |
| `tool_calls_produced` | 产出的 tool call 数（0 表示收口） |
| `occurred_at` | 时间戳 |

- prompt 全文**不落库**（体积大 + 敏感），只落摘要 + `prompt_version`；需要复跑时靠 `prompt_version` + 会话 state 重建。
- `finish_reason=length` 频繁出现说明输出被截断，告警。

### 8.2 token 与成本追踪

- `agent_token_total` 按 prompt/completion 分桶计数，`agent_cost_usd_total` 按模型单价估算。
- 单价表 `model_pricing` 配置化（🔴 单价来源：各 provider 官网实时单价，需定期同步），`CostEstimator` 按 token × 单价算。
- 按租户/车间聚合成本，给管理层看"AI 每月花多少、哪条产线用得多"。

### 8.3 提示词版本追踪

- 每个 system prompt 有 `prompt_version`（语义化版本或内容 hash），随 `llm_call_log` 与 span 落库。
- 提示词变更 -> `prompt_version` 变 -> 评测集回归（§10）-> 对比新旧版本指标。
- 这是"提示词即代码"的观测支撑：不改提示词版本号就不许改提示词，改了必须过评测。

### 8.4 多 provider 观测一致性

- `llm_factory` 出品的模型统一包一层 `ObservableChatModel`，拦截所有 provider 的调用，统一记 `llm_call_log` + 指标——无论 Claude / 通义千问 / DeepSeek / 本地模型，观测口径一致。
- 不同 provider 的 token 计数口径差异（如中文 token）在包装层归一化，避免指标不可比。

```python
# app/infrastructure/obs/llm_obs.py
class ObservableChatModel(BaseChatModel):
    """包装任意 BaseChatModel，统一埋 LLM 观测。provider 无关。"""

    def __init__(self, inner: BaseChatModel, obs: ObservabilityPort,
                 model_name: str, prompt_version: str) -> None:
        self._inner = inner
        self._obs = obs
        self._model = model_name
        self._prompt_version = prompt_version

    async def _agenerate(self, messages, **kw):
        t0 = time.perf_counter()
        try:
            resp = await self._inner._agenerate(messages, **kw)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            self._obs.llm_called(
                model=self._model, prompt_version=self._prompt_version,
                prompt_tokens=resp.usage_metadata.prompt_tokens,
                completion_tokens=resp.usage_metadata.completion_tokens,
                latency_ms=latency_ms, finish_reason=resp.response.finish_reason,
            )
            return resp
        except Exception as e:
            self._obs.llm_failed(self._model, e)
            raise
```

---

## 9. 置信度与转人工

### 9.1 置信度来源

置信度不是模型一句话，是**三源融合**：

| 源 | 计算 | 权重 🔴 |
|----|------|--------|
| 模型自评 | 模型在报告里输出 `confidence`（0~1） | 待标定 |
| 证据充分度 | 实际引用证据数 / 该场景期望证据数 | 待标定 |
| 工具成功率 | 本会话成功工具调用占比 | 待标定 |

- 融合不是简单平均，是标定后的映射（§9.2）。
- 🔴 三源权重与融合函数待评测数据拟合后确定，初始版仅用模型自评 + 阈值。

### 9.2 置信度标定（Calibration）

模型说"0.8 置信度"不代表 80% 准确——需要**标定**：

- 离线用评测集跑，把预测按置信度分桶（0~0.1, 0.1~0.2, ...），算每桶**实际准确率**，画可靠性图（reliability diagram）。
- 若模型自信但常错（overconfident），用标定映射修正：Platt scaling（逻辑回归）或 isotonic regression。
- 标定曲线版本化（`calibration_version`），随模型/提示词变更重标。
- 🔴 标定方法（Platt vs isotonic）待评测数据规模确定后选；小样本用 isotonic 易过拟合，倾向 Platt。

### 9.3 阈值与路由策略

| 层级 | 阈值 | 路由 |
|------|------|------|
| L1 | `confidence < 0.5` | `needs_human_review=True`，不推操作工，推工程师 |
| L1 | `0.5 ≤ confidence < 0.7` | 推工程师，标"参考" |
| L1 | `confidence ≥ 0.7` | 推工程师，标"较高把握" |
| L3 | high/medium/low | low -> `need_human_review`，不自动路由（[L3 §8.3](L3编排型Agent/L3编排型Agent-实现方案.md)） |

- 阈值不是拍脑袋，是标定后按"可接受的误报/漏报率"反推——🔴 初始阈值 0.5 是工程默认，待评测后调。

### 9.4 转人工可观测

- 每次转人工：`agent_low_confidence_total` +1，`diagnosis_report.needs_human_review=True`，落 trace 事件。
- 转人工**占比**与**突增**双监控（§5.4）：占比是健康兜底信号，突增是异常信号。
- 转人工的报告进工程师队列，工程师处置结果（采纳/驳回/修改）回写，作为评测的正负样本（§10.1 闭环）。

---

## 10. 评测与回归（Eval）
> 评测体系完整设计（金标准集 / 指标体系 / LLM-as-judge / 置信度标定 / 漂移 / CI 门禁）的事实源见 [RAG与Agent评测-设计与实现方案.md](../RAG与Agent评测/RAG与Agent评测-设计与实现方案.md)；本节保留链路侧简要视角，不重复展开。

### 10.1 评测集构建

- `eval_case` 表：`case_id` / `scenario`（不良场景描述）/ `question` / `expected_hypotheses`（预期根因类别与排序）/ `expected_evidence_types`（应查哪些上下文）/ `level`。
- 评测集来源：① 历史真实不良案例（脱敏）；② 转人工后被工程师确认/驳回的报告（§9.4 闭环，最有价值）；③ 人工构造的边界场景。
- 🔴 评测集规模与覆盖目标（如 200 case 覆盖 5M1E 各维度）待定，分批积累。

### 10.2 离线评测流程

- `EvalRunner` 跑全集：对每个 case 跑 Agent，比对实际报告与预期，算指标。
- 触发时机：模型升级、提示词 `prompt_version` 变更、工具集调整、定期回归。
- 评测脚本走 pytest，可进 CI 作为发版门禁（🔴 是否硬门禁待定）。

### 10.3 在线影子评测

- 生产问题按比例（🔴 比例待定，如 5%）影子复制给新模型/新提示词跑，**不影响线上**，只对比新旧报告差异。
- 用于发现线上分布下的退化，补离线评测集覆盖不足。

### 10.4 评测指标

| 指标 | 计算 | 目标 🔴 |
|------|------|--------|
| 根因准确率 | top-1 假设命中预期类别 / 总 case | 待定 |
| 证据召回 | 实际查对的上下文数 / 期望上下文数 | 待定 |
| 草稿采纳率（L2） | 被工程师采纳下达的草稿 / 总草稿 | 待定 |
| 置信度校准误差（ECE） | 预测置信度与实际准确率的加权差 | ECE < 0.1 |
| 工具调用冗余 | 实际工具调用数 / 最少必要数 | 越接近 1 越好 |

```python
# app/infrastructure/obs/eval/runner.py
class EvalRunner:
    def __init__(self, svc: DiagnosisService, cases: EvalCaseRepo) -> None:
        self._svc = svc
        self._cases = cases

    async def run_suite(self, model_version: str, prompt_version: str) -> EvalReport:
        results = []
        for case in await self._cases.all():
            report = await self._svc.diagnose(case.to_request(), case.tenant)
            results.append(EvalResult(
                case_id=case.id,
                accuracy=case.match(report),           # top-1 假设命中?
                evidence_recall=case.evidence_recall(report),
                confidence=report.confidence,
            ))
        return EvalReport(
            model_version=model_version,
            prompt_version=prompt_version,
            accuracy=sum(r.accuracy for r in results) / len(results),
            ece=self._expected_calibration(results),
            details=results,
        )
```

---

## 11. 漂移与异常检测（Drift）

### 11.1 输入漂移

- **问题分布漂移**：5M1E 类别分布变化（如某车间突然大量 Material 类问题）——可能上游物料质量变了，也可能模型分错类了。
- **工具调用模式漂移**：某工具调用频率突增/突减——可能数据问题导致 Agent 反复查同一上下文。

### 11.2 输出漂移

- **置信度分布漂移**：用 PSI（Population Stability Index）比较本周与基线的置信度分布，PSI > 0.2 告警。
- **假设类别分布漂移**：5M1E 输出占比变化——模型可能对某类问题退化。

### 11.3 成本漂移

- token/会话 P95 超基线 1.5× 告警（§5.4）——可能是提示词变长、模型啰嗦、或反复重试。

### 11.4 检测手段

- 实时：prometheus 指标 + Grafana 告警规则（突增/突减）。
- 离线：每日批处理算 PSI / 分布对比，落 `drift_report`，推 AI 负责人。
- 🔴 漂移检测的基线窗口（如 7 天滚动）与告警阈值待线上数据积累后定。

---

## 12. 兜底机制与观测联动

### 12.1 兜底矩阵

| 触发条件 | 动作 | 观测落地 |
|---------|------|---------|
| `confidence < 0.5` | `needs_human_review=True`，推工程师不推操作工 | `agent_low_confidence_total` +1，report 标记 |
| 工具连续失败 3 次 | 终止会话，转人工 | `agent_tool_error_total`，session status=FAILED，span ERROR |
| `recursion_limit` 命中 | 转人工 | `agent_recursion_limit_hit_total` +1，span TIMEOUT |
| 整会话超时 60s/120s | 转人工 | `agent_session_timeout_total` +1 |
| LLM schema 校验失败重试 N 次 | 转人工 | `agent_llm_schema_error_total` +1 |
| 写工具未带有效 confirmation token（L3） | `WriteToolGate` 拒绝 + 告警 | `l3_write_rejected_total` +1，P0 告警 |
| 证据为空（hypothesis 无 evidence） | schema 校验失败，重试 | `agent_llm_schema_error_total` +1 |
| `subgraph_ref` 回查为空（L2） | 草稿标 `needs_review`，intent 注明证据不完整，不硬凑 | `l2_draft_low_confidence_total` +1 |
| 草稿 `requires_confirmation` 恒 True（L2） | 前端无法绕过确认下达；L2 不持有写 client | `l2_draft_rejected_total` +1 |
| gate 等待超 deadline（L3） | 自动挂起 `SUSPENDED`，推责任人，不无限阻塞 | `l3_suspended_total` +1 |
| barrier 未 PASS（L3） | 禁止推放行卡，分流到 agent 或挂起 | `l3_suspended_total` +1 |
| agent 连续失败 2 次（L3） | 标 `SUSPENDED`，推异常卡，不自动重试到死 | `l3_suspended_total` +1 |

### 12.2 兜底动作可观测

- 每个兜底分支都落 trace 事件 + 指标 + 日志，**无一例外**——兜底是最后一道闸，它自己必须被盯着。
- 兜底触发的报告都带 `disclaimer`：辅助假设，最终处置需工程师确认——与 MES 防错一致，宁可让人判。
- P0 告警（写工具被拦）直接触发 PagerDuty / 钉钉，因为它意味着红线可能被绕过。

---

## 13. 数据模型（表结构汇总）

### 13.1 通用观测表

```sql
llm_call_log
  - call_id (PK)
  - session_id (FK)
  - step_no
  - model
  - prompt_version
  - prompt_token_count
  - completion_token_count
  - latency_ms
  - finish_reason
  - tool_calls_produced
  - occurred_at
  - INDEX(session_id), INDEX(model, occurred_at)

eval_case
  - case_id (PK)
  - scenario
  - question
  - expected_hypotheses (JSON)
  - expected_evidence_types (JSON)
  - level
  - source                 -- HISTORY / HANDOFF / SYNTHETIC

eval_run
  - run_id (PK)
  - case_id (FK)
  - model_version
  - prompt_version
  - actual_report (JSON)
  - passed (BOOL)
  - accuracy_score (FLOAT)
  - evidence_recall (FLOAT)
  - ran_at

drift_report
  - report_id (PK)
  - metric                  -- confidence_dist / token_per_session / ...
  - psi (FLOAT)
  - baseline_window
  - compared_at
```

### 13.2 L1 / L2 / L3 各自表

- L1：`diagnosis_session` / `tool_call_trace` / `diagnosis_report`（[L1 §5.2](L1诊断型Agent/L1诊断型Agent-实现方案.md)）。
- L2：`draft_trace` / `draft_archive`（[L2 §5](L2草稿型Agent/L2草稿型Agent-实现方案.md)）。
- L3：`node_trace`（含 `node_type` CODE/agent）/ `gate_decision`（[L3 §5](L3编排型Agent/L3编排型Agent-实现方案.md)）。

### 13.3 保留与归档策略

- 🔴 `llm_call_log` / `tool_call_trace` 增长快，保留期限待定（建议热数据 30 天 + 冷归档 1 年，按合规要求调整）。
- 🔴 评测数据（`eval_run`）长期保留，用于版本对比与标定历史。
- 🔴 `diagnosis_report` 含证据链，建议随工单归档周期保留（便于质量追溯复盘）。

---

## 14. 包结构与代码骨架

### 14.1 obs/ 包扩展

在 [L1 §6](L1诊断型Agent/L1诊断型Agent-实现方案.md) 的 `infrastructure/obs/` 基础上扩展：

```text
app/infrastructure/obs/
  context.py            # ObservabilityContext
  port.py               # ObservabilityPort（抽象，业务节点依赖它）
  tracing.py            # Tracing（OTel span 封装）
  metrics.py            # MetricsCollector（prometheus）
  logging.py            # 结构化日志 + trace 关联
  redactor.py           # 脱敏（纯函数）
  llm_obs.py            # ObservableChatModel 包装、LlmCallLogger
  cost.py               # CostEstimator（单价表 -> 估算）
  calibration.py        # 置信度标定曲线
  eval/
    runner.py           # EvalRunner
    cases.py            # EvalCaseRepo
    metrics.py          # ECE / accuracy / recall
    drift.py            # PSI / 分布对比
```

### 14.2 核心抽象（ObservabilityPort）

业务节点依赖抽象，不依赖具体实现——符合 DIP，测试时可换 `InMemoryObservability`：

```python
# app/infrastructure/obs/port.py
from typing import Protocol

class ObservabilityPort(Protocol):
    def session_span(self, obs_ctx): ...        # contextmanager
    def tool_span(self, obs_ctx, descriptor): ...
    def llm_span(self, obs_ctx, model, prompt_version): ...
    def tool_ok(self, tool, latency_s): ...
    def tool_denied(self, tool): ...
    def tool_error(self, tool): ...
    def llm_called(self, model, prompt_version, prompt_tokens,
                   completion_tokens, latency_ms, finish_reason): ...
    def low_confidence(self, level): ...
    def write_blocked(self, write_tool): ...
```

### 14.3 ToolNode 注入观测（修订 L1 §7.2）

L1 的 `ToolNode` 已有 trace 落库 + 指标雏形，本篇把它收敛到依赖 `ObservabilityPort`，统一三层级：

```python
# app/infrastructure/ai/tool_node.py
class ToolNode:
    def __init__(self, registry, trace_repo, obs: ObservabilityPort) -> None:
        self._registry = registry
        self._trace_repo = trace_repo
        self._obs = obs

    async def __call__(self, state: AgentState) -> AgentState:
        obs_ctx = state["obs_ctx"]
        results = []
        for call in state["pending_tool_calls"]:
            tool = self._registry._descriptors.get(call["name"])
            if tool is None or not obs_ctx.tenant.can_access(tool.required_tenant_scopes):
                await self._trace_repo.save_denied(call["name"], obs_ctx.tenant)
                self._obs.tool_denied(call["name"])
                results.append(self._deny_result(call))
                continue
            t0 = time.perf_counter()
            with self._obs.tool_span(obs_ctx, tool):
                try:
                    args = tool.args_schema.model_validate(call["args"])
                    view = await tool.handler(**args.model_dump(), tenant=obs_ctx.tenant)
                    self._obs.tool_ok(tool.name, time.perf_counter() - t0)
                    await self._trace_repo.save_ok(
                        call["name"], tool.bounded_context, args, view,
                        int((time.perf_counter() - t0) * 1000), obs_ctx,
                    )
                    results.append(self._ok_result(call, view))
                except Exception as e:
                    self._obs.tool_error(tool.name)
                    await self._trace_repo.save_error(call["name"], e, obs_ctx)
                    results.append(self._err_result(call, str(e)))
        state["tool_results"] = results
        state["pending_tool_calls"] = []
        return state
```

- `trace_repo.save_*` 多传 `obs_ctx`，让 `tool_call_trace` 带 `trace_id` / `session_id` / `tenant`，证据链可串。
- 工具入参出参经 `Redactor` 后再落 `tool_call_trace` 与日志（§6.3）。

---

## 15. 部署与基础设施

### 15.1 组件拓扑

| 组件 | 作用 | 部署 |
|------|------|------|
| OTel Collector | 接收 Agent + Java 的 trace/metrics，转发 | K8s，与 Java 共用 |
| Tempo / Jaeger | trace 存储与查询 | K8s |
| Prometheus | 指标存储与告警 | K8s（与 Java 共用或独立） |
| Grafana | 统一看板（trace/指标/日志/证据） | K8s |
| Loki | 结构化日志存储 | K8s |
| MySQL | 业务可观测表（`tool_call_trace` 等） | 复用 MES MySQL（独立 schema） |

### 15.2 与 Java APM 对接

- Agent 与 Java 服务共用一个 OTel Collector，trace 在 Tempo 里跨语言连成一条。
- 指标各存各的 prometheus（或同一 prometheus 不同 job），Grafana 统一画。
- **不强制 Java 改造**：Java 侧已有 OTel agent 即可续接；没有则链路在该服务断，Agent 侧仍自洽（§1.4）。

### 15.3 K8s 部署

- `agent-service` Pod 挂 OTel SDK（sidecar 或 SDK 直发 Collector），prometheus `/metrics` 端口被 scrape。
- 观测后端（Collector/Tempo/Prometheus/Grafana/Loki）若 MES 已有则复用，没有则随 Agent 一起部署。
- 🔴 观测后端是否复用 MES 既有栈 vs 独立部署，待运维确认。

---

## 16. 隐私与合规

### 16.1 租户隔离

- 所有观测表带 `tenant`（车间/产线），查询按 `TenantContext` 过滤——A 车间工程师查不到 B 车间的 trace/报告。
- Grafana 看板按租户分租，指标 label 带 `tenant` 但不暴露跨租户数据。

### 16.2 脱敏

- 工具入参出参、prompt 摘要、日志中的敏感字段经 `Redactor` 脱敏（§6.3）后落库。
- 序列号、批次号、工艺参数阈值按白名单脱敏；PII 不采集。

### 16.3 评测数据合规

- 评测集来自真实案例时必须脱敏，且**不出生产环境**（不外发给模型供应商训练）。
- 🔴 评测数据是否可跨租户共享（如多车间共用一个不良案例库）待合规确认。

---

## 17. 实现步骤（分阶段）

### 阶段一：链路与指标底座（2 周）

1. 接 OTel Python SDK + `httpx` instrumentation，trace 发 Collector，`traceparent` 透传到 Java。
2. 实现 `ObservabilityContext` / `Tracing` / `MetricsCollector` / `ObservabilityPort`。
3. 修订 L1 `ToolNode` 依赖 `ObservabilityPort`，`tool_call_trace` 带 `trace_id`/`session_id`/`tenant`。
4. 接 prometheus `/metrics`，落地 §5.2 通用指标。
5. 结构化日志 + trace 关联（`LoggingContext`）。

### 阶段二：业务视图与证据链（1 周）

6. `llm_call_log` 表 + `ObservableChatModel` 包装，统一多 provider 观测。
7. `CostEstimator` + `agent_cost_usd_total`。
8. 工程师 UI：报告 + 证据链点开回溯 trace（[L1 §9 阶段三](L1诊断型Agent/L1诊断型Agent-实现方案.md)）。

### 阶段三：置信度与兜底（1 周）

9. 置信度三源融合（初始版用模型自评 + 阈值，🔴 权重待标定）。
10. 兜底矩阵全分支落观测（§12）。
11. SLI/SLO 告警规则上线（§5.4）。

### 阶段四：评测与漂移（2 周）

12. 建 `eval_case` / `eval_run` 表，沉淀首批评测集（从转人工报告闭环）。
13. `EvalRunner` + pytest 回归脚本，接 CI（🔴 门禁强度待定）。
14. 置信度标定（reliability diagram + Platt/isotonic，🔴 方法待选）。
15. 漂移检测：PSI 离线批处理 + 实时突增告警。

### 阶段五：L2/L3 对齐与加固

16. L2 `draft_trace`、L3 `node_trace`/`gate_decision` 接入同一 `ObservabilityPort`。
17. L3 专属指标（`l3_llm_invocation_total` 等）上线，重点盯"换线 PASS 时 LLM 调用为 0"。
18. 在线影子评测试点（🔴 流量比例待定）。
19. 脱敏规则按租户配置化，评测数据合规审查。

---

## 18. 约束落地检查清单

- [ ] 观测是只读旁路：trace/指标/日志表独立于 MES 业务表，不写业务表。
- [ ] 观测不进过点主事务，不挂在过点 P99 ≤200ms 路径上。
- [ ] 观测数据按 `TenantContext` 隔离，A 车间查不到 B 车间 trace。
- [ ] 每个会话一个 root span，`traceparent` 透传到下游 Java 服务（OTel httpx 自动注入）。
- [ ] `ToolNode` 依赖 `ObservabilityPort` 抽象，不直接依赖 OTel/prometheus 实现。
- [ ] 每步工具调用落 `tool_call_trace`，带 `trace_id`/`session_id`/`tenant`，证据链可回溯。
- [ ] LLM 调用统一经 `ObservableChatModel` 包装，`llm_call_log` 记 token/成本/模型/`prompt_version`。
- [ ] 报告每条 hypothesis 至少引用 1 条 evidence（trace_id），否则 schema 失败重试。
- [ ] `confidence < 0.5` 置 `needs_human_review`，不展示给操作工，`agent_low_confidence_total` +1。
- [ ] 兜底矩阵每分支都落 trace 事件 + 指标 + 日志。
- [ ] 写工具被拦（L3）触发 P0 告警。
- [ ] prompt 变更必改 `prompt_version`，过评测集回归。
- [ ] 敏感字段经 `Redactor` 脱敏后落库，PII 不采集。
- [ ] 观测失败不阻断业务（exporter 挂，会话照跑）。

---

## 19. 面试防守 Q&A

**Q：Agent 可观测性和普通微服务的 APM 有什么区别？为什么要单独设计？**
A：普通 APM 回答"接口慢不慢、服务挂没挂"，Agent 可观测性要额外回答"这条 AI 根因假设凭什么、要不要转人"。核心区别是产物——APM 出火焰图，Agent 出**证据链**（假设 -> trace_id -> 工具结果 -> 下游数据）。而且 Agent 多了通用服务没有的质量维度：准确率、置信度校准、漂移、token 成本。MES 场景下 AI 决策必须可审计，所以把可观测性独立成 L1/L2/L3 共用底座，而不是各写各的埋点。

**Q：trace 落 Tempo，又落 MySQL 的 `tool_call_trace`，不是重复吗？**
A：不重复，是同一份 trace 的两种投影。Tempo/Jaeger 给 SRE 看火焰图，是底；`tool_call_trace` 是平铺视图给工程师 UI 用，是面。工程师不需要懂 span 树，他点假设 -> 弹证据 -> 看工具入参出参，这个交互用 MySQL 平铺表最直接。两者同源（都来自 `ToolNode` 的同一次调用），trace_id 串起来，不各说各话。

**Q：观测挂了会不会影响 Agent 跑？**
A：不会。观测是旁路，业务节点依赖 `ObservabilityPort` 抽象，具体实现挂了（OTel exporter 挂、prometheus 挂）业务照样跑，只是该次无 trace。这和"Agent 失败最坏是没诊断出来"一脉相承——观测失败最坏是没记录，不会反噬业务。这条用 DIP 保证：业务依赖抽象，不依赖观测实现。

**Q：置信度模型说 0.8 就真有 80% 准吗？怎么保证不误导？**
A：不保证，所以要**标定**。离线用评测集跑，把预测按置信度分桶算每桶实际准确率，画 reliability diagram，overconfident 就用 Platt/isotonic 修正。标定曲线版本化，随模型/提示词变更重标。加上 `confidence < 0.5` 转 `needs_human_review` 不推操作工、报告带 disclaimer——多层兜底，宁可让人判。这不是单点信任模型自评，是标定 + 阈值 + 兜底的组合。

**Q：怎么保证提示词改了不出事？**
A：提示词即代码。每个 system prompt 有 `prompt_version`，随 `llm_call_log` 和 span 落库。改提示词必改版本号，改了必须过 `EvalRunner` 评测集回归，对比新旧版本的准确率/校准误差。还可在线影子评测，生产问题按比例复制给新提示词跑，不影响线上只对比差异。这样提示词变更可追溯、可回滚、可验证，不是凭感觉改。

**Q：跨语言怎么把链路串起来？Java 那边要改吗？**
A：靠 W3C `traceparent`。Agent 调 Java REST 时 OTel 的 httpx instrumentation 自动注入 header，Java 侧挂了 OTel agent 就续接同一 trace，在 Tempo 里跨语言连成一条。Java 不用为这个改——只要透传 W3C TraceContext（行业默认）。某 Java 服务没接 OTel，链路在那断开，但 Agent 侧 span 仍完整自洽，降级可接受。

**Q：转人工的比例为什么不一上来就告警上限？**
A：因为转人工是兜底在工作，是健康的——低置信转人说明防错在起作用。告警应该盯**突增**（同比 +50%），那才说明模型或数据出问题了。盯上限会把正常的兜底误报成故障，反而干扰。这个区别写进告警规则，体现你懂"可观测不是堆指标，是分清信号和噪声"。

**Q：token 成本怎么管？会不会烧钱？**
A：先分清"测得清"和"降得下"。测得清是本篇可观测性的事，降得下是 [AgentToken成本优化-设计与实现方案.md](AgentToken成本优化-设计与实现方案.md) 的事（优先级"少调 > 少发 > 调便宜的"：代码节点归零 / prompt caching / 工具结果压缩 / 版本化缓存 / 过评测门禁的模型路由）。测得清靠三件事：一是 `agent_token_total` 按 prompt/completion 计数，`agent_cost_usd_total` 按单价估算，按租户聚合给管理层看。二是成本漂移检测，token/会话 P95 超基线 1.5× 告警。三是 `recursion_limit` 限制最大步数，从根本上限死单会话 token 上限。L3 换线顺利时 LLM 调用应趋近 0（`l3_llm_invocation_total`），全靠代码节点跑——懂什么时候不用 AI，就是最大的省钱。

---

## 20. 一句话定位

"Agent 可观测性是 L1/L2/L3 共用的只读旁路底座，用 OTel trace + prometheus 指标 + 结构化日志把每次 AI 推理串成**可审计的证据链**（假设 -> trace_id -> 工具结果 -> 下游数据），并补齐通用 APM 没有的 Agent 质量维度——置信度标定、评测回归、漂移检测、token 成本；它本身不进过点主事务、不旁路写、受租户隔离，观测失败不反噬业务，与 MES 防错理念同构：所有 AI 决策可回溯、可度量、可转人，最终处置仍由工程师确认。"
