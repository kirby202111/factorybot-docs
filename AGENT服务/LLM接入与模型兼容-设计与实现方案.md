# LLM 接入与模型兼容 - 设计与实现方案

> 面向场景：factorybot 的 LLM 抽象层如何接入真实 DeepSeek，以及**思考模型（reasoning model）的兼容要点**。
> **核心纪律**：LLM 可插拔（`ChatModel` Protocol），provider 无关；真实模型经 `ObservableChatModel` 统一埋观测（token/延迟/模型/prompt_version）。mock 模式离线可跑，real 模式按 provider 切换，二者消息协议完全一致。
> **关联实现**：`app/infrastructure/ai/`（抽象层 + 工厂）、`app/config.py`（配置）、`app/application/l3_orchestrator.py`（多 interrupt resume）。

---

## 0. 设计目标

| 目标 | 手段 |
|------|------|
| 模型可插拔 | `ChatModel` Protocol：`ainvoke`（驱动 ReAct）+ `ainvoke_structured`（产出 Pydantic 实例） |
| provider 无关 | `ObservableChatModel` 装饰任意模型，观测是只读旁路，异常不反噬业务 |
| mock 优先 | 无 API Key 用 `MockChatModel`（确定性，离线驱动 ReAct + 结构化输出） |
| 真实接入 | DeepSeek 兼容 OpenAI 协议，复用 `langchain-openai`，无需额外依赖 |
| 消息解耦 | 消息用 plain dict（role/content），避免与具体框架消息类型耦合 |

---

## 1. LLM 抽象层

```
ChatModel (Protocol)
  ├─ MockChatModel         # 确定性替身，离线可跑
  └─ _LangChainAdapter     # 包装 langchain BaseChatModel（OpenAI/DeepSeek/Anthropic）
        ↑
  ObservableChatModel      # 装饰器：埋 llm_call_log + prometheus + OTel span
        ↑
  get_llm(obs)             # 单例工厂，按 settings 产出
```

- `ainvoke(messages, tools)`：ReAct 主循环。`tools` 为 OpenAI function schema，模型返回 `ModelResponse(content, tool_calls, finish_reason)`。
- `ainvoke_structured(messages, schema)`：产出 Pydantic 实例（L2 草稿）。底层 `with_structured_output(schema)`。
- 消息助手：`sys_msg` / `user_msg` / `assistant_msg(content, tool_calls)` / `tool_msg(name, content, tool_call_id)`。

---

## 2. DeepSeek 接入

DeepSeek API **兼容 OpenAI 协议**，直接复用 `ChatOpenAI`，仅替换 `base_url`：

```python
# app/infrastructure/ai/llm_factory.py
if s.llm_provider == "deepseek":
    from langchain_openai import ChatOpenAI
    model = s.llm_model if s.llm_model and not s.llm_model.startswith("claude") else "deepseek-chat"
    return _LangChainAdapter(ChatOpenAI(
        model=model, api_key=s.llm_api_key,
        base_url=s.llm_base_url or "https://api.deepseek.com",
    ))
```

- **无需额外依赖**：`langchain-openai` 已在 `pyproject.toml` 的 `llm` extra。
- **model 回退**：`llm_model` 未配置或仍是 claude 默认值时回退 `deepseek-chat`，避免误发模型名。
- **可选模型**：`deepseek-chat`（V3 通用）、`deepseek-v4-flash`（思考模型，见 §4）。

---

## 3. LLM 与 RUN_MODE 解耦（关键设计决策）

**问题**：`Settings.is_mock` 原是单一总开关，同时控制 LLM 与 ACL（MES 数据源）。`RUN_MODE=real` 会把 ACL 也切真实 HTTP，但本地无 MES 后端，工具调用全失败。

**决策**：LLM 真实与否**只由 `LLM_PROVIDER` + `LLM_API_KEY` 决定**，不再受 `RUN_MODE` 影响：

```python
# llm_factory._build_inner_model()
if s.llm_provider == "mock" or not s.llm_api_key:   # 去掉 s.is_mock
    return MockChatModel()
```

**意义**：可 `RUN_MODE=mock`（ACL/MySQL/Redis/Kafka 走进程内 mock fixtures）+ 真实 DeepSeek 同时生效--本地无 MES 后端时，LLM 真实推理 + 数据源 mock，端到端可跑。`is_mock` 仍控制 ACL/存储/ModelRouter 启动断言的宽松度。

| RUN_MODE | LLM_PROVIDER | ACL/存储 | LLM | 适用 |
|----------|--------------|----------|-----|------|
| mock | mock | mock | MockChatModel | 离线开发/CI |
| mock | deepseek | mock | 真实 DeepSeek | **本地验证 LLM（无 MES 后端）** |
| real | deepseek | real | 真实 DeepSeek | 接入真实 MES 后 |

---

## 4. 思考模型兼容（deepseek-v4-flash）

`deepseek-v4-flash` 是**思考模型（reasoning）**，与通用模型（`deepseek-chat`）能力边界不同。三条限制 + 对应适配：

| 限制 | 错误现象 | 适配 |
|------|----------|------|
| 不支持 `json_schema` response_format | `400: This response_format type is unavailable now` | 结构化输出改 `json_mode` |
| 不支持 `tool_choice`（强制） | `400: Thinking mode does not support this tool_choice` | 工具调用用 `bind_tools`（`tool_choice=auto`） |
| `json_object` 模式要求 prompt 含 "json" | `400: Prompt must contain the word 'json'` | 注入含 "JSON" 的 schema 描述 |

### 4.1 结构化输出（L2 草稿）

`with_structured_output` 在新版 `langchain-openai` 默认用 `json_schema`，DeepSeek 思考模型不支持。`ainvoke_structured` 对 ChatOpenAI 系改用 `json_mode` + 注入 schema 提示：

```python
# _LangChainAdapter.ainvoke_structured
if isinstance(self._lc, ChatOpenAI):
    kwargs["method"] = "json_mode"
    schema_hint = ("请严格输出符合如下 JSON Schema 的 JSON 对象，仅输出 JSON，不要解释或 markdown：\n"
                   + json.dumps(schema.model_json_schema(), ensure_ascii=False))
    lc_msgs.append(SystemMessage(content=schema_hint))   # 满足 "prompt 含 json" 约束
structured = self._lc.with_structured_output(schema, **kwargs)
```

- `GateManager` 已兼容 resume value 的 dict 形态（`isinstance(confirmation, dict)` 分支），dict 便于 checkpointer 序列化。
- 仅 ChatOpenAI 系（openai/deepseek）走 json_mode；Anthropic 等保留默认 method。

### 4.2 工具调用（L1 ReAct）

ReAct 用 `bind_tools(tools)`（`tool_choice` 默认 `auto`，模型自主决定是否调工具），思考模型支持。L1 诊断多步 ReAct 验证通过：DeepSeek 主动调 `query_traceability_graph` → `query_pass_records` → 输出报告。

---

## 5. 多步 ReAct 的 tool_call_id 贯穿

**问题**：`_LangChainAdapter._to_lc` 原丢弃 assistant 消息的 `tool_calls` 和 tool 消息的 `tool_call_id`。真实模型多步 ReAct 第二轮（喂回工具结果）时，`tool` 消息无对应 `tool_call_id` → API 400。

**修复**：`tool_call_id` 贯穿全链路：

```
ModelResponse.tool_calls[i].id  (模型返回)
  → assistant_msg(tool_calls=[{id,...}])        (agent_node 透传)
  → pending_tool_calls[i].id                    (state)
  → tool_msg(tool_call_id=id)                   (ToolNode 产出)
  → AIMessage(tool_calls=[{id,type="tool_call"}]) + ToolMessage(tool_call_id=id)  (_to_lc 还原)
```

- `ToolCall` 加 `id: str = ""`（[base.py](../factorybot/app/infrastructure/ai/base.py)）。
- `ToolNode` 从 `pending_tool_calls` 提取 `id` 透传给 `_tool_msg`。
- `_to_lc` 还原 assistant 的 `tool_calls`（含 `id` + `type="tool_call"`）和 tool 消息的 `tool_call_id`。
- Mock 模型用确定性 id（`call_mock_{step}`），不影响 mock 行为（mock 不经 `_to_lc`）。
- 回归测试：`tests/test_tool_call_pairing.py` 校验配对不变量。

---

## 6. L1 输出 schema 对齐

**问题**：DeepSeek 自由发挥字段名（`m1e_dimension` 代替 `category`、`confidence` 用中文"高"），`json.loads` 解析后 `DiagnosisReport` 校验失败。

**修复**（[graph_builder.py](../factorybot/app/infrastructure/ai/graph_builder.py)）：

1. **`_extract_json` 容错**：兼容 markdown ```` ```json ```` 代码块 + 前后解释文本，提取首个平衡 `{...}`。
2. **强化 `L1_SYSTEM_PROMPT`**：明确字段定义 + 枚举（`category` 仅 Man/Machine/Material/Method/Measurement/Environment）+ 类型（`confidence` 0-1 浮点，禁止"高/中/低"）+ 示例 JSON。
3. **失败日志**：`_parse_report` 解析失败时 `get_logger("diagnosis").warning("llm.output.non_json", content_preview=...)`，便于排查模型实际输出。

> 思考模型擅长按明确指令输出，强化 prompt + 示例后 schema 遵循率显著提升。若仍不稳定，终极方案是完成步改用 `ainvoke_structured(DiagnosisReport)`（function calling / json_mode 强 schema）。

---

## 7. L3 多 interrupt resume（并行 gate）

**问题**：`fault_response` 图 `gate_repair ‖ gate_isolation` **并行**，两个 gate 都 `interrupt(value=card)` → 2 个 pending interrupt。且两 gate 的 `step`（REPAIR/ISOLATION）与 `writes_via` 不同，单 token 的 `action` 无法同时匹配。`Command(resume=token)` 报 `must specify the interrupt id when resuming`。

**修复**（[l3_orchestrator.py](../factorybot/app/application/l3_orchestrator.py)）：

```python
# 提取所有 pending interrupt 的 (id, card)
pending = _extract_pending_interrupts(state)
if len(pending) > 1:
    resume_value = {}
    for iid, card in pending:
        tok = await self._store.issue(session_id, card.step, approved, user_id,
                                      action=card.writes_via_action())  # 各 gate 匹配 token
        resume_value[iid] = asdict(tok)   # dict 形态便于 checkpointer 序列化
else:
    resume_value = token                  # 单 interrupt 保持原行为
await graph.ainvoke(Command(resume=resume_value), config=config)
```

- **每个 interrupt 签发匹配 token**：`action=card.writes_via_action()`，确保各 gate `valid_for` 通过。
- **dict 形态**：`asdict(token)` 便于 msgpack 序列化，`GateManager` 兼容 dict 解析。
- **单 interrupt 不变**：保持 token 对象，不破回归。
- **语义妥协**：并行 gate 需 LangGraph 一次 resume 所有 interrupt，故一个 confirm 覆盖该步所有并行 gate（同 approved）。

---

## 8. 配置与运行

`.env`（见 `.env.example`）：

```env
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-v4-flash      # 或 deepseek-chat（通用，无思考模型限制）
LLM_BASE_URL=https://api.deepseek.com
RUN_MODE=mock                    # ACL/存储/Kafka 走 mock；LLM 由 provider 决定（真实 DeepSeek）
```

- 安装：`pip install -e ".[llm]"`（含 `langchain-openai`）。
- 启动：`python main.py`（默认 8000 端口）。
- 复测：`http://127.0.0.1:8000/docs` Swagger UI，或 curl（请求体见 `factorybot/req*.json`）。

**测试隔离**：`tests/conftest.py` 顶部强制 `LLM_PROVIDER=mock`（env var 优先 `.env`），避免 `.env` 配真实 provider 污染测试（否则 `test_l1_diagnosis` 期望 mock 固定输出却走真实模型，既失败又烧 token）。

---

## 9. 踩坑记录（面试可讲）

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | L1 返回 `LLM 输出非合法 JSON` | DeepSeek 输出 markdown fence / 带解释文本 | `_extract_json` 容错 + 强化 prompt |
| 2 | L1 返回 `DiagnosisReport 校验失败` | DeepSeek 自创字段名（`m1e_dimension`）、`confidence` 用中文 | prompt 明确字段 + 枚举 + 示例 |
| 3 | L2 `500: response_format type unavailable` | 思考模型不支持 `json_schema` | `ainvoke_structured` 改 `json_mode` |
| 4 | L2 `500: Thinking mode does not support tool_choice` | 思考模型不支持强制 `tool_choice`（function_calling） | json_mode 不涉及 tool_choice |
| 5 | L2 `500: Prompt must contain the word 'json'` | `json_object` 模式要求 prompt 含 "json" | 注入含 "JSON" 的 schema 描述 |
| 6 | L1 多步 ReAct 第二轮 API 400 | `_to_lc` 丢弃 `tool_calls` / `tool_call_id` | id 贯穿全链路（§5） |
| 7 | L3 resume `must specify interrupt id` | 并行 gate 多 interrupt，单 token 不匹配 | `_extract_pending_interrupts` + `{iid: token}`（§7） |
| 8 | 测试失败 + 烧 token | `.env` 真实 provider 污染测试 | conftest 强制 mock（§8） |

---

## 10. 演进方向

- **结构化输出统一**：L1 完成步可改 `ainvoke_structured(DiagnosisReport)`，用 function calling/json_mode 强 schema，替代 prompt + `_extract_json`。
- **cascading 降级**：`ModelRouter` 已留 `fallback: deepseek` 路由，主模型失败可降级。
- **EvalGate 门禁**：真实模型上线前须过评测门禁（`ModelRouter.validate_on_startup` 当前 mock 模式豁免，生产需取消注释 raise）。
- **思考模型分离**：若思考模型（v4-flash）用于推理、通用模型（deepseek-chat）用于结构化输出，可按 capability 路由不同模型（`ModelRouter` 已支持）。
