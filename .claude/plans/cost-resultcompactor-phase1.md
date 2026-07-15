# cost 子系统第一阶段：ResultCompactor 接入 ReAct + 修 #15

## 目标
解决清单 #25 的第一阶段：把 `ResultCompactor` 从悬空状态接入 ReAct 主流程——工具结果**回灌 LLM 前压缩**（字段白名单裁剪 + 列表截断），直接省 token，顺带修 #15（无白名单工具整包透传缺口可见化）。其余 6 个 cost 组件（ModelRouter/CacheControl/EarlyStopDetector/PhaseToolBinder/ToolResultCache/EvalGate）维持悬空并标注未上线，待 EvalGate 评测数据源 / provider 目标就绪后后续阶段再接。**硬约束：不破坏诊断护栏 `_guard_no_evidence` 与证据链完整性。**

## 现状（已核实）
- `ResultCompactor` 有边界单测、逻辑完备，但 `app/` 下零调用；`tool_node.py:92` 注释"可由 ResultCompactor 压缩"从未执行。
- ToolNode 当前把 `{"trace_id": tid, "data": view_dict}` **整包**放进 tool 消息喂 LLM（[tool_node.py:93](factorybot/app/infrastructure/ai/tool_node.py#L93)），大查询结果可能撑爆上下文（#15）。
- `_guard_no_evidence`（[graph_builder.py:180](factorybot/app/infrastructure/ai/graph_builder.py#L180)）在 finalize 时扫描 **`state.messages` 里的 tool 消息 `data`** 找不良证据（BLOCK/FAIL/DEFECTIVE/不良数>0）。这是关键约束。
- MockChatModel `_diagnosis`（[mock_chat_model.py:67-74](factorybot/app/infrastructure/ai/mock_chat_model.py#L67-L74)）从喂它的 tool 消息里只读 `trace_id`，不读 `data`。
- ObservableChatModel 是纯透传包装（[observable_chat_model.py:32](factorybot/app/infrastructure/ai/observable_chat_model.py#L32)），不解析 tool 消息内容做分支；`_est_tokens` 用 `len(content)` 估 token——压缩后估值反而更准。
- `build_react_graph` 有 4 个调用方：`build_diagnosis_graph`（诊断/C 共用）+ `RootCauseAgent`/`FaultImpactAgent`/`DraftAgent`，都接收 `(llm, registry, trace_repo, obs)`。

## 方案：思路 D —— 在 model_node 喂 LLM 前压缩 history（不动 ToolNode）

**为什么不在 ToolNode 压缩**：ToolNode 写入 `state.messages` 的 tool 消息是 `_guard_no_evidence` 的数据源。`query_traceability_graph` 的 FIELD_WHITELIST 不含 `nodes`，若在 ToolNode 压缩，`nodes` 被裁 → 护栏扫不到 nodes 里的 BLOCK 节点 → 漏判"证据不足"。思路 D 让**护栏读全文、LLM 读摘要**彻底解耦，且符合 ResultCompactor docstring"工具结果回灌前压缩"。

**压缩点**：`model_node` 构造喂 LLM 的 messages 时，对 history 中 `role==tool` 的消息解析 content → 压缩 `data`（`trace_id` 顶层保留不动）→ 重组序列化。`state.messages` 始终保持全文（ToolNode 不改），护栏/trace 读全文不受影响。

### 1. `app/infrastructure/cost/result_compactor.py` —— 修 #15
无白名单工具分支加 warning（让缺口可见），**维持透传不裁剪**（不替领域拍板"无白名单工具留哪些字段"）：
```python
from app.infrastructure.obs.logging import get_logger
...
def compact(self, tool_name, view):
    if not isinstance(view, dict):
        return {"_summary": str(view)[:200]}
    whitelist = FIELD_WHITELIST.get(tool_name)
    omitted = 0
    out: dict = {}
    if whitelist:
        for k in whitelist:
            if k in view: out[k] = view[k]
        omitted = len(view) - len(out)
    else:
        # #15: 无白名单工具告警，维持透传（逐工具白名单补全留后续领域决策）
        get_logger("result_compactor").warning(
            "cost.result_compactor.no_whitelist", tool_name=tool_name,
            hint=f"工具 {tool_name} 未配 FIELD_WHITELIST，结果整包透传（可能烧 token）",
        )
        out = dict(view)
    # 列表截断逻辑不变 ...
```

### 2. `app/infrastructure/ai/react_graph.py` —— 接入压缩
`build_react_graph` 加可选参数 `result_compactor=None`（None=不压缩，向后兼容）；新增**模块级** `_compact_tool_history(history, compactor)`（模块级以便单测）；`model_node` 中 `messages.extend(history)` → `messages.extend(_compact_tool_history(history, result_compactor))`：
```python
def _compact_tool_history(history, compactor):
    """喂 LLM 前：压缩 tool 消息的 data（trace_id 保留），其余原样。
    state.messages 仍存全文，护栏/trace 读全文不受影响。"""
    if compactor is None:
        return history
    out = []
    for m in history:
        if m.get("role") != "tool":
            out.append(m); continue
        try:
            payload = json.loads(m.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            out.append(m); continue
        data = payload.get("data")
        if data is not None:
            payload["data"] = compactor.compact(m.get("name", ""), data)
        out.append({**m, "content": json.dumps(payload, ensure_ascii=False, default=str)})
    return out
```

### 3. 透传 `result_compactor`（机械改签名，DI 正路）
- `graph_builder.py`：`build_diagnosis_graph(..., result_compactor=None)` → 透传 `build_react_graph`。
- `orchestration/agents/__init__.py`：`build_agent_registry(..., result_compactor=None)` → 传给各 agent 构造。
- `root_cause_agent.py` / `fault_impact_agent.py` / `draft_agents.py`：`__init__(..., result_compactor=None)` → 透传 `build_react_graph`。
- `traceability_agent.py`：`__init__(..., result_compactor=None)` → 透传 `build_diagnosis_graph`。
- `diagnosis_service.py`：`__init__(..., result_compactor=None)` → `diagnose()` 内透传 `build_diagnosis_graph`。

### 4. `app/container.py` —— 装配单例
```python
from app.infrastructure.cost.result_compactor import ResultCompactor
...
# 成本
self.eval_gate = EvalGate()
self.model_router = ModelRouter(...)
self.result_compactor = ResultCompactor()        # 新增
...
self.diagnosis_service = DiagnosisService(
    self.diagnosis_registry, self.llm, self.tool_trace_repo, self.obs,
    result_compactor=self.result_compactor,      # 新增
)
self.agents = build_agent_registry(
    self.llm, self.orchestration_registry, self.diagnosis_registry,
    self.tool_trace_repo, self.obs, self.result_compactor,  # 新增
)
```

### 5. 标注其余组件未上线
`app/infrastructure/cost/__init__.py` 顶部 docstring 补一行总览：仅 `ResultCompactor` 已接入主流程（ReAct 回灌前压缩），其余 6 组件待 EvalGate 评测数据源 / provider 目标就绪后接入。

### 6. 测试
- `tests/test_cost.py` 补：无白名单工具 `compact` 发 warning（caplog 断言 `cost.result_compactor.no_whitelist`）。
- 新增 `tests/test_react_graph_compaction.py`：
  - `test_compact_tool_history_compresses_data_keeps_trace_id`：构造 tool 消息（`data` 含白名单外字段），跑 `_compact_tool_history`，断言 `data` 被裁、`trace_id` 保留、非 tool 消息原样。
  - `test_compact_tool_history_none_passthrough`：`compactor=None` 原样返回。
  - `test_guard_no_evidence_reads_full_messages_unaffected`：构造追溯图含 BLOCK 节点的 tool 消息，经 `_compact_tool_history` 后**原文 history**仍能被 `_has_defect_evidence` 检出（确认压缩不污染 state.messages）。
- 跑全量既有测试：`test_cost.py` / `test_orchestration_changeover.py` / `test_real_mode_paths.py` 等全过（护栏读全文、mock 读 trace_id，均不受影响）。

## 风险与缓解
- **护栏被压缩误伤**：思路 D 让护栏读 `state.messages` 全文、LLM 读压缩副本，二者解耦；新增护栏端到端断言兜底。
- **mock 模式行为变化**：MockChatModel 只读 `trace_id`（顶层保留），不读 `data`，压缩不影响；跑现有诊断/编排测试确认。
- **real 模式诊断质量**：有白名单的 3 个工具（pass_records/test_results/traceability_graph）压缩后模型看到的是关键字段摘要，详情靠 `trace_id` 查 trace。FIELD_WHITELIST 是既有领域设计，本阶段不改。无白名单工具维持透传（只告警），不冒险裁剪。
- **透传改动面（~9 文件签名）**：均为机械加 `result_compactor=None` 透传，低风险；符合项目既有 DI 风格（container 已装配 eval_gate/model_router）。
- **备选（若嫌透传重）**：`build_react_graph` 内部默认 `ResultCompactor()`（3 文件改动），代价是失去 DI/可配置性、与 container cost 装配区不一致。本 plan 选注入式。

## 不在范围（后续阶段）
- ModelRouter.route() 接入（被 LLM 单例→多实例架构 + provider 模型映射 + EvalGate 数据源三重阻塞）。
- EarlyStopDetector 接 ReAct（需加 state 证据计数通道）。
- CacheControl（强依赖 anthropic provider）。
- PhaseToolBinder / ToolResultCache（默认关闭/灰度，语义风险）。
- EvalGate 评测数据源就绪与 raise 门禁。
- 压缩省 token 量的 metric（属 P1 #7-11 可观测块，`_est_tokens` 估值已自然反映效果）。
- 逐工具 FIELD_WHITELIST 补全（领域决策，后续）。
