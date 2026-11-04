# FactoryAgent 诊断能力（多步只读推理）

> 本文是 [FactoryAgent-总体设计与实现方案.md](FactoryAgent-总体设计与实现方案.md) 中**诊断能力（`diagnosis`）**的落地展开，输出实现方案、运行时形态、工具集、报告结构与约束。
> **定位**：诊断能力 = 开放式 ReAct 取证循环。输入是"一个待解释的现象"，输出是"根因假设排序 + 证据链"。它既是一个可独立调用的入口，也是被[草稿编写能力](FactoryAgent-草稿编写能力.md)与[流程编排能力](FactoryAgent-流程编排能力.md)复用的**能力（`Agent`）**。
> **口径纪律**：全程**只读**，永不进过点主事务（[领域总览.md](../../领域模型/领域总览.md) §5.3），不旁路任何上下文的写路径。输出是**根因假设 + 证据链 + 建议**，不是结论性判定——最终处置仍由工程师在正式界面确认。

---

## 1. 定位与边界

### 1.1 这项能力解决什么

把制造追溯的**单跳取证**升级成**多步只读推理**：模型不再一次性检索，而是自己决定下一步查什么，逐步收敛到根因，按 **5M1E** 给出假设排序与证据链。注意分工：追溯工具（`trace_lookup`）只负责“给定种子与锚点取回证据”，多步推理与结论归诊断能力（[制造追溯-需求与实现效果.md](../../制造追溯-需求与实现效果.md) §5）。

典型链路（"单件 SN-001 焊接不良"）：

1. 查该单件过点记录（过点执行上下文）
2. 拿到工艺版本 → 查当时工艺（工艺管理上下文，§5.1 版本一致性）
3. 查同批次锡膏批次（物料上下文）+ 贴片机当时参数（设备数据接入上下文）
4. 查同批次其他单件不良率 → 给出 5M1E 假设排序

### 1.2 两种入口，同一个 Agent

同一个 ReAct Agent 有两种触发方式，入参不同、其余全同：

| 入口 | 输入 | 谁触发 | 输出侧重 |
|------|------|--------|---------|
| **提问式** | 自然语言问题（"这批焊接不良根因"）+ 业务对象引用 | 工艺 / 质量工程师在 UI 提问 | 5M1E 假设排序 + 处置建议 |
| **异常驱动式** | 结构化异常（`mismatch{expected, actual, code}` / `fault{asset_id, alarm_at}` / `complaint{batch_no}`） | 编排 flow 的非确定能力节点、领域事件订阅器 | 根因假设 + 置信度 + 仍需人确认的疑点 |

**关键差别只有一个**：异常驱动式已经有结构化锚点，取证从锚点出发；提问式需要模型自己先把问题落到业务对象上。这正是"能力"与"入口"分离的好处——编排里要判根因时，不需要另写一个"根因 Agent"，只需要换个入参。

### 1.3 硬边界

| 边界 | 说明 | 落地 |
|------|------|------|
| **只读** | 所有工具只读，写工具不注册到该能力 | `CapabilitySpec(writes_allowed=False)` + `Permission` 断言 |
| **无写 client** | ACL 客户端只有 `query/fetch/search` 动词 | `validate_acl_clients` 扫描方法名前缀 |
| **不进过点主事务** | 不调过点引擎放行 / 拦截 API | 过点 toolset 只暴露 `query_*`；放行类工具全局禁注册 |
| **版本一致性** | 查工艺必须带版本锚点 | 工具强制版本入参，ACL 校验 `ACTIVE` |
| **权限隔离** | 调用前按车间 / 产线 / 角色过滤 | 工具入参强制 `tenant_context`，`ToolExecHook` 前置拦截（注：本仓库自有钩子，与 LangGraph prebuilt 的 `ToolNode` 无关） |
| **可观测兜底** | 每步带工具调用链 + 置信度，低置信度转人工 | `agent_step_record` + 置信度阈值 |

### 1.4 与其它能力的关系

```text
                   ┌────────────────────────────┐
   提问式 ─────────▶│                            │
                   │      诊断能力（ReAct Agent）  │──▶ DiagnosisReport
   编排非确定节点 ──▶│  - 自有 toolset（只读）      │      ├─ 5M1E 假设排序
   领域事件订阅 ────▶│  - 报告 + 证据链 + 置信度    │      ├─ 证据引用（trace_id / evidence_set_id）
                   └────────────────────────────┘      └─ needs_human_review
                              ▲
                              │ 复用同一 Agent，不重写
                   ┌──────────┴──────────┐
                   │  草稿编写能力         │  用 report 作为草稿语义骨架
                   │  流程编排能力         │  在异常分支调用它判根因
                   └─────────────────────┘
```

- **被草稿复用**：草稿不重复推理，直接消费 `DiagnosisReport`（含 `evidence_set_id` 与证据引用），保证草稿每句话可回溯证据。
- **被编排复用**：换线 mismatch、客诉追溯等节点 `invoke("diagnosis", payload, parent_session_id=...)`，共用同一 trace 与租户上下文。
- **不反向调用**：诊断能力不调草稿、不调编排——它是纯上游。这条单向性让"只读"这个安全属性可以一键验证。

---

## 2. 运行时形态：开放式 ReAct Agent

用 AgentScope 2.0 的统一 `Agent` 类（内置 ReAct）承载 `model ↔ tool` 循环：模型节点（思考 + 产出 tool calls）与工具执行钩子（执行工具 + 回灌结果）。模型无 tool call 时收口，输出根因报告。

```text
用户问题 / 结构化异常："单件 SN-001 焊接不良根因"
  ↓
[model_node] 需要先拿到该单件的过点轨迹 -> tool_call: query_pass_records
[tool_hook] 执行 -> 过点记录列表，含工艺版本 v3、asset_id...
  ↓
[model_node] 版本 v3，查当时工艺 -> tool_call: query_process_route(v3)
[tool_hook] 执行 -> 工艺步骤、焊接站参数模板
  ↓
[model_node] 查同批次锡膏 + 贴片机参数 -> tool_call: query_material_batch + query_device_params
[tool_hook] 执行 -> 锡膏批次 B-77、贴片机参数偏移
  ↓
[model_node] 查同批次不良率 -> tool_call: query_defect_rate(B-77)
[tool_hook] 执行 -> 同批次不良率 12%，高于基线
  ↓
[model_node] 无更多 tool call -> 输出 5M1E 假设排序 + 证据链
```

**为什么必须开放规划**：取证路径由中间结果驱动（查到"ST-A 借出未还"才去查上工单收线记录），不是固定 JOIN。这是诊断能力"三问"中"分支难以穷举"的命中点——详见 [能力边界与选型决策卡](FactoryAgent-能力边界与选型决策卡.md)。

**循环与预算的约束**：

| 项 | 取值 | 落地 |
|----|------|------|
| 最大步数 | 步数与工具调用预算上限 `=20` | 一次 model+tool 算 2 步，最多 10 次工具调用；超预算即终止并兜底 |
| 单工具超时 | ≤2s | httpx timeout |
| 整会话超时 | ≤60s | `asyncio.wait_for` 包住 Agent 驱动 |
| 会话内上下文 | `AgentState` + `SessionService`（Redis-backed） | 多轮追问与会话隔离（按 `session_id`）；**不承担跨闸门断点续跑**（诊断是一次性出报告的短调用）；同一调用内的 HITL 确认用 `event.RequireUserConfirmEvent` |
| 收口条件 | 模型无 tool call | 输出 `DiagnosisReport`（Pydantic 校验） |

---

## 3. 工具集：对齐限界上下文

每个上下文暴露一组只读工具，**边界即工具边界**（[领域总览.md](../../领域模型/领域总览.md) §2）。但工具粒度不等于 endpoint 粒度：**tool 数量 = 实体 × 问题类型，不是 API 数量**——同一实体的多查询口径合并为一个 tool 的可选定位维度，分页 / 字段裁剪 / 排序是实现细节不上 schema，由 ACL 内部定死（返回摘要字段 + `_truncated`/`_omitted_count` 标注）。规模治理四层收敛见 [FactoryAgent-总体设计与实现方案.md](FactoryAgent-总体设计与实现方案.md) §5.4。

| 上下文 | 只读工具（实体 × 问题粒度） | 说明 |
|--------|-----------------|------|
| 过点执行 | `query_pass_records(filter)` | 过点记录 + TestResult（§5.3 同事务数据）；口径：`serial_no` / `wo_id` / `time_range` |
| 工单管理 | `query_work_orders(filter)` / `get_work_order_detail(wo_id)` | 列表定位（多口径）/ 单工单全景（状态 + 进度 + BOM 版本 + 关联批次） |
| 工艺管理 | `get_process_route(route_id, version)` / `query_route_audit(route_id)` | **强制版本锚点**（§5.1） |
| 物料 | `query_material_batch(filter)` / `get_bom_version(bom_id, version)` | 批次（口径：`batch_no` / `material_code` / `wo_id`）、BOM 版本 |
| 在制品追踪 | `query_wip_position(serial_no)` / `query_kit_status(wo_id)` | 位置与齐套 |
| 设备数据接入 | `query_device_params(asset_id, time_range)` / `query_equipment_telemetry(asset_id, time_range)` | 当时参数、遥测时序 |
| 设备工装台账 | `query_asset(filter)` / `query_stencil_history(filter)` | 设备状态（口径：`asset_id` / `asset_type` / `status`）、钢网借还与主数据变更 |
| 返修 / 返工 | `query_repair_history(filter)` / `query_rework_orders(filter)` | 历史返修返工（口径：`serial_no` / `wo_id` / `batch_no`） |
| 质量 | `query_defect_rate(filter)` / `search_history_8d(keywords)` | 批次不良率（口径：`batch_no` / `wo_id` / `product_code`）、历史同类 8D |
| 制造追溯（只读，归属待定） | `trace_lookup` / `record_expand` / `version_resolve` | 追溯读模型（**首位注册**） |
| 文档知识工具 | `search_docs(query, version_anchor)` | 文档检索与引用 |

> `trace_lookup` 注册在诊断 toolset 首位，system prompt 引导"先查追溯读模型"；固定关联查询覆盖不足时再降级到各上下文 REST 补齐。

**查询口径合并示例（工单）**：Java 侧"按物料查 / 按产品编码查 / 按序列号反查"是多个 endpoint，对模型只是**同一个语义问题的不同入参维度**，收敛为一个 tool：

```python
class WorkOrderQuery(BaseModel):
    """工单定位：至少提供一个定位维度（model_validator 校验，否则拒绝）。"""
    wo_id: str | None = None          # 精确查
    material_code: str | None = None  # 按物料
    product_code: str | None = None   # 按产品编码
    serial_no: str | None = None      # 按序列号反查
    status: str | None = None         # 状态过滤
    time_range: TimeRange | None = None  # 强制时间窗 + 记录数上限（既有纪律）
```

- ACL 承担"语义接口 -> 多 endpoint"路由：按物料与按产品编码在 Java 侧是两个接口，对模型是一次 tool call；组合查询后端不支持时 ACL 内部两跳（先按物料查批次，再按批次查工单），比让模型自己编排两跳更省步数。
- 口径互斥 / 必须搭配时间窗等复杂约束场景，退化为 `mode` 枚举 + `key` 的显式口径形态（等价于把策略分派搬到 tool 层，与草稿能力"固定步骤链 + 策略分派"同构），schema 仍只有一份。

**工具元数据**：

```python
class ToolDescriptor(BaseModel):
    name: str                          # "query_work_orders"
    description: str                   # 给模型看的语义说明
    bounded_context: str               # "工单管理上下文"
    tool_group: str = "context:wo"     # Toolkit 分组键（静态装配 + reset_equipped_tools 兜底，总体设计 §5.4）
    capabilities: list[str]            # ["diagnosis", "orchestration"]
    read_only: bool                    # 诊断能力必须 True
    args_schema: type[BaseModel]       # Pydantic 模型，自动转 JSON Schema
    required_tenant_scopes: list[str]  # ["WORKSHOP", "LINE"]
```

**权限与版本**：

- **权限**：`ToolExecHook` 在调用前从会话拿 `TenantContext`，与 `required_tenant_scopes` 比对，不匹配直接拒绝并落 `DENIED` trace，不进 ACL。
- **版本**：`query_process_route` 在 ACL 层强制版本非空且为 `ACTIVE`，否则返回错误让模型重试——避免基于已失效工艺给根因（§5.1）。

---

## 4. 报告结构

输出是**结构化报告**，不是自由文本（Pydantic 强约束）：

```python
class FiveM1ECategory(str, Enum):
    MATERIAL = "MATERIAL"
    MACHINE = "MACHINE"
    METHOD = "METHOD"
    MEASUREMENT = "MEASUREMENT"
    MAN = "MAN"
    ENVIRONMENT = "ENVIRONMENT"

class Hypothesis(BaseModel):
    category: FiveM1ECategory       # 枚举固化，模型只能选，不能编类别
    rank: int
    statement: str
    evidence: list[str]             # ["trace_id=...", "trace_id=..."]
    suggested_action: str
    open_questions: list[str] = []  # 仍需人确认的疑点（异常驱动式必填）

class DiagnosisReport(BaseModel):
    session_id: str
    summary: str
    confidence: float               # 0.0 ~ 1.0
    hypotheses: list[Hypothesis]
    evidence_set_id: str | None = None  # 证据集引用，供草稿能力回查
    version: str | None = None       # 版本锚点（透传自证据）
    version_kind: str | None = None  # route|bom|rule|asset|standard
    version_ref_id: str | None = None
    evidence_refs: list[str] = []
    needs_human_review: bool = False
    disclaimer: str = "本报告为辅助诊断假设，最终处置需工程师确认"
```

- `confidence < 0.5` 直接置 `needs_human_review=True`，不展示给操作工，只推工程师。
- 模型输出经 `model_validate` 校验，不符合 schema 判失败重试；重试仍失败转人工。
- `open_questions` 是异常驱动式的关键字段：低置信度时必须列出疑点，**不允许硬答**。

---

## 5. 关键实现骨架（设计目标，尚未落地）

### 5.1 工具执行钩子（权限 + trace）

```python
# app/ai/tool_exec_hook.py
class ToolExecHook:
    """工具执行钩子（挂在 Toolkit 上）：权限校验 -> 调 ACL -> 落 trace。"""

    def __init__(self, toolkit: Toolkit, trace_repo: StepRepo,
                 metrics: MetricsCollector, capability: str = "diagnosis") -> None:
        self._toolkit = toolkit
        self._trace_repo = trace_repo
        self._metrics = metrics
        self._capability = capability

    async def __call__(self, state: AgentState) -> AgentState:
        tenant = state["tenant"]
        results = []
        for call in state["pending_tool_calls"]:
            tool = self._toolkit.get(call["name"], self._capability, tenant)
            if tool is None:
                await self._trace_repo.save_denied(call["name"], tenant)
                self._metrics.tool_denied.inc(call["name"])
                results.append(self._deny_result(call))
                continue

            t0 = time.perf_counter()
            try:
                args = tool.args_schema.model_validate(call["args"])
                view = await tool.handler(**args.model_dump(), tenant=tenant)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                await self._trace_repo.save_ok(
                    call["name"], tool.bounded_context, args, view, latency_ms
                )
                results.append(self._ok_result(call, view))
            except Exception as exc:
                await self._trace_repo.save_error(call["name"], exc)
                self._metrics.tool_error.inc(call["name"])
                results.append(self._err_result(call, str(exc)))
        state["tool_results"] = results
        state["pending_tool_calls"] = []
        return state
```

### 5.2 ACL 防腐层（版本校验）

```python
# app/infrastructure/acl/process_management.py
class ProcessManagementAclClient:
    """工艺管理上下文只读 ACL，强制版本锚点。"""

    async def query_process_route(
        self, route_id: str, version: str, tenant: TenantContext
    ) -> ProcessRouteView:
        if not version:
            raise ValueError("version 必填，禁止查询无版本工艺（§5.1）")
        resp = await self._http.get(
            f"/api/process-routes/{route_id}",
            params={"version": version},
            headers=tenant.headers(),
            timeout=2.0,
        )
        resp.raise_for_status()
        dto = ProcessRouteDTO.model_validate(resp.json())
        if dto.status != "ACTIVE":
            raise ValueError(f"工艺版本 {version} 非生效状态: {dto.status}")
        return ProcessRouteMapper.to_view(dto)
```

外部 DTO 不进入能力核心，只暴露 `ProcessRouteView`——防腐层的核心职责。

### 5.3 Agent 装配与调用

```python
# app/ai/diagnosis_agent.py
def build_diagnosis_agent(model, toolkit, deps) -> Agent:
    """诊断能力 = 统一 Agent 类（内置 ReAct）+ 只读 Toolkit 切片。"""
    return Agent(
        name="diagnosis",
        model=model,                                   # AgentScope Model 抽象，可插拔 provider
        toolkit=toolkit.tools_for("diagnosis", deps.tenant),  # 只读工具切片
        session_service=deps.session_service,          # AgentState + SessionService（Redis-backed）
        permission=deps.permission,                    # 每次工具执行前过 Permission 拦截
        max_steps=20,                                  # 步数与工具调用预算上限
        tracing=deps.tracing_middleware,               # TracingMiddleware 注入 trace
    )
```

被编排调用时（共用一条 trace）：

```python
# 编排 flow 中的非确定能力节点
async def root_cause_node(state: OrchestrationState) -> OrchestrationState:
    result = await runtime.invoke(
        "diagnosis",
        {
            "mode": "structured_exception",
            "exception": {
                "type": "TOOLING_MISMATCH",
                "expected": state["expected"],
                "actual": state["actual"],
                "code": state["mismatch_code"],
            },
        },
        tenant=state["tenant"],
        parent_session_id=state["session_id"],   # 关键：跨能力同一条链
    )
    state["agent_hypothesis"] = result["hypotheses"]
    state["agent_confidence"] = result["confidence"]
    state["action_card"] = build_disposition_card(result)
    return state
```

### 5.4 system prompt 要素（诊断）

```text
你是 MES 车间根因诊断助手。基于只读工具按 5M1E 给出根因假设排序。
约束：
1. 只能调用提供的工具，不得编造数据。
2. 查工艺必须带版本锚点。
3. 每个假设必须引用工具返回的证据（trace_id）。
4. 取证不足时降低置信度并列出仍需人确认的疑点，不得硬答。
5. 输出严格遵循 DiagnosisReport JSON 结构。
```

---

## 6. 兜底与可观测（通用底座上的能力特有项）

底座事实源见 [Agent可观测性-设计与实现方案.md](../Agent可观测性-设计与实现方案.md)。诊断特有的兜底：

- `confidence < 0.5` → `needs_human_review`，只推工程师不推操作工。
- 连续 3 次工具失败 → 终止会话转人工，不空转消耗。
- 超步数预算 / 超时 → 返回 `DiagnosisReport.partial(...)`"诊断未完成，已转人工"，不硬答。
- 主动触发（`defect_rate.spike` 等只读事件）同样落 report 并推工程师，不自动触发任何写。

---

## 7. 实现步骤

1. 装配 ReAct Agent + 一个 LLM provider（AgentScope `Model` 抽象可插拔）。
2. 实现 2–3 个核心 toolset（过点 / 工艺 / RAG 追溯读模型），跑通提问式闭环。
3. 落 `ToolExecHook`：权限过滤 + trace 落库 + 指标埋点。
4. 固化 `DiagnosisReport` / `Hypothesis` / `FiveM1ECategory` 与解析器。
5. 补全 `open_questions` 与置信度阈值兜底。
6. 增结构化异常入口（`mode=structured_exception`），供编排能力调用。
7. 接领域事件做主动诊断试点（只订阅只读事件）。
8. 工具结果缓存与限流，降低下游压力。
9. 沉淀诊断评测集（典型不良场景 + 预期根因 + 证据），回归 prompt / 模型变更。

---

## 8. 约束落地检查清单

- [ ] `CapabilitySpec(writes_allowed=False)`，工具全为 `read_only=True`，启动断言生效。
- [ ] 诊断 ACL 客户端无 `create/update/delete/submit/release/issue/save` 动词。
- [ ] `query_process_route` 强制版本入参，ACL 校验 `ACTIVE`。
- [ ] 工具调用前按 `TenantContext` 过滤，拒绝记录可观测。
- [ ] 过点 toolset 只含 `query_*`，无放行 / 拦截类工具（全局前缀断言）。
- [ ] 每步工具调用落 `agent_step_record`，`traceparent` 透传下游 Java 服务。
- [ ] 报告带置信度与 disclaimer，`< 0.5` 转人工不展示给操作工。
- [ ] 单工具 ≤2s、整会话 ≤60s、步数与工具调用预算上限 ≤20。
- [ ] 被编排调用时 `parent_session_id` 落库，跨能力同 trace。
- [ ] 主动触发只订阅只读事件，产出仍需人确认。

---

## 9. 面试防守 Q&A

**Q：诊断能力为什么不单独做成一个 Agent？**
A：因为它不只被"提问"用。换线 mismatch 后判根因、客诉追溯做 5M1E 排序，都要同一种能力——开放式取证循环。做成独立 Agent 就有两个后果：编排里要判根因时只能"远程调一个服务"或"复制一份推理逻辑"；会话表和 trace 断成两截。现在它是**一个可被 flow `await` 复用的能力（统一 `Agent` 的只读 ReAct 循环）**，编排用它只要换入参（结构化异常），并且 `parent_session_id` 把两条链串在同一条 trace 上。

**Q：怎么保证诊断不会基于失效工艺给根因？**
A：`query_process_route` 强制版本入参，ACL 层校验 `ACTIVE`，否则返回错误让模型重试。过点记录本身绑工艺版本（§5.1），模型从过点记录拿到的版本就是当时生产用的版本——版本一致性是从领域模型兜上来的，不是 Agent 自己保证的。

**Q：Agent 调这么多上下文，权限怎么管？**
A：工具注册时声明 `required_tenant_scopes`，`ToolExecHook` 调用前按会话 `TenantContext` 过滤，不匹配直接拒绝并落 trace——权限在调用前过滤，不是答完再裁剪。限界上下文边界本身就是天然权限切分面，每个上下文一个 toolset。

**Q：诊断失败会不会误导操作工？**
A：报告带置信度，`< 0.5` 标 `needs_human_review` 只推工程师；工具连续失败 3 次终止转人工；报告含 disclaimer。诊断全程只读，最坏情况是"没诊断出来"，不产生写副作用——和 MES 防错理念一致，宁可拦下让人判。

**Q：为什么把追溯工具注册在首位？**
A：追溯走 MySQL 关系型读模型（`trace_lookup` → 返回 `Evidence[]` + 三段锚点，整次请求用 `evidence_set_id` 统一回查），版本锚点在写入证据集时就和批次/工单/工序钉在一起，一次查询就能拿到"当时生产用的工艺版本 + 跨上下文证据"，避免模型多次往返后才对齐版本。这正是版本锚点三段传递链（诊断 → 草稿 → 写意图）的第一环。固定关联查询计划覆盖不足时再降级到各上下文 REST 只读补齐。这是"先证据集后 REST"的顺序，不是二选一；**不引入图数据库/Cypher/图投影**（[制造追溯-需求与实现效果.md](../../制造追溯-需求与实现效果.md) §1.3、ADR-13）。

---

## 10. 一句话定位

"诊断能力是同一个 ReAct 循环（统一 `Agent`）的两种用法——工程师提问时它做多步只读推理，编排的异常分支里它拿结构化 mismatch 直接判根因。全程只读、工具对齐 14 个限界上下文、查工艺强制版本锚点、输出带置信度的 5M1E 假设而非结论，被草稿与编排复用时只换入参不复制逻辑，一条 trace 串到底。"
