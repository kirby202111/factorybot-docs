# L1 诊断型 Agent —— LangGraph 架构与流程图

> 本文从 [L1诊断型Agent-实现方案.md](L1诊断型Agent-实现方案.md) 抽取 LangGraph 相关内容，集中呈现**总体架构图**与 **ReAct 推理流程图**，便于聚焦理解 LangGraph 在 L1 中的落地形态。
> 配套说明见实现方案 §2.2、§3、§5.1、§7.4。

---

## 1. 为什么选 LangGraph

L1 的核心是**多步规划**：模型需根据上一步工具返回决定下一步查什么。LangGraph 的 `StateGraph` 把"模型思考节点 -> 工具执行节点 -> 回模型"做成显式图，可对每条边加条件路由、超时、递归上限。

- LangChain 的 `AgentExecutor` 是黑盒循环，难做细粒度权限拦截与 trace；裸调模型 API 要自己实现 tool-calling 循环、重试、参数校验，重复造轮子。
- LangGraph 的 `recursion_limit` 直接对应"最大步数"红线，硬上限靠框架兜底，不是口头约束。
- 配合 `checkpointer`（`SqlSaver`）可把中间状态落 MySQL，进程重启从断点续跑。

---

## 2. 总体架构图

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

**图例要点**

- `LangGraph StateGraph` 内部只有两个核心节点：`model_node`（模型思考 + 产出 tool calls）与 `tool_node`（执行工具 + 回灌结果），二者循环驱动。
- `DiagnosisService` 负责**构建图 + 驱动循环**，不是用 if-else 串工具调用——这是 L1 不写成流水账的关键。
- 工具调用经 `ToolRegistry`（只读白名单）-> ACL 防腐层 -> 下游 Java 服务的只读 REST；`tool_node` 内部做权限过滤、trace 落库、指标埋点。

---

## 3. ReAct 推理流程图

用 LangGraph 的 `StateGraph` 构建 ReAct 图：`model_node` ↔ `tool_node`。模型无 tool call 时收口，输出根因报告。

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

**节点与边的约束**

| 项 | 取值 | 落地 |
|----|------|------|
| 最大步数 | `recursion_limit=20` | 一次 model+tool 算 2 步，即最多 10 次工具调用；超过抛 `GraphRecursionError` 被捕获，返回"诊断未完成转人工" |
| 单工具超时 | ≤2s | httpx timeout |
| 整会话超时 | ≤60s | `asyncio.wait_for` 包住图驱动 |
| 中断恢复 | `SqlSaver` checkpointer | 中间状态落 MySQL，进程重启可从断点续跑 |
| 收口条件 | 模型无 tool call | 输出 `DiagnosisReport`（Pydantic 校验） |

---

## 4. 图驱动代码骨架（对应实现方案 §7.4）

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
                    config={
                        "recursion_limit": 20,
                        "configurable": {"thread_id": session.id},
                    },
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
```

- `config={"recursion_limit": 20, "configurable": {"thread_id": session.id}}`：`recursion_limit` 是最大步数红线；`thread_id` 让 `SqlSaver` checkpointer 按会话持久化与恢复。
- `asyncio.wait_for(..., timeout=60.0)`：整会话超时兜底，与 LangGraph 内部 `recursion_limit` 形成"步数 + 时长"双闸门。
- `GraphRecursionError` 与 `TimeoutError` 都不硬答，转人工——与 L1 全程只读、宁可让人判的防错理念一致。

---

## 5. 工具执行节点（LangGraph `tool_node` 落地）

`tool_node` 是 LangGraph 图里的执行节点，L1 在此注入权限拦截、trace、指标三件事——这正是选 LangGraph 而非黑盒 `AgentExecutor` 的核心收益（实现方案 §7.2）：

```python
# app/infrastructure/ai/tool_node.py
class ToolNode:
    """LangGraph 工具执行节点：权限校验 -> 调 ACL -> 落 trace。"""

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

---

## 6. 一句话定位

L1 用 LangGraph 的 `StateGraph` 把 ReAct 循环显式化为 `model_node ↔ tool_node` 两节点图，靠 `recursion_limit` 锁最大步数、`SqlSaver` 做中断恢复、`asyncio.wait_for` 兜整会话超时，并在 `tool_node` 注入权限拦截与 trace——多步规划是图驱动而非流水账。
