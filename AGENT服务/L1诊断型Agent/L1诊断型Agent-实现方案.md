# L1 诊断型 Agent 实现方案（Python 技术栈）

> 本文是 [AGENT服务引入路线.md](../AGENT服务引入路线.md) §2.2 L1 诊断型 Agent 的落地展开，输出**技术栈、实现方案、实现步骤、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + LangGraph + Pydantic）。Agent 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 REST / Kafka 解耦，互不侵入。
> **口径纪律**：L1 全程**只读**，永不进过点主事务（[领域总览.md](../../领域模型/领域总览.md) §5.3），绝不旁路任何上下文的写路径。Agent 输出的是**根因假设 + 证据链 + 建议**，不是结论性判定——最终处置仍由工程师在正式界面确认。

---

## 1. 设计目标与边界

### 1.1 目标

把 RAG 路线 A 的"追溯型检索"升级成**多步只读推理**：Agent 主动调用各限界上下文暴露的只读工具，自己决定下一步查什么，最终按 **5M1E** 给出根因假设排序 + 证据链。

典型场景："某单件出现焊接不良" -> Agent 自动串起：

1. 查该单件过点记录（过点执行上下文）
2. 拿到 `routeVersion` -> 查当时工艺版本（工艺管理上下文，§5.1 版本一致性）
3. 查同批次锡膏批次（物料上下文）+ 贴片机当时参数（设备数据接入上下文）
4. 查同批次其他单件不良率 -> 给出 5M1E 假设排序

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **只读** | 所有工具只读，无任何写工具注册到 Agent | toolset 白名单只含 `query_*` 工具；`ReadOnlyToolGate` 启动断言 |
| **不进过点主事务** | Agent 不调过点引擎放行 / 拦截 API | 过点上下文 toolset 不暴露 `pass/judge` 类工具 |
| **版本一致性** | 查工艺必须带 `routeVersion` 过滤 | 工艺查询工具强制 `routeVersion` 入参，ACL 层校验 |
| **权限隔离** | 工具调用前按车间 / 产线 / 角色过滤 | 工具入参强制 `tenant_context`，检索前过滤 |
| **可观测兜底** | 每步推理带工具调用链 + 置信度，低置信度转人工 | trace 落库 + 置信度阈值 |

### 1.3 与 Java 技术栈的关系

- Agent 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈——只通过 REST 调只读接口、订阅 Kafka 只读事件。
- 跨语言带来的边界反而是好事：物理上无法共享 Java 事务 / 内存，天然强制 Agent 不进过点主事务、不旁路应用服务写路径。
- 复用 [实现说明](../../实现说明/) 既有的 Kafka topic、REST 只读接口、领域事件 envelope，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 类型提示 + Pydantic 校验，AI 生态最成熟 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI、与 Pydantic 无缝，适合做 Agent HTTP 入口 |
| Agent 编排 | **LangGraph** | 显式状态图 + ReAct 工具调用循环，比裸 LangChain 更可控；可设 `recursion_limit` 等硬上限 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 模型可插拔，配置切换 Claude / 通义千问 / DeepSeek / 本地化模型 |
| 数据校验 | **Pydantic v2** | 工具入参 / 报告 / DTO 的 schema 即类型，自动生成 JSON Schema 给模型 tool-calling |
| HTTP 客户端 | **httpx**（异步） | 调各上下文只读 REST、调 RAG 服务 |
| 消息 | **aiokafka** | 订阅领域事件（`ProcessRouteActivated` 等），异步非阻塞 |
| 持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 会话、工具调用 trace、根因报告 |
| 缓存 | **redis-py (async)** | 工具结果短期缓存（同会话重复查询去重）、限流 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 串联、指标告警 |
| 配置 | pydantic-settings | 环境变量 / 配置文件统一管理 |
| 部署 | 独立微服务 `agent-service`（uvicorn + gunicorn worker） | 与三大服务同网格，K8s 部署 |

### 2.2 为什么选 LangGraph 而非裸 LangChain / 裸调模型 API

- L1 的核心是**多步规划**：模型需根据上一步工具返回决定下一步。LangGraph 的 `StateGraph` 把"模型思考节点 -> 工具执行节点 -> 回模型"做成显式图，可对每条边加条件路由、超时、递归上限。
- LangChain 的 `AgentExecutor` 是黑盒循环，难做细粒度权限拦截与 trace；裸调模型 API 要自己实现 tool-calling 循环、重试、参数校验，重复造轮子。
- LangGraph 的 `recursion_limit` 直接对应"最大步数"红线，硬上限靠框架兜底。

### 2.3 为什么不引入 Java 侧 Spring AI

- 本 MES 全栈 Java，但 Agent 场景 Python 生态（LangGraph、模型 SDK、评测工具）明显更成熟，且 Agent 服务是**只读旁路**，不参与 MES 写事务，跨语言风险可控。
- Agent 失败最坏情况是"没诊断出来"，不会产生写副作用——这降低了跨语言运维的代价。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ agent-service（独立微服务，Python + FastAPI + LangGraph）          │
│                                                                    │
│  ┌──────────────────┐    ┌──────────────────────────────────────┐ │
│  │ FastAPI Router    │───▶│ DiagnosisService                      │ │
│  │  POST /diagnose   │    │  - 构建 LangGraph 状态图               │ │
│  └──────────────────┘    │  - 驱动 ReAct 循环                     │ │
│         ▲                 └──────────────┬───────────────────────┘ │
│         │                                │                         │
│         │                     ┌──────────▼──────────┐              │
│      工程师 UI                  │  LangGraph StateGraph  │            │
│                                 │  model_node ↔ tool_node │            │
│                                 └──────────┬──────────┘              │
│                                            │ 调用工具                 │
│                     ┌──────────────────────┼──────────────────┐     │
│                     │     ToolRegistry（只读白名单）            │     │
│                     │  过点 toolset | 工艺 toolset | 物料 ...   │     │
│                     └──────┬──────────┬──────────┬─────────────┘     │
│                            │          │          │                   │
│              ┌─────────────▼┐ ┌───────▼────────┐ ┌▼────────────┐    │
│              │ ACL 适配层    │ │ ACL 适配层      │ │ RAG 检索     │    │
│              │ (httpx->REST) │ │ (httpx->REST)   │ │ (httpx)      │    │
│              └──────┬───────┘ └───────┬────────┘ └┬────────────┘    │
└─────────────────────┼─────────────────┼───────────┼──────────────────┘
                      │                 │           │
        ┌─────────────▼─────┐  ┌────────▼────────┐  │
        │ 生产执行服务 (Java) │  │ 制造资源服务(Java)│  │ RAG 服务
        │ (过点/WIP/工单 REST)│  │ (工艺/物料 REST) │  │ (向量检索)
        └────────────────────┘  └─────────────────┘  └─────────┘
                      ▲
                      │ 领域事件订阅（可选，主动触发场景）
              ┌───────┴────────┐
              │ aiokafka        │
              │ ProcessRoute*   │
              │ 设备状态变更     │
              └─────────────────┘
```

### 3.1 关键设计决策

- **ACL 防腐层**：每个上下文的 REST 调用都经防腐层适配，把外部 DTO 转成 Agent 内部领域模型（`TraceNode`、`ProcessVersionSnapshot` 等）。外部 DTO schema 变化不污染 Agent 核心——符合 CLAUDE.md 的低耦合 / ACL 约束。
- **ToolRegistry 白名单**：工具注册集中管理，**只读工具才允许注册**。写工具（如发起返工）在 L1 阶段根本不注册到 `ToolRegistry`，从代码层面杜绝越界。
- **会话状态外置**：多步推理的中间状态存 MySQL + Redis，进程重启可恢复，不依赖模型侧会话。LangGraph 的 `checkpointer` 接 SQLAlchemy 做持久化中断恢复。

---

## 4. 工具注册：对齐限界上下文

### 4.1 工具与上下文映射

每个限界上下文暴露一组只读工具，**边界即工具边界**（[领域总览.md](../../领域模型/领域总览.md) §2）：

| 上下文 | 只读工具（示例） | 说明 |
|--------|-----------------|------|
| 过点执行 | `query_pass_records(serial_no)` / `query_test_results(serial_no)` | 查过点记录、TestResult（§5.3 同事务数据） |
| 工单管理 | `query_work_order(wo_id)` / `query_wo_progress(wo_id)` | 工单状态与进度 |
| 工艺管理 | `query_process_route(route_id, route_version)` | **强制带 `route_version`**（§5.1） |
| 物料 | `query_material_batch(batch_no)` / `query_bom_version(bom_id, version)` | 批次、BOM 版本 |
| 在制品追踪 | `query_wip_position(serial_no)` / `query_kit_status(wo_id)` | 位置与齐套 |
| 设备数据接入 | `query_device_params(asset_id, time_range)` | 贴片机当时参数 |
| 设备工装台账 | `query_asset_status(asset_id)` | 设备当时状态 |
| 返修 / 返工 | `query_repair_history(serial_no)` / `query_rework_orders(wo_id)` | 历史返修返工 |
| RAG 服务 | `search_docs(query, route_version_filter)` | 文档检索（路线 B），带版本过滤 |

### 4.2 工具元数据

每个工具注册时声明（Pydantic 模型）：

```python
class ToolDescriptor(BaseModel):
    name: str                          # "query_pass_records"
    description: str                   # 给模型看的语义说明
    bounded_context: str               # "过点执行上下文" —— 权限过滤与可观测
    read_only: bool                    # L1 必须 True
    args_schema: type[BaseModel]       # Pydantic 模型，自动转 JSON Schema 给模型
    required_tenant_scopes: list[str]  # ["WORKSHOP", "LINE"] —— 权限范围
```

`read_only=False` 的工具在 L1 的 `ToolRegistry` 启动时直接拒绝注册——**红线靠代码兜底，不靠口头约束**。

### 4.3 权限与版本过滤

- **权限**：工具调用前，`ToolExecutionInterceptor` 从会话拿 `TenantContext`（车间 / 产线 / 角色），与工具 `required_tenant_scopes` 比对，不匹配直接拒绝，不进 ACL。
- **版本**：`query_process_route` 在 ACL 层强制校验 `route_version` 非空且为已生效版本，否则返回错误让 Agent 重试——避免基于已失效工艺给根因（§5.1）。

---

## 5. 实现方案

### 5.1 推理循环（LangGraph ReAct）

用 LangGraph 的 `StateGraph` 构建 ReAct 图：`model_node`（模型思考 + 产出 tool calls）↔ `tool_node`（执行工具 + 回灌结果）。模型无 tool call 时收口，输出根因报告。

```text
用户问题："单件 SN-001 焊接不良根因"
  ↓
[model_node] 需要先拿到该单件的过点轨迹 -> 产出 tool_call: query_pass_records
[tool_node] 执行 -> 返回过点记录列表，含 routeVersion=v3、assetId=...
  ↓
[model_node] routeVersion=v3，查当时工艺 -> tool_call: query_process_route(v3)
[tool_node] 执行 -> 返回工艺步骤，焊接站参数模板
  ↓
[model_node] 查同批次锡膏和贴片机参数 -> tool_call: query_material_batch + query_device_params
[tool_node] 执行 -> 锡膏批次 B-77、贴片机参数偏移
  ↓
[model_node] 查同批次不良率 -> tool_call: query_defect_rate(B-77)
[tool_node] 执行 -> 同批次不良率 12%，高于基线
  ↓
[model_node] 无更多 tool call -> 输出 5M1E 假设排序 + 证据链
```

- **最大步数限制**：`recursion_limit=20`（一次 model+tool 算 2 步，即最多 10 次工具调用），超过抛 `GraphRecursionError` 被捕获，返回"诊断未完成转人工"。
- **超时**：单工具调用 ≤2s（httpx timeout），整会话 ≤60s（`asyncio.wait_for` 包住图驱动）。
- **中断恢复**：LangGraph `SqlSaver` checkpointer 把中间状态落 MySQL，进程重启可从断点续跑。

### 5.2 会话与状态

```sql
diagnosis_session
  - session_id (PK)
  - user_id / tenant_context (JSON)
  - question (原始问题)
  - status (RUNNING / DONE / TIMEOUT / FAILED)
  - created_at / updated_at

tool_call_trace
  - trace_id (PK)
  - session_id (FK)
  - step_no
  - tool_name
  - bounded_context
  - input_payload (JSON)
  - output_payload (JSON)
  - latency_ms
  - status (OK / DENIED / ERROR)
  - occurred_at

diagnosis_report
  - report_id (PK)
  - session_id (FK)
  - summary
  - confidence (FLOAT)
  - hypotheses (JSON)        -- 5M1E 假设列表
  - needs_human_review (BOOL)
  - created_at
```

- 每一步工具调用落 `tool_call_trace`，既是可观测来源，也是给工程师"证据链"的原始数据。
- LangGraph 的 state 也通过 checkpointer 持久化，但 `tool_call_trace` 是业务侧可读的平铺视图，方便 UI 展示。

### 5.3 输出结构（根因报告）

Agent 输出**结构化**报告，不是自由文本（Pydantic 强约束）：

```python
class Hypothesis(BaseModel):
    category: FiveM1ECategory    # Material / Machine / Method / Measurement / Man / Environment
    rank: int
    statement: str
    evidence: list[str]          # ["trace_id=...", "trace_id=..."]
    suggested_action: str

class DiagnosisReport(BaseModel):
    summary: str
    confidence: float            # 0.0 ~ 1.0
    hypotheses: list[Hypothesis]
    disclaimer: str = "本报告为辅助诊断假设，最终处置需工程师确认"
    needs_human_review: bool = False
```

- `confidence < 0.5` 直接置 `needs_human_review=True`，不展示给操作工，只推给工程师。
- 5M1E 分类固化在 `Enum`，模型只能选枚举值，避免乱编类别。模型输出经 Pydantic 校验，不符合 schema 直接判失败重试。

### 5.4 ACL 防腐层

每个上下文一个 ACL 客户端，示例（过点上下文）：

```python
class PassExecutionAclClient:
    """过点执行上下文只读 ACL：调生产执行服务 REST，外部 DTO -> 内部视图。"""

    def __init__(self, rest_client: PassExecutionRestClient):
        self._rest = rest_client

    async def query_pass_records(
        self, serial_no: str, tenant: TenantContext
    ) -> PassRecordView:
        # 1. 权限校验（tenant 在拦截器已做，此处二次确认）
        # 2. 调 REST，带 X-Tenant-* header
        dto = await self._rest.find_by_serial_no(serial_no, tenant)
        # 3. 外部 DTO -> 内部 PassRecordView（防腐层核心职责）
        return PassRecordMapper.to_view(dto)
```

- 外部 DTO `PassRecordDTO` 不进入 Agent 核心，只暴露 `PassRecordView`。
- 工艺查询工具额外做 `route_version` 校验（见 §4.3）。

### 5.5 与 RAG 服务的复用

- L1 不重建向量库，文档检索通过 httpx 调 [RAG服务](../../RAG服务/) 的检索接口（路线 B）。
- `search_docs` 工具封装该 HTTP 调用，`route_version_filter` 透传，保证检索到的 SOP / 工艺文档与生产执行侧缓存版本一致（§5.1）。

---

## 6. 推荐包结构（Python src layout）

```text
agent_service/
  app/
    api/                # FastAPI 路由层（对应 facade / facade-impl）
      diagnosis_router.py
      schemas.py             # Request / Response 模型
    application/        # 应用服务，编排会话
      diagnosis_service.py   # 构建 LangGraph 图、驱动循环
      session_manager.py
    domain/             # Agent 领域模型
      session.py             # DiagnosisSession, SessionStatus
      report.py              # DiagnosisReport, Hypothesis, FiveM1ECategory
      tool.py                # ToolDescriptor, ToolRegistry, ReadOnlyToolGate
      tenant.py              # TenantContext
    infrastructure/
      ai/                    # LangGraph 图工厂、LLM 客户端
        graph_builder.py
        llm_factory.py
      acl/                   # 各上下文 ACL 客户端 + Mapper
        pass_execution.py
        process_management.py
        material.py
        device_data.py
        ...
      rag/                   # RagSearchClient（httpx 调 RAG 服务）
      kafka/                 # aiokafka Consumer（主动触发场景）
        listeners.py
      persistence/           # SQLAlchemy 模型 + Repository
        models.py
        session_repo.py
        trace_repo.py
        report_repo.py
        checkpointer.py      # LangGraph SqlSaver
      redis_/                # 工具结果缓存
        tool_cache.py
      obs/                   # OTel exporter、prometheus 指标
        tracing.py
        metrics.py
    config.py                # pydantic-settings
    main.py                  # FastAPI app 入口 + lifespan 启动断言
  tests/
  pyproject.toml
```

- `domain/tool.ReadOnlyToolGate`：FastAPI `lifespan` 启动时校验所有注册工具 `read_only=True`，否则启动失败——红线靠启动断言。
- `infrastructure/acl/` 是防腐层落地，每个上下文一个客户端 + Mapper，符合 CLAUDE.md ACL 约束。

---

## 7. 关键代码骨架

### 7.1 工具注册与只读闸门

```python
# app/domain/tool.py
from pydantic import BaseModel

class ToolDescriptor(BaseModel):
    name: str
    description: str
    bounded_context: str
    read_only: bool
    args_schema: type[BaseModel]
    required_tenant_scopes: list[str]
    handler: object  # 实际可调用对象，注册时不暴露给模型

class ReadOnlyToolGate(Exception):
    """启动时发现非只读工具，拒绝启动。"""

class ToolRegistry:
    def __init__(self) -> None:
        self._descriptors: dict[str, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        if not descriptor.read_only:
            raise ReadOnlyToolGate(
                f"L1 Agent 禁止注册非只读工具: {descriptor.name}"
            )
        self._descriptors[descriptor.name] = descriptor

    def validate_on_startup(self) -> None:
        for d in self._descriptors.values():
            if not d.read_only:
                raise ReadOnlyToolGate(f"非只读工具混入: {d.name}")

    def tools_for(self, tenant: TenantContext) -> list[ToolDescriptor]:
        """按租户范围过滤可见工具。"""
        return [
            d for d in self._descriptors.values()
            if tenant.can_access(d.required_tenant_scopes)
        ]
```

### 7.2 工具执行节点（权限 + trace）

```python
# app/infrastructure/ai/tool_node.py
class ToolNode:
    """LangGraph 工具执行节点：权限校验 -> 调 ACL -> 落 trace。"""

    def __init__(
        self,
        registry: ToolRegistry,
        trace_repo: ToolCallTraceRepo,
        metrics: MetricsCollector,
    ) -> None:
        self._registry = registry
        self._trace_repo = trace_repo
        self._metrics = metrics

    async def __call__(self, state: AgentState) -> AgentState:
        tenant = state["tenant"]
        results = []
        for call in state["pending_tool_calls"]:
            tool = self._registry._descriptors.get(call["name"])
            if tool is None or not tenant.can_access(tool.required_tenant_scopes):
                await self._trace_repo.save_denied(call["name"], tenant)
                self._metrics.tool_denied.inc(call["name"])
                results.append(self._deny_result(call))
                continue

            t0 = time.perf_counter()
            try:
                args = tool.args_schema.model_validate(call["args"])
                # 工艺查询在 ACL 内部做 route_version 校验
                view = await tool.handler(**args.model_dump(), tenant=tenant)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                await self._trace_repo.save_ok(
                    call["name"], tool.bounded_context, args, view, latency_ms
                )
                results.append(self._ok_result(call, view))
            except Exception as e:
                await self._trace_repo.save_error(call["name"], e)
                self._metrics.tool_error.inc(call["name"])
                results.append(self._err_result(call, str(e)))
        state["tool_results"] = results
        state["pending_tool_calls"] = []
        return state
```

### 7.3 ACL 客户端（httpx + 版本校验）

```python
# app/infrastructure/acl/process_management.py
class ProcessManagementAclClient:
    """工艺管理上下文只读 ACL，强制 route_version。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def query_process_route(
        self, route_id: str, route_version: str, tenant: TenantContext
    ) -> ProcessRouteView:
        if not route_version:
            raise ValueError("route_version 必填，禁止查询无版本工艺（§5.1）")

        resp = await self._http.get(
            f"/api/process-routes/{route_id}",
            params={"version": route_version},
            headers=tenant.headers(),
            timeout=2.0,
        )
        resp.raise_for_status()
        dto = ProcessRouteDTO.model_validate(resp.json())
        # ACL 层二次校验返回的版本状态为已生效
        if dto.status != "ACTIVE":
            raise ValueError(f"工艺版本 {route_version} 非生效状态: {dto.status}")
        return ProcessRouteMapper.to_view(dto)
```

### 7.4 应用服务编排（LangGraph）

```python
# app/application/diagnosis_service.py
class DiagnosisService:
    def __init__(
        self,
        graph_builder: GraphBuilder,
        session_manager: SessionManager,
        report_repo: ReportRepo,
    ) -> None:
        self._graph_builder = graph_builder
        self._session_manager = session_manager
        self._report_repo = report_repo

    async def diagnose(
        self, request: DiagnosisRequest, tenant: TenantContext
    ) -> DiagnosisReport:
        session = await self._session_manager.create(request, tenant)
        graph = self._graph_builder.build_for(tenant)  # 按权限过滤工具集

        try:
            final_state = await asyncio.wait_for(
                graph.ainvoke(
                    {
                        "messages": [self._build_system_prompt(request)],
                        "tenant": tenant,
                        "session_id": session.id,
                    },
                    config={"recursion_limit": 20, "configurable": {"thread_id": session.id}},
                ),
                timeout=60.0,
            )
            report = ReportParser.parse(final_state["messages"][-1], session)
            await self._session_manager.finish(session, report)
            await self._report_repo.save(report)
            return report

        except (GraphRecursionError, asyncio.TimeoutError) as e:
            await self._session_manager.mark_timeout(session)
            return DiagnosisReport.partial(session, f"诊断未完成，已转人工: {e}")

    def _build_system_prompt(self, req: DiagnosisRequest) -> str:
        return (
            "你是 MES 车间根因诊断助手。基于只读工具按 5M1E 给出根因假设排序。\n"
            "约束：\n"
            "1. 只能调用提供的工具，不得编造数据。\n"
            "2. 查工艺必须带 route_version。\n"
            "3. 每个假设必须引用工具返回的证据。\n"
            "4. 输出严格遵循 DiagnosisReport JSON 结构。\n"
            f"问题：{req.question}"
        )
```

### 7.5 FastAPI 入口 + 启动断言

```python
# app/main.py
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动断言：所有工具必须只读
    app.state.tool_registry.validate_on_startup()
    # 初始化 httpx client、LLM、checkpointer ...
    yield

app = FastAPI(title="MES L1 Diagnosis Agent", lifespan=lifespan)

@app.post("/agent/diagnose", response_model=DiagnosisReport)
async def diagnose(
    req: DiagnosisRequest,
    tenant: TenantContext = Depends(tenant_from_token),
    svc: DiagnosisService = Depends(get_diagnosis_service),
) -> DiagnosisReport:
    return await svc.diagnose(req, tenant)
```

### 7.6 主动触发（可选，订阅领域事件）

```python
# app/infrastructure/kafka/listeners.py
class DefectRateSpikeListener:
    """订阅只读事件，主动诊断同批次。不消费任何写命令。"""

    def __init__(self, svc: DiagnosisService) -> None:
        self._svc = svc

    async def run(self, consumer: AIOKafkaConsumer) -> None:
        async for msg in consumer:
            event = DefectRateSpikeEvent.model_validate_json(msg.value)
            tenant = TenantContext.from_event(event)
            # 不等人问，主动诊断
            await self._svc.diagnose(
                DiagnosisRequest.auto(
                    f"批次 {event.batch_no} 不良率突增，请诊断"
                ),
                tenant,
            )
```

- 仅订阅只读事件，不消费任何写命令事件。
- 主动诊断结果同样落 `diagnosis_report`，推送给工程师而非操作工。

---

## 8. 可观测性与兜底

> **完整设计见** [可观测性方案](../Agent可观测性-设计与实现方案.md)（L1/L2/L3 共用可观测底座，事实源唯一）。本节仅列 L1 要点索引。

L1 是可观测底座基准，无层级特有指标，全部使用通用 `agent_*` 指标。要点对应新文档章节：

- **指标**：`agent_session_total` / `agent_tool_call_*` / `agent_low_confidence_total` / `agent_recursion_limit_hit_total` 等 -> 新文档 §5.2。
- **链路**：每会话一个 `trace_id`，`traceparent` 透传下游 Java 服务，`tool_call_trace` 落证据链 -> 新文档 §4、§7。
- **置信度与兜底**：`confidence < 0.5` 转 `needs_human_review` 不推操作工；工具连续失败 3 次终止；`recursion_limit` / 超时转人工 -> 新文档 §9、§12。
- **约束清单**：见新文档 §18。

---

## 9. 实现步骤

### 阶段一：骨架与最小诊断（2 周）

1. 搭 `agent_service` 骨架（FastAPI + uvicorn），对齐 §6 包结构。
2. 接入 LangGraph + 一个 LLM 供应商（`langchain-{provider}`），配可插拔 `llm_factory`。
3. 实现 `DiagnosisService` 单步调用（先支持 1–2 个工具，验证 ReAct 循环跑通）。
4. 建 MySQL 表：`diagnosis_session` / `tool_call_trace` / `diagnosis_report`，接 SQLAlchemy async。
5. 实现 `ReadOnlyToolGate` 启动断言（FastAPI lifespan）。

### 阶段二：工具集与 ACL（3 周）

6. 梳理 [领域总览.md](../../领域模型/领域总览.md) §2 各上下文现有只读 REST，逐个封装 ACL 客户端 + Mapper（httpx async）。
7. 优先实现过点 / 工艺 / 物料 / 设备参数四个核心 toolset（覆盖 5M1E 大部分维度）。
8. 实现 `query_process_route` 的 `route_version` 强制校验（§4.3）。
9. 接入 RAG 服务 `search_docs` 工具（httpx）。
10. 实现 `ToolNode`：权限过滤 + trace 落库 + 指标埋点。

### 阶段三：报告与可观测（2 周）

11. 固化 `DiagnosisReport` / `Hypothesis` / `FiveM1ECategory`，实现 `ReportParser`（Pydantic 校验模型输出）。
12. 置信度阈值与 `needs_human_review` 兜底。
13. 接入 OpenTelemetry + prometheus 指标（[可观测性方案](../Agent可观测性-设计与实现方案.md) §5.2）。
14. 工程师 UI：展示报告 + 证据链可点开回溯 trace。

### 阶段四：主动触发试点（2 周）

15. 接 aiokafka，订阅 `mes.defect-rate-spike` 等只读事件，做主动诊断试点。
16. 验证主动诊断不进过点主事务、不调写 API。
17. 灰度一条产线，收集工程师反馈。

### 阶段五：加固与推广

18. 工具结果缓存（redis）去重，降低下游压力。
19. 限流：每会话 `recursion_limit`、每租户并发会话上限（redis 信号量）。
20. 补全剩余上下文 toolset，覆盖 14 个上下文。
21. 沉淀评测集（典型不良场景 + 预期根因），用 pytest + 评测脚本回归模型 / 提示词变更（评测体系完整设计见 [RAG与Agent评测-设计与实现方案.md](../../RAG与Agent评测/RAG与Agent评测-设计与实现方案.md)）。

---

## 10. 约束落地检查清单

- [ ] 所有注册工具 `read_only=True`，`ReadOnlyToolGate` lifespan 启动断言生效。
- [ ] 无任何写工具（返工 / 工艺修改 / 放行拦截）注册到 Agent。
- [ ] `query_process_route` 强制 `route_version` 入参，ACL 层校验返回状态为 `ACTIVE`。
- [ ] 工具调用前按 `TenantContext` 权限过滤，拒绝记录可观测。
- [ ] Agent 不调过点引擎放行 / 拦截 API，过点 toolset 只含 `query_*`。
- [ ] 每步工具调用落 `tool_call_trace`，OpenTelemetry 透传 `traceparent` 到下游 Java 服务。
- [ ] 报告带置信度，低于 0.5 转 `needs_human_review` 不展示给操作工。
- [ ] 单工具超时 ≤2s，整会话 ≤60s，`recursion_limit` ≤20（≤10 次工具调用）。
- [ ] 主动触发只订阅只读事件，不消费写命令。
- [ ] 输出经 Pydantic 校验，不符合 schema 判失败重试。
- [ ] 输出含 disclaimer：辅助假设，最终处置需工程师确认。

---

## 11. 面试防守 Q&A

**Q：为什么 Agent 用 Python，而 MES 主体是 Java？跨语言不会增加复杂度吗？**
A：是经过取舍的。Agent 是**只读旁路**，不参与 MES 写事务，最坏情况是"没诊断出来"，不会产生写副作用——这降低了跨语言运维的代价。而 Python 在 AI 生态（LangGraph、模型 SDK、评测工具）上明显更成熟。跨语言的物理边界反而强化了红线：Agent 无法共享 Java 事务 / 内存，天然不能进过点主事务、不能旁路应用服务写路径。两边只通过 REST 只读接口 + Kafka 只读事件解耦，复用既有契约，不造新管道。

**Q：为什么选 LangGraph 而不是 LangChain 的 AgentExecutor？**
A：L1 的核心是多步规划，要对每步做权限拦截、trace、超时、递归上限。LangGraph 的 `StateGraph` 把"模型思考节点 ↔ 工具执行节点"做成显式图，条件路由、超时、`recursion_limit` 都可精细控制；`AgentExecutor` 是黑盒循环，难做细粒度拦截。`recursion_limit` 直接对应"最大步数"红线，硬上限靠框架兜底，不是口头约束。

**Q：L1 诊断 Agent 怎么保证不写成面向过程的流水账？**
A：按分层落地——`api` 路由层、`application` 编排会话、`domain` 放 `DiagnosisSession` / `DiagnosisReport` / `ToolRegistry` 等领域模型、`infrastructure/acl` 做防腐。Agent 的"多步规划"是 `DiagnosisService` 驱动 LangGraph 图，不是一堆 if-else 串工具调用。工具用 `ToolDescriptor` 元数据描述，注册到 `ToolRegistry`，符合 SRP——每个工具只负责一个上下文的只读查询。

**Q：怎么保证 Agent 不会基于失效工艺给根因？**
A：`query_process_route` 工具强制 `route_version` 入参，ACL 层校验返回状态为 `ACTIVE`，否则返回错误让 Agent 重试。过点记录本身就绑 `routeVersion`（§5.1），Agent 从过点记录拿到的版本就是当时生产用的版本——版本一致性是从领域模型兜上来的，不是 Agent 自己保证的。

**Q：Agent 调这么多上下文，权限怎么管？**
A：工具注册时声明 `required_tenant_scopes`，`ToolNode` 在调用前按会话的 `TenantContext` 过滤，不匹配直接拒绝并落 trace。权限在调用前过滤，不是答完再裁剪。本 MES 的限界上下文边界本身就是天然的权限切分面，每个上下文一个 toolset，权限跟着上下文走。

**Q：Agent 失败了怎么办？会不会误导操作工？**
A：所有报告带置信度，低于 0.5 标记 `needs_human_review`，只推工程师不推操作工。工具连续失败 3 次终止会话转人工。报告含 disclaimer：辅助假设，最终处置需工程师确认。这和 MES 防错理念一致——宁可拦下让人判，不硬答。L1 全程只读，最坏情况是"没诊断出来"，不会产生写副作用。

**Q：模型输出不符合 schema 怎么办？**
A：`DiagnosisReport` 用 Pydantic 模型强约束，模型输出经 `model_validate` 解析，不符合 schema 直接判失败重试（LangGraph 可配重试次数）。5M1E 分类用 Enum 固化，模型只能选枚举值，避免乱编类别。重试仍失败则转人工，不硬答。

---

## 12. 一句话定位

"L1 诊断型 Agent 用 Python + LangGraph 的 ReAct 状态图把 RAG 追溯检索升级成多步只读推理，工具注册对齐 14 个限界上下文、靠 `ReadOnlyToolGate` 启动断言锁死只读边界、工艺查询强制带 `route_version` 保版本一致性——作为只读旁路与 Java 主体跨语言解耦，全程不进过点主事务、不旁路写路径，输出经 Pydantic 校验的带置信度 5M1E 假设而非结论，最终处置仍由工程师确认。"
