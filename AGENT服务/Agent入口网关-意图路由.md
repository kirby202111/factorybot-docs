# Agent 入口网关与意图路由（统一入口 + 能力分派 + 跨能力委托）

> **定位**：本文承接"统一入口 + 意图路由 + 跨能力委托"职责，落地形态是独立微服务 **`agent-gateway-service`**。它是 Agent 能力的**收口形态**，不是第四项 Agent 能力，也**不属于 Agentic RAG**。
>
> **与编排层的关系**：编排权唯一在 `agent-service`（[ADR-13](../架构设计v2/00-架构决策记录ADR.md)）。`rag-service` 只提供文档知识工具 `search_docs`，制造追溯是 agent 侧只读工具（归属待定，见 [制造追溯-需求与实现效果.md](../制造追溯-需求与实现效果.md)）；网关完成分诊后，请求进入**单层**编排，不存在第二层 ReAct。入口分诊、跨能力委托、统一答案收敛全部归本文的网关。
>
> **口径纪律**：这是设计阶段的引入规划，不是线上已落地能力。讲法说"规划方向 / 设计取舍"，不说"我们已经上线了统一入口网关"。
>
> **技术栈**：Python 3.12+（FastAPI + Pydantic v2 + httpx）。与三大 MES 服务（Java/Spring）通过 REST 解耦，互不侵入。

---

## 1. 为什么路由要独立成网关

### 1.1 三种路由，必须拆开

| 层次 | 做什么 | 归属 |
|------|--------|------|
| **① 入口分诊** | 自然语言 → 该找哪项能力（检索 / 诊断 / 草稿） | **本文网关** |
| **② 能力分派与委托** | 选定能力 → 发起调用、统一收敛答案 | **本文网关** |
| **③ Agent 内部工具选择** | 选哪个工具、下一步查什么 | **`agent-service` 编排层（单层）** |

把 ① 放进检索侧会让文档服务承担入口职责，违背 [rag-service-整体设计.md](../RAG服务/rag-service-整体设计.md) §0 的定位：“文档知识工具只回答一件事：给定问题与版本锚点，召回可引用证据”，**多步编排归 `agent-service`**（[ADR-13](../架构设计v2/00-架构决策记录ADR.md)）。

### 1.2 为什么独立成网关，而不是并进 agent-service

- **依赖方向会成环**：若入口放进 agent-service，它要反向依赖 rag-service 文档工具与追溯工具；而网关侧又要委托 agent-service 诊断/草稿，两个服务互相依赖，发布与故障隔离全耦合。
- **流量性质不同**：入口是"人在问"（同步、低 QPS、强审计）；agent-service 还有"主动触发"（订阅 `equipment.fault` 等事件巡检）。混在一起，限流与扩容策略打架。
- **变化频率不同**：渠道、鉴权、限流、会话、审计这些入口职责变化远快于"5M1E 推理链"，绑在一起会拖慢能力迭代。
- **单一职责**：诊断/草稿是"深而专"的专精能力，不该兼做"广而浅"的分诊。

### 1.3 收口不等于每个请求都跑 Agent

网关的**分派是确定性的**：分类 → 调对应能力的 HTTP 接口 → 收敛答案。只有"规则命中不了"的兜底分类才用一次 LLM（小模型 + Enum 强约束）。这样路由层薄、可审计、成本可控，也不会因为一次误分类就进入自由 ReAct 循环。

---

## 2. 职责边界

| 维度 | 本文网关 | agent-service 编排层（检索内 flow） | 诊断能力 | 草稿能力 |
|------|---------|---------------------------|---------|---------|
| 形态 | 入口分诊 + 确定性分派 + 统一答案 | 单层 ReAct + 确定性 flow（`version_resolve` → `trace_lookup` → `search_docs`）+ 证据收口 | 多步只读 ReAct 诊断 | 固定链草拟（不落库） |
| 是否做意图分类 | **是** | 否 | 否 | 否 |
| 是否跨能力委托 | **是** | 否（工具调用不是委托） | 否 | 否 |
| 推理深度 | 浅（分派，无 ReAct） | 轻（≤3 次工具调用） | 深（≤10 步 5M1E） | 中（拉数据 + 草拟） |
| 写权限 | 无 | 无 | 无 | 草稿不落库 |
| 典型问题 | "这问题该找谁" | "还要再查哪份证据" | "根因是什么" | "草拟返工单" |

---

## 3. 意图分类（3 类能力 + 1 兜底）

意图**只对齐能力边界**，不再细分检索内部类型（追溯事实 / 文档查询的区分下沉到 `agent-service` 编排层的工具选择）：

| 意图 | 分派到 | 形态 | 典型问题 |
|------|--------|------|---------|
| `RETRIEVAL_REQUEST` | `agent-service` 编排层 `POST /agent/retrieval` | 单层检索编排 | "SN-001 过了哪几站""SPI 报警怎么处置""这批用了哪批锡膏" |
| `DIAGNOSIS_REQUEST` | 诊断能力 `POST /agent/diagnose` | 显式委托 | "SN-001 焊接不良根因""这批不良 5M1E" |
| `DRAFT_REQUEST` | 草稿能力 `POST /agent/draft` | 显式委托 | "给这批不良草拟返工单""草拟 8D 报告" |
| `UNKNOWN` | 转人工 | 兜底 | 路由不了的问题 |

> **边界原则**：问"过了哪几站 / 用了哪批 / SOP 怎么处置"都是 `RETRIEVAL_REQUEST`——是否需要多步、先查 `trace_lookup` 还是 `search_docs`，由编排层在检索内决定。问"为什么不良"是 `DIAGNOSIS_REQUEST`——需要多步 5M1E 推理，委托诊断能力。

---

## 4. 路由规则（规则优先 + LLM 兜底）

```text
IntentRouter.classify(question)
   │
   ├─ 1. 规则优先（命中即返回，0 次 LLM）
   │     ├─ 含"根因/5M1E/为什么不良/异常分析" -> DIAGNOSIS_REQUEST
   │     ├─ 含"草拟/生成返工单/8D/处置卡"     -> DRAFT_REQUEST
   │     └─ 含"SN/批次/工单/过点/物料/设备/SOP/怎么处置/流程" -> RETRIEVAL_REQUEST
   │
   ├─ 2. LLM 兜底（规则命中不了）
   │     小模型 + Content Block 强制 Enum，只能选枚举值
   │
   └─ 3. 仍不确定 / 调用失败 -> UNKNOWN -> 转人工
```

- **规则优先**：高频问题不走 LLM，确定性强、成本低、可回归。
- **LLM 兜底强约束**：`GatewayIntent` 固化为 Enum，避免编造意图。
- **UNKNOWN 不硬答**：转人工"该问题暂不支持，请联系工程师"。
- **模型规格**：兜底分类用小/便宜模型（Haiku / DeepSeek / 本地小模型），且换模型必须过 `EvalGate`（[AgentToken成本优化](AgentToken成本优化-设计与实现方案.md) §5.2）。

---

## 5. 分派执行（确定性，不跑 ReAct）

```text
分派 intent
 ├─ RETRIEVAL_REQUEST ──▶ RetrievalClient.delegate(question, seed, version_anchor, budget)
 │                        └─ 返回 RagAnswer（证据集 + sources + tool_chain）
 ├─ DIAGNOSIS_REQUEST ──▶ DelegationClient.invoke_diagnosis(...)  (≤60s)
 ├─ DRAFT_REQUEST     ──▶ DelegationClient.invoke_draft(...)     (≤30s)
 └─ UNKNOWN           ──▶ 转人工兜底
```

- **不挂 ReAct**：网关不推理、不选工具、不做多步组合；选工具是多跳检索内部的事。
- **缓存（策略化）**：`data_scope` **只由 `as_of`** 派生（纯函数，0 次 LLM，见 [Agent记忆系统-设计与实现方案.md](Agent记忆系统-设计与实现方案.md) §3.10.1）；**仅 `AS_OF`** 才按"问题 + 租户 + 版本锚点"读写答案缓存并命中返回，`CURRENT` 一律跳过——**即使请求带 `version`**。版本锚点是贯穿全链路的约束条件（记忆系统 §8.2 版本一致性红线），不是取数语义；若因"带了版本"就缓存答案，"SN-001 按 v3 路线过了哪几站"这类现场查询会拿到旧过点轨迹，直接违反时变性红线。
- **预算与超时**：整问答 70s（略大于诊断 60s 委托超时）；检索子调用 15s（🔴 待压测标定）。
- **只读**：网关无任何写工具、不领用写令牌、不调过点放行/拦截 API。

---

## 6. 与 agent-service 编排层的契约

### 6.1 请求

```python
class RetrievalRequest(BaseModel):
    question: str
    seed: dict | None = None          # {"sn": "..."} / {"batch_no": "..."} / {"wo_id": "..."}
    as_of: datetime | None = None     # 历史回放时点
    version: str | None = None        # 版本锚点
    version_kind: str | None = None
    session_id: str | None = None
    max_tool_calls: int = 2           # 检索内预算，由网关透传但不放大
```

> **不新增 `data_scope` 字段**：`data_scope` **只由 `as_of`** 派生（`AS_OF` / `CURRENT`），网关与 `agent-service` 用共享内核里的同一个纯函数各自计算，见 [Agent记忆系统-设计与实现方案.md](Agent记忆系统-设计与实现方案.md) §3.10.1。`version` / `version_kind` 仍按原语义作版本锚点透传，**不参与 `data_scope` 派生、也不放宽缓存策略**；这样版本锚点仍只有一套口径，网关也不必成为隐式会话中枢。

### 6.2 响应（`RagAnswer`）

由编排层返回，网关经 Mapper 转成 `AgentAnswer`：

| RagAnswer 字段 | 含义 |
|----------------|------|
| `retrieval_path` | `trace_lookup` / `search_docs` / 两者组合（工具序列，非能力路由） |
| `tool_chain` | 实际调用的检索工具序列 |
| `stop_reason` | 为何停（enough / no_new_evidence / budget / conflict），网关只透传不改写 |
| `evidence_set_id` | 本次证据集标识，供回查 |
| `sources` | 来源引用（SOP 带版本、证据 ID） |
| `confidence` / `needs_human_review` | 置信度与人工复核标记 |
| `trace_id` | 检索侧 span 标识 |

> **关键约束**：网关**不传消息历史**，只传问题、种子、版本锚点与会话 ID；检索侧不感知"用户上一轮问了诊断还是检索"。

---

## 7. 与诊断 / 草稿能力的委托契约

委托是**显式服务调用**，不是框架移交（AgentScope 2.0 从未提供 `Handoffs`/`Routing` 原语）：

```python
class DelegationClient:
    """显式委托诊断/草稿：只传标识，不传消息历史。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def invoke_diagnosis(
        self, question: str, evidence_set_id: str, session_id: str,
        traceparent: str, tenant: TenantContext,
    ) -> ReportView:
        resp = await self._http.post(
            "/agent/diagnose",
            json={"question": question, "evidence_set_id": evidence_set_id,
                  "session_id": session_id},
            headers={**tenant.headers(), "traceparent": traceparent},
            timeout=60.0,          # 诊断能力整会话 ≤60s
        )
        resp.raise_for_status()
        return ReportMapper.to_view(resp.json())

    async def invoke_draft(
        self, draft_kind: str, evidence_set_id: str, session_id: str,
        traceparent: str, tenant: TenantContext,
    ) -> DraftView:
        resp = await self._http.post(
            "/agent/draft",
            json={"kind": draft_kind, "evidence_set_id": evidence_set_id,
                  "session_id": session_id},
            headers={**tenant.headers(), "traceparent": traceparent},
            timeout=30.0,
        )
        resp.raise_for_status()
        return DraftMapper.to_view(resp.json())
```

- **只传标识**：`evidence_set_id` / `session_id` / `traceparent`，被委托方按 `evidence_set_id` 自行回查证据。
- **超时对齐**：诊断 ≤60s、草稿 ≤30s；超时降级"未完成，已转人工"。
- **结果转统一格式**：`DiagnosisReport` / 草稿都经 Mapper 转 `AgentAnswer`，用户看到一致体验。

---

## 8. 统一答案（AgentAnswer）

```python
class AnswerSource(BaseModel):
    source_type: str          # "trace_record" / "sop_doc" / "diagnosis_hypothesis" / "draft"
    ref: str                  # "evidence_id=..." / "SOP:WELD-014@v3" / "audit_id=..."
    route: str                # "retrieval" / "diagnosis" / "drafting"

class AgentAnswer(BaseModel):
    question: str
    intent: str               # GatewayIntent
    route_taken: str          # 实际走的能力："retrieval" / "diagnosis" / "drafting" / "HUMAN"
    summary: str
    detail: dict              # 能力相关结构化详情（证据集 / 报告 / 草稿）
    evidence_set_id: str
    sources: list[AnswerSource]
    confidence: float
    tool_chain: list[str]     # ["retrieval:agentic_query"] / ["diagnosis:diagnose"]
    trace_id: str
    needs_human_review: bool = False
    disclaimer: str = "本答案为辅助信息，最终处置需工程师在正式界面确认"
```

- `route_taken` 表达**结果**，`tool_chain` 表达**过程**（尝试了哪些调用、是否失败），职责不混。
- 失败统一：`route_taken="HUMAN"` + `detail["reason"]`，所尝试路线记在 `tool_chain`。
- `sources` 强制引用；`disclaimer` 不可省。

---

## 9. 网关编排服务（GatewayService）

```python
class BudgetExceededError(Exception):
    """应用层预算守卫（非框架内置）：超时 / 预算超限。"""

class GatewayService:
    def __init__(
        self,
        intent_router: IntentRouter,
        retrieval_client: RetrievalClient,       # 调 agent-service 检索端点
        delegation_client: DelegationClient,     # 调诊断 / 草稿
        cache: QueryCache,
        audit_repo: AnswerAuditRepo,
        metrics: MetricsCollector,
    ) -> None: ...

    async def chat(self, request: ChatRequest, tenant: TenantContext) -> AgentAnswer:
        # 1. 缓存（策略化，见 Agent记忆系统 §3.10；resolve 为共享内核纯函数，0 次 LLM）
        scope = resolve_data_scope(request)
        allow_cache = scope.allows_answer_cache        # AS_OF -> True / CURRENT -> False（与 version 无关）
        if allow_cache:
            cached = await self._cache.get(request, tenant)
            if cached:
                self._metrics.cache_hit.inc()
                return cached

        # 2. 意图分诊（规则优先 + LLM 兜底）
        intent = await self._intent_router.classify(request.question)
        self._metrics.intent.inc(intent.value)

        # 3. 确定性分派（不做 ReAct、不做多步组合）
        try:
            match intent:
                case GatewayIntent.RETRIEVAL_REQUEST:
                    retrieval_answer = await self._retrieval_client.delegate(
                        RetrievalRequest(
                            question=request.question,
                            seed=request.seed,
                            as_of=request.as_of,
                            version=request.version,
                            version_kind=request.version_kind,
                            session_id=request.session_id,
                        ),
                        tenant,
                    )
                    answer = self._to_agent_answer(request, intent, retrieval_answer)
                case GatewayIntent.DIAGNOSIS_REQUEST:
                    report = await self._delegation_client.invoke_diagnosis(
                        request.question, request.evidence_set_id or "",
                        request.session_id or "", request.traceparent, tenant,
                    )
                    answer = self._to_agent_answer(request, intent, report)
                case GatewayIntent.DRAFT_REQUEST:
                    draft = await self._delegation_client.invoke_draft(
                        request.draft_kind or "rework_order",
                        request.evidence_set_id or "", request.session_id or "",
                        request.traceparent, tenant,
                    )
                    answer = self._to_agent_answer(request, intent, draft)
                case _:
                    answer = self._human_fallback(request, intent, "该问题暂不支持，请联系工程师")
        except (asyncio.TimeoutError, BudgetExceededError) as e:
            self._metrics.fallback.inc("timeout_or_budget")
            answer = self._human_fallback(request, intent, f"问答未完成，已转人工: {e}")
        except Exception as e:
            self._metrics.fallback.inc("downstream_error")
            answer = self._human_fallback(request, intent, f"下游能力不可用: {e}")

        # 4. 审计 + 缓存（同一策略，避免 CURRENT 答案被写入缓存）
        await self._audit_repo.record(request, intent, answer)
        if allow_cache:
            await self._cache.set(request, tenant, answer)
        return answer
```

> 会话状态不放在网关：网关是无状态分派器；跨轮会话由各能力自己的 `AgentState` + `SessionService`（storage 选 `AsyncSQLAlchemyStorage` 落 MySQL）承载。

---

## 10. 包结构

```text
agent_gateway_service/
  app/
    api/
      chat_router.py            # POST /agent/chat, GET /agent/explain/{audit_id}
      schemas.py
    application/
      gateway_service.py        # 分派编排
      intent_router.py          # 规则优先 + LLM 兜底
    domain/
      intent.py                 # GatewayIntent 枚举
      answer.py                 # AgentAnswer / AnswerSource
      tenant.py                 # TenantContext
    infrastructure/
      ai/
        classifier_factory.py   # 兜底分类 Agent（不挂工具，Enum 输出）
        model_factory.py        # AgentScope Model 接入（provider 可插拔）
      acl/
        retrieval.py            # 调 agent-service POST /agent/retrieval
        diagnosis_delegation.py # 显式委托诊断
        draft_delegation.py     # 显式委托草稿
      persistence/
        models.py               # answer_audit / route_trace
        audit_repo.py
      redis_/
        query_cache.py
      obs/
        tracing.py              # TracingMiddleware 接线
        metrics.py
    config.py
    main.py                     # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
  Dockerfile
  docker-compose.yml
```

- 启动断言：`assert_dependencies_reachable`（`agent-service` 的检索 / 诊断 / 草稿端点必须可达），否则拒绝启动——网关是收口，依赖没就绪就是套壳。
- 部署：独立微服务 `agent-gateway-service`（uvicorn + gunicorn worker），K8s 同网格；MVP 可 docker-compose 本地起。

---

## 11. 可观测与兜底

### 11.1 指标

| 指标 | 含义 |
|------|------|
| `gateway_chat_total` | 问答次数（按 intent label） |
| `gateway_route_accuracy` | 路由准确率（评测集 / 用户反馈，健康度核心） |
| `gateway_delegation_total` | 委托次数（按 capability / status） |
| `gateway_chat_latency_seconds` | 整问答延迟（Histogram） |
| `gateway_cache_hit_total` | 缓存命中（按 `data_scope` 打 label） |
| `gateway_cache_skip_total` | 按策略跳过答案缓存的次数（`CURRENT`；📐 设计目标） |
| `gateway_low_confidence_total` | 低置信度转人工 |
| `gateway_unknown_intent_total` | 未识别意图转人工 |
| `gateway_dependency_unreachable_total` | 下游不可达降级 |

> `CURRENT` 请求不计入 `gateway_cache_hit_total` 的命中率分母——否则"按策略跳过缓存"会被统计成"缓存没命中"，指标反过来逼人把时变性红线调回去。能力侧 `AnswerValidator` 判定证据出处不合规时置 `needs_human_review`，网关只透传不改写；`stale_fact_rate`（该查却用了旧值）是评测侧 `EvalGate` 门禁指标，不在网关埋点，见 [Agent记忆系统-设计与实现方案.md](Agent记忆系统-设计与实现方案.md) §3.10.6。

### 11.2 trace

- `trace_id` 从网关生成，`TracingMiddleware` 在 `IntentRouter`、各 ACL 客户端注入 span。
- 委托时透传 `traceparent`（🔴 待与诊断/草稿实现确认接收与透传），未透传时网关侧仍保留委托 span（降级可观测）。
- `AnswerAudit` 记录问题 / 意图 / 路由 / 工具链 / 答案摘要 / `trace_id`，`/agent/explain/{audit_id}` 可回溯。

### 11.3 兜底

- **路由失败**：`UNKNOWN` → 转人工"该问题暂不支持"。
- **下游不可达**：检索 / 诊断 / 草稿故障 → 降级"该能力暂时不可用"或转人工。
- **低置信度**：`confidence < 0.6` → `needs_human_review=True`。
- **委托超时**：诊断 ≤60s、草稿 ≤30s，超时转人工。
- **误路由**：路由准确率监控 + 评测集回归 + `AnswerAudit` 回溯，不硬答。

---

## 12. 实现步骤

### 阶段一：骨架与分诊（1-2 周）

1. 搭 `agent_gateway_service` 骨架（FastAPI + uvicorn）。
2. 实现 `IntentRouter`（规则优先 + LLM 兜底，Enum 强约束）。
3. 实现 `GatewayService` 确定性分派（先打通 `RETRIEVAL_REQUEST` 路径）。

### 阶段二：委托与统一答案（2 周）

4. 实现 `RetrievalClient`（调 `agent-service` 检索端点）与 `DelegationClient`（诊断 / 草稿）。
5. 实现 `AgentAnswer` 统一收敛与 Mapper。
6. 验证 `traceparent` 透传与超时降级。

### 阶段三：可观测与加固（1 周）

7. `AnswerAudit` + `/agent/explain` 回溯。
8. OTel + prometheus；路由准确率告警。
9. 兜底链路全测（UNKNOWN / 下游不可达 / 低置信度 / 超时 / 权限不足）。
10. 灰度试点，收集"路由是否准、答案是否对"反馈。

> **前提**：网关必须在 `agent-service` 的编排层（含追溯工具）与文档知识工具成型后启动，否则无能力可分派。

---

## 13. 面试防守 Q&A

**Q：意图路由为什么不放在检索侧（原 Agentic RAG）里？**
A：检索侧的定位是“给定问题与锚点，一次召回拿对证据”，多步编排已上移到 `agent-service`（[ADR-13](../架构设计v2/00-架构决策记录ADR.md)）。入口分诊要判断“该找检索、诊断还是草稿”，这是跨能力职责，放进文档服务会让它变成入口层，既违背定位，也会让文档服务反向依赖 agent-service。

**Q：为什么不放进 agent-service？**
A：会成环。入口要调 agent-service（诊断/草稿/编排），而 agent-service 又要回调文档工具与追溯工具，两个服务互相依赖。另外入口是"人在问"的同步低 QPS 流量，agent-service 还有事件驱动的主动触发，两种流量混在一起，限流、扩容、发布全都耦合。

**Q：网关用不用 LLM？**
A：只在兜底分类用一次小模型。规则命中时 0 次 LLM；分派本身是确定性 HTTP 调用，不跑 ReAct。路由是策略不是能力，越薄越可控。

**Q：路由错了怎么办？**
A：三重兜底——路由准确率监控 + 评测集回归；被委托能力有超时与置信度阈值，低置信度回传后转人工；`UNKNOWN` 与失败一律转人工不硬答。`AnswerAudit` 记录每次分派，可回溯调规则。

**Q：网关和编排层的接口为什么不传消息历史？**
A：只传问题、种子、版本锚点与会话 ID。被调方按 `evidence_set_id` / `session_id` 自行回查上下文，避免网关成为隐式的会话中枢，也避免跨能力上下文污染。

---

## 14. 一句话定位

"统一入口与意图路由归独立的 `agent-gateway-service`：规则优先 + LLM 兜底把问题分成检索 / 诊断 / 草稿三类能力，确定性分派 + 跨能力显式委托 + 统一答案收敛。网关不推理、不选检索工具、不做多步组合——检索内部怎么查归 `agent-service` 编排层，深度诊断归诊断能力，草稿归草稿能力。"
