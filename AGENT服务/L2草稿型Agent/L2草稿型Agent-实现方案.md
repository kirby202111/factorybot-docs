# L2 草稿型 Agent 实现方案（Python 技术栈）

> 本文是 [AGENT服务引入路线.md](../AGENT服务引入路线.md) §2.3 L2 草稿型 Agent 的落地展开，输出**技术栈、实现方案、实现步骤、包结构、关键代码骨架与约束落地**。
> **与 L1 的关系**：[L1诊断型Agent-实现方案.md](../L1诊断型Agent/L1诊断型Agent-实现方案.md) 是多步**只读推理**（给根因假设）；本文 L2 是**写意图草拟**（给返工单/8D/SOP 草稿）。L2 消费 L1 的诊断结果 + 图证据，产出 `intent + draft`，**不落库**，落库走人确认 + MES 正常应用服务。
> **与结合方案的关系**：本文落实 [GraphRAG与Agent结合-落地方案.md](../../RAG与Agent协同/GraphRAG与Agent结合-落地方案.md) §4.2/§4.3/§5.4 的 L2 契约，补全 L2 的内部实现。
> **口径纪律**：L2 全程**只读检索 + 草拟**，**永不直接落库**，绝不旁路任何上下文的写路径（[领域总览.md](../../领域模型/领域总览.md) §5.3）。L2 输出的是 **intent + draft**，最终下达由工程师在正式界面确认——MES 对误写零容忍（错发一张返工单 = 批量报废），写动作的闸门 100% 在人手里。

---

## 1. 设计目标与边界

### 1.1 目标

把 L1 诊断结果（根因假设 + 证据链）升级成**可执行处置的草稿**：Agent 自动拉追溯证据 + 历史同类文档，草拟返工单 / 8D 报告 / SOP，工程师改完在正式界面下达。

典型场景："L1 诊断 SN-001 这批焊接不良要返工" -> L2 自动：

1. 按 L1 传来的 `subgraph_ref` 回查图节点，提取 `source_work_order_id`、`affected_sn_list`、`reentry_point`
2. 调文档型 RAG（路线 B）检索历史同类 8D / 现有 SOP
3. LLM 综合成结构化草稿（`intent + payload + evidence_refs`）
4. 工程师在返工上下文正式界面审核确认 -> 走返工上下文应用服务落库

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **只检索 + 草拟，不落库** | L2 产出 `intent + draft`，`requires_confirmation=True` 恒成立 | `DraftService` 只产出 `Draft`；L2 服务**不持有任何写 HTTP client**，`infrastructure/acl/` 全是只读 client |
| **不旁路应用服务写** | 落库走返工/工单/质量上下文的正常应用服务，过聚合根不变式 + 事务发件箱 | confirmation gate 的下达动作由**前端**调 MES 正式 API，L2 服务完全不参与写路径（[AGENT 路线 §4](../AGENT服务引入路线.md)） |
| **不进过点主事务** | L2 草拟异步，与过点判定解耦 | L2 草拟秒级；过点 P99 ≤200ms 不受影响 |
| **版本一致性** | 草稿锁定的工艺版本透传自 L1 证据，不自行指定 | `Draft.route_version` 来自 `DiagnosisReport`；返工单引用返工工艺路线 `ReworkRoute`（🔴 版本规则待明确，§11） |
| **权限隔离** | 草拟前按车间/产线/角色过滤，证据回查带租户上下文 | `DraftRequest.tenant` 透传到图回查 / 文档检索 / 草稿落库全程 |
| **可观测兜底** | 每份草稿带证据链 + 置信度，低置信度不推荐下达 | `Draft.evidence_refs` 引用 `subgraph_ref`/`node_id`；`confidence < 0.5` 标 `needs_review` |
| **不直查图** | L2 只按 L1 传来的 `subgraph_ref` 回查图节点（只读），不独立调 `query_traceability_graph` | L2 的图回查是 `fetch_subgraph_nodes(subgraph_ref)`，不是开放图检索 |

### 1.3 与 L1、Java 技术栈的关系

- **与 L1**：L1 是"查 + 诊断"（只读推理），L2 是"诊断 -> 草拟"（写意图生成）。L2 **消费** L1 的 `DiagnosisReport + subgraph_ref`，不重复诊断。L1 和 L2 可同属 `agent-service`（不同模块），也可拆分；MVP 建议同服务不同模块，复用 LLM 抽象与可观测基础设施。
- **与 Java**：L2 用 Python，**不替换** MES 三大服务的 Java/Spring 栈。L2 只通过 httpx 调只读 REST（图回查 / 文档检索 / 工艺版本查询），**不调任何写 API**。跨语言的物理边界强化了"L2 无法旁路写"——Python 侧连写 client 都没有。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 与 L1 同栈，复用 LLM 抽象 / Pydantic / 可观测 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI，与 L1 一致 |
| 草拟编排 | **DraftService（async 函数编排 + 策略模式）** | L2 步骤固定（取证据 -> 检索文档 -> 综合），无需 LangGraph 开放 ReAct；策略模式按草稿类型分派（SRP） |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 与 L1 一致，`with_structured_output(Draft)` 强制结构化草稿 |
| 数据校验 | **Pydantic v2** | 草稿 DTO / 证据模型 schema 即类型 |
| HTTP 客户端 | **httpx**（异步） | 调图服务（回查子图）、文档型 RAG、工艺管理只读 REST |
| 消息 | **aiokafka** | 订阅 `ProcessRouteActivated` 触发 SOP 草拟（主动场景） |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 草稿存档、草拟 trace |
| 缓存 | **redis-py (async)** | 草稿短期缓存、同证据重复草拟去重 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 串联、指标告警 |
| 配置 | pydantic-settings | 环境变量 / 配置文件统一管理 |
| 部署 | 与 L1 同服务 `agent-service`（MVP）或独立 `draft-service` | MVP 同服务不同模块，复用基础设施 |

### 2.2 为什么 L2 用策略模式 + async 编排，而非 LangGraph ReAct

- L1 用 LangGraph 是因为**多步规划**——模型需根据上一步工具返回决定下一步，要对每步做权限拦截、trace、`recursion_limit`（[L1 §2.2](../L1诊断型Agent/L1诊断型Agent-实现方案.md)）。
- L2 的步骤是**固定的**：取证据（`subgraph_ref` 回查）-> 检索文档（路线 B）-> LLM 综合成草稿。没有"模型自主决定下一步查什么"的开放性——用 async 函数编排更简洁、更可控、更易测试。
- 不同草稿类型（返工单/8D/SOP）的差异用**策略模式**（每个 `DraftBuilder` 一个类）表达，而非用 LangGraph 状态图分支——新增草稿类型只需加一个 `DraftBuilder`（OCP）。
- 若未来某草稿类型变复杂（如 8D 需多段递进生成），可**局部**引入 LangGraph，不影响整体编排。

### 2.3 为什么 L2 服务不持有任何写 client

- MES 对误写零容忍。L2 的写风险必须从代码层面杜绝，不靠口头约束。
- `infrastructure/acl/` 只注册只读 client：`RagAclClient.fetch_subgraph_nodes`、`DocRagAclClient.search_docs`、`ProcessManagementAclClient.fetch_route_version`——全是 `fetch/query/search`，无 `create/update/delete`。
- confirmation gate 的"下达"动作由**前端**调 MES 正式 API（返工上下文/工单管理/质量上下文），L2 服务**完全不参与写路径**。这样 L2 最坏情况是"草稿没用上"，不会产生任何写副作用——与 L1"最坏是没诊断出来"同理（[L1 §2.3](../L1诊断型Agent/L1诊断型Agent-实现方案.md)）。
- 启动断言 `NoWriteClientGate` 扫描所有 ACL client 方法名，禁止 `create/update/delete/submit/release` 等写动词——红线靠代码兜底。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────────┐
│ agent-service / draft 模块（L2）                                       │
│                                                                        │
│  ┌──────────────┐    ┌──────────────────────────────────────────┐    │
│  │ FastAPI      │───▶│ DraftService                              │    │
│  │ /agent/draft │    │  1. 按 draft_kind 分派到 DraftBuilder     │    │
│  └──────────────┘    │  2. 取证据 + 检索文档 + LLM 综合           │    │
│                      │  3. 产出 Draft（requires_confirmation=True）│    │
│                      └──────────────┬───────────────────────────┘    │
│                                     │                                  │
│              ┌──────────────────────┼──────────────────────┐          │
│              ▼                      ▼                      ▼          │
│  ┌──────────────────┐  ┌─────────────────────┐  ┌──────────────────┐ │
│  │ RagAclClient     │  │ DocRagAclClient     │  │ ProcessMgmtAcl   │ │
│  │ fetch_subgraph_  │  │ search_docs         │  │ fetch_route_     │ │
│  │ nodes(只读)      │  │ (只读,路线B)         │  │ version(只读)    │ │
│  └────────┬─────────┘  └──────────┬──────────┘  └────────┬─────────┘ │
└───────────┼───────────────────────┼─────────────────────┼────────────┘
            │                       │                     │
            ▼                       ▼                     ▼
  ┌──────────────────┐    ┌──────────────────┐  ┌────────────────────┐
  │ rag-service      │    │ 文档型 RAG(路线B) │  │ 制造资源服务(Java)  │
  │ /rag/trace/sub-  │    │ search_docs      │  │ 工艺版本只读 REST   │
  │ graph/{ref}(只读)│    │                  │  │                    │
  └──────────────────┘    └──────────────────┘  └────────────────────┘

  ┌──────────────────────────────────────────────────────────────────┐
  │ confirmation gate（L2 服务之外）：                                  │
  │   工程师 UI 展示 Draft + 证据链 -> 驳回(归档) / 确认(调 MES 正式 API) │
  │   确认后：返工上下文 / 工单管理 / 质量上下文 正常应用服务落库        │
  │   （聚合根不变式 + 事务发件箱）—— L2 服务不参与                     │
  └──────────────────────────────────────────────────────────────────┘

  主动触发（SOP 草拟）：
    aiokafka 订阅 ProcessRouteActivated -> DraftService.draft(SOP)
```

### 3.1 关键设计决策

- **草稿即证据投影**：L2 草稿的关键字段（`source_work_order_id`、`affected_sn_list`、`reentry_point`）都来自图的追溯子图——图是 L2 草稿的"证据基础"。L2 不自己调图开放检索，靠 L1 传来的 `subgraph_ref` 回查，保证证据已被 L1 验证过版本/权限。
- **策略模式分派**：`DraftBuilder` 是抽象基类，`ReworkOrderDraftBuilder` / `EightDDraftBuilder` / `SopDraftBuilder` 各自实现。新增草稿类型只加一个 builder，不改 `DraftService`（OCP/SRP）。
- **confirmation gate 在 L2 之外**：L2 服务止步于"产出 Draft"。下达动作由前端调 MES 正式 API，L2 服务不持有写 client——这是 L2 写风险归零的关键。
- **复用 L1 基础设施**：LLM 抽象、OTel trace、Pydantic 校验、租户上下文都与 L1 同构，MVP 同服务不同模块复用。

---

## 4. 草稿类型与只读工具

### 4.1 三种草稿类型

| 草稿类型 | draft_kind | 落库上下文 | 关键字段 | 证据来源 |
|---------|-----------|-----------|---------|---------|
| 返工单 | `REWORK_ORDER` | 返工上下文（`brework.*`） | `source_work_order_id`、`affected_sn_list`、`reentry_point`、`rework_route_ref` | 图 `subgraph_ref`（`WipUnit`/`WorkOrder`/`RouteStep`） |
| 8D 报告 | `EIGHT_D` | 质量上下文（🔴 待定义） | 问题描述/根因/containment/纠正措施 | 图 5M1E 证据 + 文档型 RAG 历史 8D |
| SOP | `SOP` | 工艺管理上下文（🔴 待定义） | 工序步骤/参数/版本 | `ProcessRouteActivated` 新版本 + 文档型 RAG 现有 SOP |

> 返工单草拟对齐 [返工上下文.md](../../领域模型/生产执行服务/事件风暴/返工上下文.md)：`BatchReworkOrder` 覆盖一批 SN，关联源工单 + SN 清单 + **返工工艺路线 `ReworkRoute`**（独立于正常 `RouteVersion`）+ 批量再入点 `reentry_point`。下达走返工上下文"人工决策生成工单"通道，发布 `brework.order.released`。

### 4.2 只读工具集（L2 的全部外部依赖）

| 工具 | 调用方 | 用途 | 写动词 |
|------|--------|------|--------|
| `fetch_subgraph_nodes(subgraph_ref)` | `RagAclClient` | 按 L1 传来的 `subgraph_ref` 回查图节点，提取证据字段 | 无（只读） |
| `search_docs(query, route_version_filter)` | `DocRagAclClient` | 文档型 RAG 检索历史 8D / 现有 SOP | 无（只读） |
| `fetch_route_version(route_id, route_version)` | `ProcessManagementAclClient` | 锁定返工/SOP 草稿的工艺版本 | 无（只读） |

> L2 **没有** `query_traceability_graph`（开放图检索归 L1）、**没有任何** `create/update/delete` client。`NoWriteClientGate` 启动断言扫描所有 ACL client 方法名，禁止写动词。

### 4.3 权限与版本

- **权限**：`DraftRequest.tenant` 透传到 `fetch_subgraph_nodes` / `search_docs` / `fetch_route_version` 的 header（`X-Tenant-*`），下游服务前置过滤。L2 服务本身不做权限拦截（只读检索，权限由下游兜）。
- **版本**：`Draft.route_version` 来自 L1 的 `DiagnosisReport`（透传自图 `SNAPSHOT_OF_ROUTE`），L2 不自行指定。返工单引用 `ReworkRoute`（返工专用路线，🔴 版本规则待工艺管理上下文明确，§11）。

---

## 5. 实现方案

### 5.1 草拟编排（DraftService）

```text
DraftRequest(diagnosis_report, draft_kind, tenant)
        │
        ▼
DraftService.draft()
  ├─ 1. 按 draft_kind 分派到对应 DraftBuilder（策略模式）
  ├─ 2. builder.build()：
  │     ├─ fetch_subgraph_nodes(subgraph_ref) -> 证据节点
  │     ├─ search_docs(...) -> 文档知识（8D 历史 / 现有 SOP）
  │     ├─ LLM.with_structured_output(Draft) -> 综合成草稿
  │     └─ Draft.requires_confirmation = True（恒成立）
  └─ 3. 落草稿存档表 + 返回 Draft（不落 MES 业务库）
```

### 5.2 草稿生成器（策略模式）

```python
class DraftBuilder(Protocol):
    """一个草稿类型一个 builder。"""
    draft_kind: DraftKind
    async def build(
        self, report: DiagnosisReport, tenant: TenantContext
    ) -> Draft: ...
```

- 每个 builder 只负责一种草稿的"取证据 + 检索 + 综合"——单一职责（SRP）。
- 新增草稿类型（如"维修单草拟"）只需加一个 builder，不改 `DraftService`（OCP）。

### 5.3 输出结构（Draft）

```python
class DraftKind(str, Enum):
    REWORK_ORDER = "REWORK_ORDER"
    EIGHT_D = "EIGHT_D"
    SOP = "SOP"

class Draft(BaseModel):
    draft_kind: DraftKind
    intent: str                        # "对 WO-2026-0707-001 的 12 件 SN 执行焊接返工，再入点 ST-05"
    payload: dict                      # 草稿结构化内容（返工单字段 / 8D 段落 / SOP 步骤）
    evidence_refs: list[str]           # ["subgraph_ref=...", "node_id=CheckpointRecord:..."]
    route_version: str | None = None   # 草稿锁定的工艺版本（透传自 L1）
    confidence: float                  # 0.0 ~ 1.0
    requires_confirmation: bool = True # L2 恒为 True
    needs_review: bool = False         # confidence < 0.5 时 True
    disclaimer: str = "本草稿为辅助草拟，下达需工程师在正式界面确认"
```

- `intent` 让工程师一眼看懂草稿要干什么，再看 `payload` 细节。
- `requires_confirmation` 恒 `True`——L2 从不产出"可直接落库"的草稿。
- `evidence_refs` 让草稿每个字段可回溯到图节点 / L1 诊断证据。

### 5.4 ACL 防腐层（只读）

```python
class RagAclClient:
    """图服务只读 ACL：按 subgraph_ref 回查图节点。不开放图检索。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_subgraph_nodes(
        self, subgraph_ref: str, tenant: TenantContext
    ) -> list[TraceNodeView]:
        resp = await self._http.get(
            f"/rag/trace/subgraph/{subgraph_ref}",   # 图服务只读端点
            headers=tenant.headers(),
            timeout=2.0,
        )
        resp.raise_for_status()
        return [TraceNodeMapper.to_view(n) for n in resp.json()["nodes"]]
```

- 外部 DTO 不进 L2 核心，只暴露 `TraceNodeView`——防腐层核心职责（CLAUDE.md ACL 约束）。
- 方法名 `fetch_subgraph_nodes`（只读动词），`NoWriteClientGate` 启动断言放行。

### 5.5 confirmation gate 落库流程

```text
L2 产出 Draft（requires_confirmation=True）
        │
        ▼  返回给前端
工程师 UI：展示 Draft + 证据链（evidence_refs 可点开回溯图节点/L1 诊断）
        │
        ├─ 驳回 -> 草稿归档（draft_archive 表），不落 MES 业务库
        │
        └─ 确认 -> 前端调 MES 正式 API（L2 服务不参与）：
                  │  返工单 -> 返工上下文"人工决策生成工单"应用服务（🔴 端点待对齐）
                  │  8D    -> 质量上下文 8D 发布应用服务（🔴 待定义）
                  │  SOP   -> 工艺管理上下文 SOP 发布应用服务（🔴 待定义）
                  ▼
          聚合根不变式校验 + 事务发件箱 -> 落库 + 发布领域事件
                  │  返工单 -> brework.order.released
                  ▼
          过点执行上下文消费 brework.order.released 做批量再入校验（INV-07）
```

- L2 服务止步于"产出 Draft"。下达是前端 + MES 正式应用服务的事，L2 不持有写 client、不调写 API。
- 落库过聚合根不变式 + 事务发件箱，与正常人工下达完全一致——Agent 只是"帮人填草稿"，不享受任何写捷径。

### 5.6 与 L1、RAG 的衔接

- **L1 -> L2**：L1 诊断产出的 `DiagnosisReport` 带 `subgraph_ref`（[结合方案 §4.2](../../RAG与Agent协同/GraphRAG与Agent结合-落地方案.md)）。L2 入参 `DraftRequest.diagnosis_report` 即它。L1->L2 的触发方式（自动续接 vs 人工发起）🔴 待定（§11）。
- **L2 -> 图**：只按 `subgraph_ref` 回查（`fetch_subgraph_nodes`），不调 `query_traceability_graph`。图的 `/rag/trace/subgraph/{ref}` 是 L2 专用的只读回查端点（图服务需补，🔴）。
- **L2 -> 文档型 RAG**：`search_docs(query, route_version_filter)` 调路线 B，版本过滤对齐 `route_version`（[结合方案 §4.4](../../RAG与Agent协同/GraphRAG与Agent结合-落地方案.md)）。

---

## 6. 推荐包结构（Python src layout，L2 模块）

```text
agent_service/
  app/
    api/
      draft_router.py          # /agent/draft
    application/
      draft_service.py         # 编排：分派 + 取证据 + 综合
      builders/                # 草稿生成器（策略模式）
        rework_order.py        # 返工单草稿
        eight_d.py             # 8D 报告草稿
        sop.py                 # SOP 草稿
    domain/
      draft.py                 # Draft / DraftKind / DraftRequest
      evidence.py              # TraceNodeView / 证据模型
      tenant.py                # TenantContext（与 L1 共用）
      gate.py                  # NoWriteClientGate 启动断言
    infrastructure/
      acl/                     # 只读 ACL client（无写 client）
        rag.py                 # fetch_subgraph_nodes
        doc_rag.py             # search_docs
        process_management.py  # fetch_route_version
      ai/                      # LLM 客户端（与 L1 共用 llm_factory）
      kafka/                   # 订阅 ProcessRouteActivated（SOP 主动触发）
        listeners.py
      persistence/
        draft_repo.py          # 草稿存档
        draft_trace_repo.py    # 草拟 trace
      redis_/                  # 草稿缓存
      obs/                     # OTel / prometheus（与 L1 共用）
    config.py
    main.py                    # lifespan：NoWriteClientGate 启动断言
  tests/
```

- `application/builders/` 是策略模式落地，每个草稿类型一个文件，符合 SRP——新增草稿类型只加文件不改 `DraftService`。
- `infrastructure/acl/` **只有只读 client**，`NoWriteClientGate` 启动断言扫描方法名禁止写动词——从代码层面杜绝旁路写。
- 与 L1 共用 `tenant.py` / `ai/llm_factory` / `obs/`，MVP 同服务不同模块。

---

## 7. 关键代码骨架

### 7.1 草稿服务编排

```python
# app/application/draft_service.py
class DraftService:
    def __init__(
        self,
        builders: dict[DraftKind, DraftBuilder],
        draft_repo: DraftRepo,
        trace_repo: DraftTraceRepo,
        metrics: MetricsCollector,
    ) -> None:
        self._builders = builders
        self._draft_repo = draft_repo
        self._trace_repo = trace_repo
        self._metrics = metrics

    async def draft(self, req: DraftRequest) -> Draft:
        builder = self._builders.get(req.draft_kind)
        if builder is None:
            raise ValueError(f"不支持的草稿类型: {req.draft_kind}")
        t0 = time.perf_counter()
        try:
            draft = await builder.build(req.diagnosis_report, req.tenant)
            draft.requires_confirmation = True           # L2 恒成立
            if draft.confidence < 0.5:
                draft.needs_review = True
            await self._draft_repo.archive(draft)        # 草稿存档（不落 MES 业务库）
            await self._trace_repo.save_ok(req.draft_kind, draft, t0)
            self._metrics.draft_total.inc(req.draft_kind, "ok")
            return draft
        except Exception as e:
            await self._trace_repo.save_error(req.draft_kind, e)
            self._metrics.draft_total.inc(req.draft_kind, "error")
            raise
```

### 7.2 返工单草稿生成器

```python
# app/application/builders/rework_order.py
class ReworkOrderDraftBuilder:
    """返工单草稿：按 subgraph_ref 提取源工单/SN 清单/再入点，综合成 BatchReworkOrder 草稿。"""

    draft_kind = DraftKind.REWORK_ORDER

    def __init__(self, rag: RagAclClient, process: ProcessManagementAclClient,
                 llm: BaseChatModel) -> None:
        self._rag = rag; self._process = process; self._llm = llm

    async def build(self, report: DiagnosisReport, tenant: TenantContext) -> Draft:
        # 1. 按 subgraph_ref 回查图节点（只读，不开放图检索）
        nodes = await self._rag.fetch_subgraph_nodes(report.subgraph_ref, tenant)
        source_wo = self._extract(nodes, "WorkOrder", "work_order_id")
        sn_list = self._extract_sn_list(nodes)              # 受影响 SN 清单
        reentry_point = self._infer_reentry_point(report)   # 从 L1 假设推再入点
        route_version = self._extract_route_version(report) # 透传自 L1 证据
        # 2. 返工工艺路线引用（ReworkRoute，独立于正常 RouteVersion，🔴 版本规则待明确）
        rework_route_ref = await self._resolve_rework_route(source_wo, route_version, tenant)
        # 3. LLM 综合成草稿
        draft = await self._llm.with_structured_output(Draft).ainvoke(
            self._build_prompt(report, source_wo, sn_list, reentry_point, rework_route_ref)
        )
        draft.draft_kind = self.draft_kind
        draft.route_version = route_version
        draft.evidence_refs = [f"subgraph_ref={report.subgraph_ref}"] + report.evidence_refs
        return draft

    def _build_prompt(self, report, wo, sn_list, reentry, route_ref) -> str:
        return (
            "你是 MES 返工单草拟助手。基于 L1 诊断 + 图证据，草拟 BatchReworkOrder。\n"
            "约束：\n"
            "1. 只能基于提供的证据，不得编造 SN 或工单。\n"
            "2. intent 一句话说明要返工什么、再入点在哪。\n"
            "3. payload 含 source_work_order_id / affected_sn_list / reentry_point / rework_route_ref。\n"
            "4. 输出严格遵循 Draft JSON 结构，requires_confirmation 必须为 true。\n"
            f"L1 诊断：{report.summary}\n"
            f"证据：源工单={wo}, SN 清单={sn_list}, 再入点={reentry}, 返工路线={route_ref}"
        )
```

### 7.3 8D 草稿生成器

```python
# app/application/builders/eight_d.py
class EightDDraftBuilder:
    """8D 报告草稿：图 5M1E 证据 + 文档型 RAG 历史 8D。"""

    draft_kind = DraftKind.EIGHT_D

    def __init__(self, rag: RagAclClient, doc_rag: DocRagAclClient,
                 llm: BaseChatModel) -> None:
        self._rag = rag; self._doc_rag = doc_rag; self._llm = llm

    async def build(self, report: DiagnosisReport, tenant: TenantContext) -> Draft:
        nodes = await self._rag.fetch_subgraph_nodes(report.subgraph_ref, tenant)
        five_m1e = self._cluster_5m1e(nodes)               # 按 5M1E 聚类证据
        route_version = self._extract_route_version(report)
        # 检索历史同类 8D（路线 B，带版本过滤）
        history = await self._doc_rag.search_docs(
            query=report.summary, route_version_filter=route_version, tenant=tenant
        )
        draft = await self._llm.with_structured_output(Draft).ainvoke(
            self._build_prompt(report, five_m1e, history)
        )
        draft.draft_kind = self.draft_kind
        draft.route_version = route_version
        return draft
```

### 7.4 SOP 草稿生成器（主动触发）

```python
# app/application/builders/sop.py
class SopDraftBuilder:
    """SOP 草稿：订阅 ProcessRouteActivated，基于新版本 + 现有 SOP 草拟新 SOP。"""

    draft_kind = DraftKind.SOP

    def __init__(self, doc_rag: DocRagAclClient, llm: BaseChatModel) -> None:
        self._doc_rag = doc_rag; self._llm = llm

    async def build_from_route_activated(
        self, route_id: str, route_version: str, tenant: TenantContext
    ) -> Draft:
        # 检索现有 SOP（路线 B，按 route_version 过滤旧版本）
        existing = await self._doc_rag.search_docs(
            query=f"SOP route={route_id}", route_version_filter=None, tenant=tenant
        )
        draft = await self._llm.with_structured_output(Draft).ainvoke(
            self._build_prompt(route_id, route_version, existing)
        )
        draft.draft_kind = self.draft_kind
        draft.route_version = route_version                # 锁定新版本
        draft.intent = f"基于工艺升版 {route_id} v{route_version} 草拟新 SOP"
        return draft
```

### 7.5 只读 ACL 与启动断言

```python
# app/domain/gate.py
WRITE_VERBS = ("create", "update", "delete", "submit", "release", "issue", "save")

class NoWriteClientGate(Exception):
    """启动时发现 L2 持有写 client，拒绝启动。"""

def assert_no_write_clients(acl_clients: list[object]) -> None:
    """扫描所有 ACL client 方法名，禁止写动词。"""
    for client in acl_clients:
        for attr in dir(client):
            if callable(getattr(client, attr)) and not attr.startswith("_"):
                if any(attr.lower().startswith(v) for v in WRITE_VERBS):
                    raise NoWriteClientGate(
                        f"L2 禁止持有写 client 方法: {client.__class__.__name__}.{attr}"
                    )

# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    assert_no_write_clients(app.state.acl_clients)   # 启动断言：L2 无写 client
    yield
```

### 7.6 FastAPI 入口

```python
# app/api/draft_router.py
router = APIRouter()

@router.post("/agent/draft", response_model=Draft)
async def draft(
    req: DraftRequest,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: DraftService = Depends(get_draft_service),
) -> Draft:
    """L2 草拟处置。消费 L1 诊断 + subgraph_ref，不重查图，不落库。"""
    return await svc.draft(req)

@router.get("/agent/draft/{draft_id}/evidence")
async def evidence(
    draft_id: str,
    tenant: TenantContext = Depends(tenant_from_token),
    rag: RagAclClient = Depends(get_rag_client),
) -> list[TraceNodeView]:
    """工程师 UI 回溯草稿证据：按 evidence_refs 回查图节点（只读）。"""
    ...
```

### 7.7 主动触发（SOP 草拟，订阅领域事件）

```python
# app/infrastructure/kafka/listeners.py
class ProcessRouteActivatedListener:
    """订阅工艺升版事件，主动草拟新 SOP。不消费任何写命令。"""

    def __init__(self, svc: DraftService, builder: SopDraftBuilder) -> None:
        self._svc = svc; self._builder = builder

    async def run(self, consumer: AIOKafkaConsumer) -> None:
        async for msg in consumer:
            event = ProcessRouteActivated.model_validate_json(msg.value)
            tenant = TenantContext.from_event(event)
            draft = await self._builder.build_from_route_activated(
                event.route_id, event.route_version, tenant
            )
            await self._svc.archive(draft)   # 推送给工艺工程师，不自动下达
```

- 仅订阅只读事件（`process.route.lifecycle`），不消费写命令。
- 主动草拟的 SOP 推送给工艺工程师，仍需人确认下达——不自动落库。

---

## 8. 可观测性与兜底

### 8.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `l2_draft_total` | 草稿生成数（按 draft_kind / status label） |
| `l2_draft_latency_seconds` | 草拟延迟（取证据 + 检索 + LLM 综合，Histogram） |
| `l2_draft_low_confidence_total` | `confidence < 0.5` 草稿数 |
| `l2_draft_rejected_total` | 工程师驳回草稿数（confirmation gate 拒绝） |
| `l2_acl_error_total` | 只读 ACL 调用失败次数（按 client label） |
| `l2_active_trigger_total` | 主动触发草拟次数（SOP） |

### 8.2 trace 串联

- 每份草稿一个 `trace_id`，OpenTelemetry 在 `DraftBuilder`、ACL client、LLM 调用都注入 span，透传到下游图服务 / 文档型 RAG / 制造资源服务（`traceparent` header）。
- `Draft.evidence_refs` + `subgraph_ref` 让工程师从草稿回溯到图节点、再到 L1 诊断、再到原始领域事件——证据链可点开回溯。

### 8.3 兜底

- **置信度兜底**：`confidence < 0.5` -> `needs_review=True`，草稿标记"需复核"才推工程师，不直接进 confirmation gate 下达流程。
- **证据缺失兜底**：`subgraph_ref` 回查为空（图投影滞后）-> 草稿标 `needs_review`，`intent` 注明"证据不完整，请人工补齐"，不硬凑草稿。
- **LLM 输出兜底**：`Draft` 经 Pydantic 校验，不符合 schema 判失败重试；重试仍失败返回"草拟失败转人工"，不硬答。
- **confirmation gate 兜底**：草稿 `requires_confirmation` 恒 `True`，前端无法绕过确认直接下达；L2 服务不持有写 client，即使被绕过也无法落库。

---

## 9. 实现步骤

### 阶段一：骨架与返工单草拟（2 周）
1. 搭 L2 模块骨架（`draft_service` / `builders/` / `acl/`），对齐 §6 包结构。
2. 实现 `ReworkOrderDraftBuilder`（§7.2），验证 L1 诊断 -> 返工单草稿闭环。
3. 实现 `NoWriteClientGate` 启动断言（§7.5），验证 L2 无写 client。
4. 实现图服务只读端点 `/rag/trace/subgraph/{ref}`（🔴 图服务侧待补）。

### 阶段二：8D / SOP 草拟 + 文档型 RAG 协同（2 周）
5. 对接路线 B `search_docs`，实现 `EightDDraftBuilder`（§7.3）。
6. 实现 `SopDraftBuilder` + 订阅 `ProcessRouteActivated` 主动触发（§7.4/§7.7）。
7. 验证版本透传：L1 `route_version` -> `Draft.route_version` -> 文档检索版本过滤。

### 阶段三：confirmation gate + 存档（2 周）
8. 实现草稿存档表 + 工程师 UI 展示草稿 + 证据链回溯。
9. 对接返工上下文"人工决策生成工单"应用服务（🔴 端点待对齐），验证 confirmation gate 落库走正常应用服务。
10. 接 OTel + prometheus 指标（§8.1）。

### 阶段四：评测与加固（2 周）
11. 沉淀评测集：每个草稿类型含 L1 诊断 + 预期草稿字段 + 预期证据引用。
12. 灰度一条产线（返工单草拟），收集工程师反馈，按反馈调整 prompt 与 builder。

---

## 10. 约束落地检查清单

- [ ] L2 服务**不持有任何写 HTTP client**，`infrastructure/acl/` 全是只读 client，`NoWriteClientGate` 启动断言生效。
- [ ] `Draft.requires_confirmation` 恒为 `True`；草稿只落存档表，不落 MES 业务库。
- [ ] confirmation gate 下达动作由前端调 MES 正式 API，L2 服务不参与写路径；落库过聚合根不变式 + 事务发件箱。
- [ ] L2 不调 `query_traceability_graph`（开放图检索归 L1），只按 `subgraph_ref` 回查图节点（`fetch_subgraph_nodes`）。
- [ ] `Draft.route_version` 透传自 L1 `DiagnosisReport`，L2 不自行指定版本；返工单引用 `ReworkRoute`（🔴 版本规则待明确）。
- [ ] 返工单草稿字段对齐 `BatchReworkOrder`：`source_work_order_id` / `affected_sn_list` / `reentry_point` / `rework_route_ref`。
- [ ] `DraftRequest.tenant` 透传到图回查 / 文档检索 / 草稿落库全程。
- [ ] `confidence < 0.5` -> `needs_review=True`，不进 confirmation gate 下达流程。
- [ ] 证据缺失（`subgraph_ref` 回查为空）-> 草稿标 `needs_review`，不硬凑。
- [ ] LLM 输出经 Pydantic `Draft` 校验，不符合 schema 判失败重试，重试仍失败转人工。
- [ ] 主动触发（SOP）只订阅 `process.route.lifecycle` 只读事件，不消费写命令；主动草拟仍需人确认下达。
- [ ] 每份草稿落 `draft_trace`，`evidence_refs` + `subgraph_ref` 让证据链可点开回溯。
- [ ] 所有草稿带 disclaimer：辅助草拟，下达需工程师在正式界面确认。

---

## 11. 待判断事项（🔴 交用户 / SME）

1. **🔴 返工工艺路线 `ReworkRoute` 的版本规则**：返工走独立路线（非正常 `RouteVersion`），但 `ReworkRoute` 是否有版本生命周期？L2 草拟返工单时如何选择 `rework_route_ref`？需工艺管理上下文明确。
2. **🔴 8D 模板归属**：8D 报告的字段模板（问题描述/根因/containment/纠正措施）由质量上下文定义标准模板，还是 L2 自由生成？影响 `EightDDraftBuilder` 的 prompt 结构。
3. **🔴 confirmation gate 审批人角色矩阵**：返工单 / 8D / SOP 各自的确认审批人是谁（线长 / 工艺工程师 / 质量工程师 / 生产主管）？需与权限模型对齐。返工上下文 §1.1 提到"生产主管/质量主管/工程师通过终端决策"。
4. **🔴 L1->L2 触发方式**：L1 诊断完成后，L2 草拟是自动续接还是人工发起？自动续接省一步但可能草拟出不需要的处置；人工发起稳妥。
5. **🔴 `subgraph_ref` 跨服务生命周期**：`subgraph_ref` 落在 rag-service，L2 在 agent-service 回查——保留多久？是否随工单/草稿归档？跨服务引用的清理策略需定义。
6. **🔴 草稿落库的应用服务契约**：返工上下文"人工决策生成工单"的 REST 端点 / 命令名；质量上下文 8D 发布应用服务（8D 上下文尚未定义）；工艺管理 SOP 发布应用服务。L2 草稿 `payload` 要能映射到这些应用服务入参。
7. **🔴 图服务只读回查端点**：`/rag/trace/subgraph/{ref}` 在图方案 §9.8 未定义，需图服务侧补一个按 `subgraph_ref` 返回子图节点的只读端点。
8. **🔴 草稿保留与归档策略**：驳回的草稿保留多久？确认下达的草稿是否随工单归档？影响 `draft_archive` 表的清理策略。
9. **🔴 质量上下文批量异常事件**：返工上下文 §1.1 标注"质量上下文批量异常事件未定义"——L2 返工单草拟若由批量异常触发（而非 L1 诊断），需等该事件定义。MVP 走"L1 诊断 -> 人工发起返工单草拟"通道。

---

## 12. 面试防守 Q&A

**Q：L2 草稿会不会直接落库？怎么保证不越界？**
A：不会。L2 服务**不持有任何写 HTTP client**——`infrastructure/acl/` 全是只读（`fetch_subgraph_nodes` / `search_docs` / `fetch_route_version`），`NoWriteClientGate` 启动断言扫描方法名禁止写动词。`Draft.requires_confirmation` 恒 `True`，草稿只落存档表。下达动作由前端调 MES 正式 API，走返工/工单/质量上下文的正常应用服务，过聚合根不变式 + 事务发件箱。L2 最坏情况是"草稿没用上"，不会产生写副作用——这是 MES 写红线的安全落地形态。

**Q：L2 和 L1 什么关系？是不是重复了？**
A：不重复，是接力。L1 是多步只读推理，给"根因假设 + 证据链"；L2 是写意图草拟，给"返工单/8D/SOP 草稿"。L2 消费 L1 的 `DiagnosisReport + subgraph_ref`，按 `subgraph_ref` 回查图节点提取证据字段，不重复诊断、不重查图。L1 解决"问得到/诊断得出"，L2 解决"草拟得出来"，但写的闸门始终在人手里（[AGENT 路线 §2.3](../AGENT服务引入路线.md)）。

**Q：为什么 L2 不自己调图检索，要用 L1 传来的 subgraph_ref？**
A：两个原因。一是证据已被 L1 验证过版本/权限——L1 调图时版本由 `SNAPSHOT_OF_ROUTE` 结构性锁定、权限由 `tenant_scope WHERE` 前置过滤，L2 回查的是同一份已验证子图，不重复承担版本兜底责任。二是 L2 职责是"草拟"不是"检索"——开放图检索归 L1，L2 只按引用回查，职责清晰（SRP），也避免 L2 阶段重复检索的延迟。

**Q：返工单草稿的关键字段从哪来？**
A：从图的追溯子图。`source_work_order_id` 来自 `WipUnit.BELONGS_TO -> WorkOrder`，`affected_sn_list` 来自 `BatchReworkOrder` 的 SN 清单（或 `CONSUMED_BATCH` 反向扩展），`reentry_point` 从 L1 诊断的根因假设推。返工工艺路线 `ReworkRoute` 是返工专用路线（独立于正常 `RouteVersion`），从工艺管理上下文只读引用。这些都靠 `subgraph_ref` 回查图节点提取，L2 不编造。

**Q：为什么 L2 用策略模式 + async 编排，不用 LangGraph？**
A：L1 用 LangGraph 是因为多步开放规划（模型自主决定下一步查什么）。L2 步骤固定（取证据 -> 检索文档 -> 综合），没有开放性，用 async 函数编排更简洁可控。不同草稿类型的差异用策略模式（每个 `DraftBuilder` 一个类）表达，新增类型只加 builder 不改 `DraftService`（OCP）。若未来某草稿变复杂可局部引入 LangGraph，不影响整体。

**Q：工艺变更后自动草拟 SOP，会不会乱发？**
A：不会。SOP 草拟订阅 `ProcessRouteActivated` 只读事件，产出草稿推送给工艺工程师，仍需人确认下达——不自动落库。L2 不持有写 client，即使主动触发也止步于草稿。主动触发的价值是"工艺升版后及时提醒更新 SOP"，不是"自动改 SOP"。

**Q：上线了吗？**
A：这是设计阶段的引入规划，不是已落地。重点是三个架构判断：①L2 服务不持有写 client、`requires_confirmation` 恒成立，从代码层面把写风险归零；②L2 靠 `subgraph_ref` 回查图证据不重查图，职责清晰且复用 L1 已验证证据；③草拟走策略模式，下达走人确认 + 正常应用服务，Agent 只"帮人填草稿"不享受写捷径。落地依赖 L1 诊断与图 MVP 成型，按"先 L1 后 L2"推进。

---

## 13. 一句话定位

"L2 草稿型 Agent 用 Python + 策略模式把 L1 诊断结果升级成可执行处置草稿——按 `subgraph_ref` 回查图证据不重查图、按草稿类型分派 `DraftBuilder` 综合成返工单/8D/SOP、`requires_confirmation` 恒成立且 L2 服务不持有任何写 client、下达走人确认 + 返工/工单/质量上下文正常应用服务不旁路写——全程不进过点主事务、版本透传自 L1 证据、写闸门 100% 在人手里，是 MES 写红线下'查完 -> 诊断 -> 草拟处置'链路的安全落地形态。"
