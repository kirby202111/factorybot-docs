# FactoryAgent 草稿编写能力（写意图草拟）

> 本文是 [FactoryAgent-总体设计与实现方案.md](FactoryAgent-总体设计与实现方案.md) 中**草稿编写能力（`drafting`）**的落地展开。
> **定位**：草稿编写 = 把诊断结论变成**可下达的处置草稿**。步骤固定（取证据 → 检索知识 → 结构化生成 → 校验存档），差异用策略分派表达；输出 `intent + draft`，`requires_confirmation` 恒真，**不落 MES 业务库**。
> **与诊断的关系**：诊断解决"根因是什么"，草稿解决"该怎么处置、单子怎么填"。草稿**消费** `DiagnosisReport`（含 `subgraph_ref` 与版本锚点），不重复推理、不重查图。
> **口径纪律**：草稿能力**不持有任何写 client**、**永不直接落库**，绝不旁路任何上下文的写路径（[领域总览.md](../../领域模型/领域总览.md) §5.3）。MES 对误写零容忍（错发一张返工单 = 批量报废），写动作的闸门 100% 在人手里。

---

## 1. 定位与边界

### 1.1 这项能力解决什么

诊断给出"这批 SN 焊接不良，根因大概率是锡膏批次 B-77 回温不足"之后，工程师还要做四件事：翻追溯链确认受影响范围、找历史同类 8D 参考、查返工路线、把单子字段一项项填进界面。草稿编写能力把这四步合成一条链：

1. 按诊断传来的 `subgraph_ref` 回查图节点，提取 `source_work_order_id`、`affected_sn_list`、`reentry_point`
2. 调文档型 RAG 检索历史同类 8D / 现有 SOP / 模板
3. LLM 综合成结构化草稿（`intent + payload + evidence_refs`）
4. 工程师在正式界面审核确认 → 走对应上下文的**正常应用服务**落库

### 1.2 图形态：固定链 + 策略分派

草稿的步骤是**固定的**，没有"模型自主决定下一步查什么"的开放性——这是它与诊断能力的本质差异，也是三问（输入是否开放 / 是否需推理生成 / 分支是否难穷举）中"分支可穷举"的体现。

```text
DraftRequest(report, draft_kind, tenant)
        │
        ▼
[resolve_evidence]   代码：按 subgraph_ref 回查图节点，提取证据字段 + 版本锚点
        │
        ▼
[retrieve_knowledge] 代码：search_docs（历史 8D / 现有 SOP / 模板），带版本过滤
        │
        ▼
[generate]           LLM：按 draft_kind 分派到对应 DraftBuilder，with_structured_output(Draft)
        │
        ▼
[validate_archive]   代码：Pydantic 校验 + requires_confirmation=True + 置信度分级 + 落草稿存档表
        │
        ▼
Draft（返回给前端，等人在正式界面确认）
```

- **为什么仍是 LangGraph 图**：为了与诊断、编排共用同一个运行时——同一套 checkpointer（大草稿生成中断可恢复）、同一套 trace 注入、同一套 `recursion_limit`。**不是**为了开放规划，草稿图的边是硬编码的直线。
- **为什么差异用策略而不是图分支**：返工单 / 8D / SOP / 处置卡 / 隔离集卡的差异是"证据字段 + prompt + payload schema"，用 `DraftBuilder` 子类表达最直接（新增类型只加一个 builder，不改图）。

### 1.3 硬边界

| 边界 | 说明 | 落地 |
|------|------|------|
| **只检索 + 草拟，不落库** | 只产 `Draft`，`requires_confirmation=True` 恒成立 | `CapabilitySpec(writes_allowed=False)`；`Draft` 只落 `agent_draft` 存档表 |
| **不持有写 client** | ACL 只有 `fetch/query/search` 动词 | `validate_acl_clients` 扫描方法名前缀 |
| **不旁路应用服务写** | 落库走返工 / 工单 / 质量 / 工艺上下文的正常应用服务 | confirmation gate 的"下达"由前端调 MES 正式 API，草稿服务完全不参与写路径 |
| **不进过点主事务** | 草拟异步，与过点判定解耦 | 草拟秒级；过点 P99 ≤200ms 不受影响 |
| **版本一致性** | 草稿锁定的版本透传自诊断证据，不自行指定 | `Draft.version/kind/ref_id` 来自 `DiagnosisReport`；返工路线 `ReworkRoute` 走独立版本规则（待工艺上下文明确） |
| **不开放图检索** | 只按 `subgraph_ref` 回查图节点 | `fetch_subgraph_nodes(subgraph_ref)`，不调 `query_traceability_graph`（开放检索归诊断） |
| **权限隔离** | 租户上下文透传全程 | `DraftRequest.tenant` 透传到图回查 / 文档检索 |

### 1.4 与其它能力的关系

- **上游**：诊断能力提供 `DiagnosisReport`（根因 + 证据 + `subgraph_ref` + 版本锚点）。触发方式两种：工程师在报告页点"生成处置草稿"（人工发起），或编排能力在 gate 前调用（自动续接）。
- **下游**：人在正式界面确认后，由前端调 MES 正式 API 落库——**不在 Agent 进程内发生**。
- **被编排复用**：编排的 `draft_isolation` / `draft_8d` / `draft_sop` / `draft_disposition` 节点直接 `invoke("drafting", {...}, parent_session_id=...)`，不复制 builder。

---

## 2. 五类草稿

| 草稿类型 | `draft_kind` | 落库上下文 | 关键字段 | 证据来源 |
|---------|-------------|-----------|---------|---------|
| 返工单 | `REWORK_ORDER` | 返工上下文（`brework.*`） | `source_work_order_id`、`affected_sn_list`、`reentry_point`、`rework_route_ref` | 图 `subgraph_ref`（`WipUnit` / `WorkOrder` / `RouteStep`） |
| 8D 报告 | `EIGHT_D` | 质量上下文 | 问题描述 / 根因 / containment / 纠正措施 / 验证 | 图 5M1E 证据 + 历史 8D + 诊断假设排序 |
| SOP | `SOP` | 工艺管理上下文 | 工序步骤 / 参数 / 版本 | `ProcessRouteActivated` 新版本 + 文档型 RAG 现有 SOP |
| 处置卡 | `DISPOSITION_CARD` | 对应上下文应用服务（钢网归还 / 领用 / 台账修正…） | `root_cause_hypothesis`、`suggested_actions`、`route_to` | 诊断根因 + 钢网借还 / 工艺审计 / 收线记录 |
| 隔离集卡 | `ISOLATION_CARD` | 返工 / 返修上下文 | `batch_set`、每批隔离理由、`release_set`（不受影响） | 设备遥测窗口 + FMEA + 产品敏感度 + 在库品查询 |

> 返工单草稿对齐 [返工上下文.md](../../领域模型/生产执行服务/事件风暴/返工上下文.md)：`BatchReworkOrder` 覆盖一批 SN，关联源工单 + SN 清单 + **返工工艺路线 `ReworkRoute`**（独立于正常工艺路线）+ 批量再入点 `reentry_point`。下达走返工上下文"人工决策生成工单"通道，发布 `brework.order.released`。

**处置卡与隔离集卡的意义**：它们让"编排能力的非确定节点"有了统一的产物形态——节点只负责"用诊断能力判清楚"，产物统一交给草稿能力"按模板结构化成卡"，再由 gate 等人确认。这样编排层不需要知道每类卡的字段怎么填。

---

## 3. 输出结构

```python
class DraftKind(str, Enum):
    REWORK_ORDER = "REWORK_ORDER"
    EIGHT_D = "EIGHT_D"
    SOP = "SOP"
    DISPOSITION_CARD = "DISPOSITION_CARD"
    ISOLATION_CARD = "ISOLATION_CARD"

class Draft(BaseModel):
    draft_id: str
    draft_kind: DraftKind
    intent: str                         # "对 WO-2026-0707-001 的 12 件 SN 执行焊接返工，再入点 ST-05"
    payload: dict                       # 返工单字段 / 8D 段落 / SOP 步骤 / 卡片动作
    evidence_refs: list[str]            # ["subgraph_ref=...", "trace_id=..."]
    version: str | None = None          # 版本锚点（透传自诊断证据）
    version_kind: str | None = None     # route|bom|rule|asset|standard
    version_ref_id: str | None = None
    confidence: float
    requires_confirmation: bool = True  # 恒为 True
    needs_review: bool = False          # confidence < 0.5 或证据缺失
    disclaimer: str = "本草稿为辅助草拟，下达需工程师在正式界面确认"
```

- `intent` 让工程师一眼看懂草稿要干什么，再看 `payload` 细节。
- `requires_confirmation` 恒 `True`——草稿能力**从不产出"可直接落库"的草稿**。
- `evidence_refs` 让草稿每个字段可回溯到图节点 / 诊断证据，支撑"基于证据确认而非盲批"。

---

## 4. 关键实现骨架

### 4.1 草稿链装配

```python
# app/ai/draft_chain.py
def build_draft_chain(deps) -> CompiledGraph:
    g = StateGraph(DraftState)
    g.add_node("resolve_evidence", deps.draft_nodes.resolve_evidence)      # 代码
    g.add_node("retrieve_knowledge", deps.draft_nodes.retrieve_knowledge)  # 代码
    g.add_node("generate", deps.draft_nodes.generate)                      # LLM（策略分派）
    g.add_node("validate_archive", deps.draft_nodes.validate_archive)      # 代码
    g.add_edge(START, "resolve_evidence")
    g.add_edge("resolve_evidence", "retrieve_knowledge")
    g.add_edge("retrieve_knowledge", "generate")
    g.add_edge("generate", "validate_archive")
    g.add_edge("validate_archive", END)
    return g.compile(checkpointer=deps.checkpointer)
```

### 4.2 生成节点（策略分派）

```python
# app/application/builders/base.py
class DraftBuilder(Protocol):
    """一个草稿类型一个 builder：只负责证据 -> prompt -> 结构化草稿。"""
    draft_kind: DraftKind

    async def build(self, report: DiagnosisReport, evidence: list[TraceNodeView],
                    knowledge: list[DocHit], tenant: TenantContext) -> Draft: ...
```

```python
# app/application/draft_service.py
class DraftService:
    def __init__(self, builders: dict[DraftKind, DraftBuilder], draft_repo: DraftRepo,
                 metrics: MetricsCollector) -> None:
        self._builders = builders
        self._draft_repo = draft_repo
        self._metrics = metrics

    async def draft(self, req: DraftRequest) -> Draft:
        builder = self._builders.get(req.draft_kind)
        if builder is None:
            raise ValueError(f"不支持的草稿类型: {req.draft_kind}")
        draft = await builder.build(req.report, req.evidence, req.knowledge, req.tenant)
        draft.requires_confirmation = True
        if draft.confidence < 0.5 or not req.evidence:
            draft.needs_review = True          # 证据缺失不硬凑
        await self._draft_repo.archive(draft)  # 只落草稿存档，不落 MES 业务库
        self._metrics.draft_total.inc(req.draft_kind.value, "ok")
        return draft
```

### 4.3 只读 ACL（回查图 + 检索文档）

```python
# app/infrastructure/acl/rag.py
class RagAclClient:
    """图服务只读 ACL：按 subgraph_ref 回查图节点，不开放图检索。"""

    async def fetch_subgraph_nodes(
        self, subgraph_ref: str, tenant: TenantContext
    ) -> list[TraceNodeView]:
        resp = await self._http.get(
            f"/rag/trace/subgraph/{subgraph_ref}",
            headers=tenant.headers(),
            timeout=2.0,
        )
        resp.raise_for_status()
        return [TraceNodeMapper.to_view(n) for n in resp.json()["nodes"]]
```

```python
# app/infrastructure/acl/doc_rag.py
class DocRagAclClient:
    """文档型 RAG 只读 ACL：历史 8D / 现有 SOP / 模板检索，带版本过滤。"""

    async def search_docs(
        self, query: str, tenant: TenantContext,
        version: str | None = None, version_kind: str | None = None,
        version_ref_id: str | None = None,
    ) -> list[DocHit]:
        ...
```

### 4.4 confirmation gate 落库流程（在草稿能力之外）

```text
草稿能力产出 Draft（requires_confirmation=True）
        │
        ▼  返回给前端 / 编排的 gate 节点
工程师 UI：展示 Draft + 证据链（evidence_refs 可点开回溯图节点 / 诊断）
        │
        ├─ 驳回 -> 草稿归档（agent_draft 表），不落 MES 业务库
        │
        └─ 确认 -> 前端调 MES 正式 API（草稿服务不参与）：
                  │  返工单 -> 返工上下文"人工决策生成工单"应用服务
                  │  8D    -> 质量上下文 8D 发布应用服务
                  │  SOP   -> 工艺管理上下文 SOP 发布应用服务
                  │  处置卡 / 隔离集卡 -> 对应上下文应用服务（钢网归还/领用、隔离下达）
                  ▼
          聚合根不变式校验 + 事务发件箱 -> 落库 + 发布领域事件
                  │  返工单 -> brework.order.released
                  ▼
          过点执行上下文消费 brework.order.released 做批量再入校验（INV-07）
```

- 草稿能力止步于"产出 Draft"；下达是前端 + MES 正式应用服务的事。
- 落库过聚合根不变式 + 事务发件箱，与人工填单完全一致——Agent 只是"帮人填草稿"，不享受任何写捷径。

### 4.5 主动触发（SOP 升版草拟）

```python
# 编排/事件路由命中 drafting 能力
await runtime.invoke(
    "drafting",
    {"draft_kind": "SOP", "trigger": {"route_id": ..., "version": ...}},
    tenant=TenantContext.from_event(event),
)
```

- 仅订阅只读事件（`process.route.activated`），不消费写命令。
- 主动草拟的 SOP 推给工艺工程师，仍需人确认下达——不自动落库。

---

## 5. 兜底与可观测（通用底座上的能力特有项）

- **特有指标**：`agent_draft_total{draft_kind,status}` / `agent_draft_latency_seconds` / `agent_draft_low_confidence_total` / `agent_draft_adoption_total` / `agent_draft_rejected_total` / `agent_draft_trigger_total`。
- **证据链**：`Draft.evidence_refs` + `subgraph_ref` 可回溯图节点与诊断步骤。
- **特有兜底**：`confidence < 0.5` 或 `subgraph_ref` 回查为空 → `needs_review`，不硬凑；`requires_confirmation` 恒真；Pydantic 校验失败重试，重试仍失败转人工。

---

## 6. 实现步骤

1. 落 `DraftService` + `DraftBuilder` 策略基类 + 返工单 builder，打通"诊断 → 返工单草稿"闭环。
2. 接 `fetch_subgraph_nodes`（图服务需提供只读回查端点）与 `search_docs`。
3. 实现 8D builder（历史 8D 检索 + 5M1E 证据聚类）与 SOP builder（工艺 diff + 现有 SOP）。
4. 实现处置卡 / 隔离集卡 builder，供编排的非确定节点统一产出。
5. 落草稿存档表 + 工程师 UI（草稿 + 证据链 + 确认/驳回）。
6. 对接各上下文"人工确认后落库"的正式应用服务，验证 confirmation gate 走正常写路径。
7. 接指标与评测集（每个草稿类型的字段准确率 + 采纳率）。

---

## 7. 约束落地检查清单

- [ ] `CapabilitySpec(writes_allowed=False)`；草稿能力**不持有任何写 HTTP client**，启动断言扫描方法名通过。
- [ ] `Draft.requires_confirmation` 恒 `True`；草稿只落 `agent_draft` 存档表。
- [ ] 下达动作由前端调 MES 正式 API，草稿能力不参与写路径；落库过聚合根不变式 + 事务发件箱。
- [ ] 不调 `query_traceability_graph`（开放图检索归诊断能力），只按 `subgraph_ref` 回查。
- [ ] `Draft.version/kind/ref_id` 透传自 `DiagnosisReport`，不自行指定。
- [ ] 返工单字段对齐 `BatchReworkOrder`：`source_work_order_id` / `affected_sn_list` / `reentry_point` / `rework_route_ref`。
- [ ] `tenant` 透传到图回查 / 文档检索全程。
- [ ] 证据缺失或置信度 `< 0.5` → `needs_review=True`，不进确认下达流程。
- [ ] 结构化输出经 Pydantic 校验，不符合 schema 重试，重试仍失败转人工。
- [ ] 编排调用时 `parent_session_id` 落库，跨能力同 trace。
- [ ] 主动触发只订阅只读事件；主动草拟仍需人确认。

---

## 8. 待判断事项

1. **返工路线 `ReworkRoute` 的版本规则**：返工走独立路线（非正常工艺版本），是否有版本生命周期？草拟时如何选 `rework_route_ref`？需工艺管理上下文明确。
2. **8D 模板归属**：字段模板由质量上下文定义还是自由生成？影响 `EightDDraftBuilder` 的 prompt 结构。
3. **审批人角色矩阵**：返工单 / 8D / SOP / 处置卡 / 隔离集卡各自确认人是谁（线长 / 工艺 / 质量 / 生产主管）？需与权限模型对齐。
4. **`subgraph_ref` 跨服务生命周期**：落在 rag-service，草稿能力回查——保留多久？是否随工单归档？
5. **草稿落库的应用服务契约**：各上下文"人工决策生成"的 REST 端点 / 命令名需逐个对齐，`payload` 要能映射到应用服务入参。
6. **图服务只读回查端点**：`/rag/trace/subgraph/{ref}` 需图服务侧补齐。

---

## 9. 面试防守 Q&A

**Q：草稿会不会直接落库？怎么保证不越界？**
A：草稿能力**不持有任何写 HTTP client**——ACL 全是 `fetch/query/search`，启动断言扫描方法名禁止写动词；`requires_confirmation` 恒 `True`，草稿只落存档表。下达由前端调 MES 正式 API，走返工 / 质量 / 工艺上下文的正常应用服务，过聚合根不变式 + 事务发件箱。最坏情况是"草稿没用上"，不会产生写副作用。

**Q：草稿和诊断是不是重复了？**
A：不重复，是接力。诊断做多步只读推理给"根因假设 + 证据链"；草稿做写意图生成给"返工单 / 8D / SOP / 卡片"。草稿消费 `DiagnosisReport + subgraph_ref`，按引用回查图节点提取字段，不重复诊断、不重查图。诊断解决"诊断得出"，草稿解决"草拟得出来"，写的闸门始终在人手里。

**Q：为什么草稿不自己调图检索？**
A：两个原因。一是证据已被诊断验证过版本与权限——诊断调图时版本由 `SNAPSHOT_OF_ROUTE` 结构性锁定、权限由租户过滤前置，草稿回查的是同一份已验证子图，不重复承担版本兜底责任。二是职责清晰：开放检索归诊断，草稿只按引用回查，避免重复检索的延迟。

**Q：为什么草稿用固定链而不是开放规划？**
A：步骤固定（取证据 → 检索 → 生成 → 校验），没有"下一步查什么"的开放性。差异在草稿类型，用策略分派表达最直接——新增草稿类型只加一个 builder，不改图、不改服务。过三问：输入半结构化、输出结构化、分支可穷举，所以不需要开放规划。

**Q：处置卡和隔离集卡为什么也算草稿？**
A：因为它们和返工单一样，都是"结构化意图 + 证据 + 等人确认"。编排图的非确定节点只负责用诊断能力把根因 / 隔离范围判清楚，产物统一交给草稿能力按模板格式化——编排层因此不需要为每类卡各写一套拼装逻辑，也不会出现"某类卡字段填错但没人发现"的隐性不一致。

**Q：工艺变更后自动草拟 SOP，会不会乱发？**
A：不会。SOP 草拟订阅 `process.route.activated` 只读事件，产出草稿推给工艺工程师，仍需人确认下达。主动触发的价值是"升版后及时提醒更新 SOP"，不是"自动改 SOP"。

---

## 10. 一句话定位

"草稿编写能力用固定链 + 策略分派把诊断结论变成可下达的处置草稿——按 `subgraph_ref` 回查图证据不重查图、五类草稿由一个 builder 一族表达、`requires_confirmation` 恒真且不持有任何写 client、下达走人确认 + 各上下文正常应用服务不旁路写——它和诊断共用同一个运行时、同一条 trace，区别只在于这张图是直线而不是循环。"
