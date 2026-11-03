# LLM 接入与模型兼容 - 设计与实现方案

> 面向场景：factorybot 的 LLM 抽象层如何接入真实 DeepSeek，以及**思考模型（reasoning model）的兼容要点**。
>
> **口径纪律（重要）**：本篇是**设计目标，尚未落地**。代码仓 `factorybot` 目前 `src/` 下只有 `agents/tool_test.py`、`api/{health,index,tool_test}.py`、`schemas/tool_test.py`、`tools/get_current_time.py`、`factory.py`，**不存在 `app/infrastructure/ai/` 等目录**，本文出现的类名/文件路径是设计落点或历史草稿实现，不代表已上线代码。目标底座为 **AgentScope 2.0**：provider 可插拔统一走框架 **`Model` 抽象 + `Credential`**（如 `agentscope.model.DashScopeChatModel` + `DashScopeCredential`；2.0 需显式 Credential，1.x 直传 `api_key` 的写法作废），OpenAI 兼容协议（含 DeepSeek）经同一 `Model` 抽象接入。本文中 `ChatModel` Protocol / `_LangChainAdapter` / `langchain-openai` 一系是**历史实现路线与 provider 兼容要点的载体，不是目标架构的框架 API**；思考模型的三条 provider 限制（§4）与 tool_call_id 配对不变量（§5）是 provider 事实，换底座后仍然成立。
> **核心纪律**：LLM 可插拔，provider 无关；真实模型经观测装饰器（本项目自定义 `ObservableChatModel`，非框架 API）统一埋观测（token/延迟/模型/prompt_version）。mock 模式离线可跑，real 模式按 provider 切换，二者消息协议完全一致。
> **设计落点（尚未落地）**：LLM 抽象层与工厂（`Model` 适配 + 观测装饰）、配置项、`flows/` 编排的并行 gate 确认续跑（见 §7）。

---

## 0. 设计目标

| 目标 | 手段 |
|------|------|
| 模型可插拔 | 目标形态：框架 **`Model` 抽象 + `Credential`**（`agentscope.model.*`，已核实）；本项目在其上包一层 `ChatModel` Protocol：`ainvoke`（驱动 ReAct）+ `ainvoke_structured`（产出 Pydantic 实例） |
| provider 无关 | `ObservableChatModel`（本项目自定义装饰器）包任意模型，观测是只读旁路，异常不反噬业务 |
| mock 优先 | 无 API Key 用 `MockChatModel`（确定性，离线驱动 ReAct + 结构化输出） |
| 真实接入 | DeepSeek 兼容 OpenAI 协议，经 `Model` 抽象的 OpenAI 兼容 provider 接入（历史草稿用 `langchain-openai`，非目标 API） |
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

**目标形态（尚未落地）**：底座替换为框架 `Model` 抽象 + `Credential`，本项目的 `ObservableChatModel` 观测装饰器与 mock 替身继续包在它外面；下图里的 `_LangChainAdapter` 是历史草稿实现。

- `ainvoke(messages, tools)`：ReAct 主循环。`tools` 为 OpenAI function schema（目标形态为 `Toolkit` 工具描述），模型返回 `ModelResponse(content, tool_calls, finish_reason)`。
- `ainvoke_structured(messages, schema)`：产出 Pydantic 实例（草稿）。目标形态用框架的**结构化输出 / Content Block（Pydantic `Msg`）**；历史草稿底层为 `with_structured_output(schema)`，非框架 API。
- 消息助手：`sys_msg` / `user_msg` / `assistant_msg(content, tool_calls)` / `tool_msg(name, content, tool_call_id)`。

---

## 2. DeepSeek 接入

DeepSeek API **兼容 OpenAI 协议**，仅替换 `base_url` 即可复用 OpenAI 兼容 provider。

**目标形态（尚未落地）**：经框架 `Model` 抽象 + 显式 `Credential` 接入（OpenAI 兼容 provider；已核实的原生实现示例为 `agentscope.model.DashScopeChatModel` + `DashScopeCredential`，1.x 直传 `api_key` 的写法在 2.0 作废）：

```python
# 设计骨架（本项目 provider 工厂，尚未落地）
if s.llm_provider == "deepseek":
    model = s.llm_model if s.llm_model and not s.llm_model.startswith("claude") else "deepseek-chat"
    return build_openai_compatible_model(        # 落到 AgentScope Model 抽象
        model=model, credential=Credential(api_key=s.llm_api_key),
        base_url=s.llm_base_url or "https://api.deepseek.com",
    )
```

> 历史草稿写法（`from langchain_openai import ChatOpenAI` + `_LangChainAdapter`）仅作 provider 兼容要点的记录，**不是目标 API**。
- **依赖现状（已核实）**：`pyproject.toml` 依赖里**没有** `langchain-openai`，也没有 `llm` extra；`agentscope>=2.0.0` 是唯一模型底座依赖。目标形态不引入 langchain。
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

### 4.1 结构化输出（草稿）

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

- `GateManager`（本项目封装）兼容确认令牌的 dict 形态（`isinstance(confirmation, dict)` 分支），dict 便于写入 `AgentState` 后经 `SessionService` 序列化落 Redis。
- 仅 ChatOpenAI 系（openai/deepseek）走 json_mode；Anthropic 等保留默认 method。

### 4.2 工具调用（诊断能力 ReAct）

ReAct 用工具绑定（`tool_choice` 默认 `auto`，模型自主决定是否调工具；目标形态为 AgentScope `Agent` 内置 ReAct + `Toolkit` 的工具选择），思考模型支持。**设计目标（尚未在 2.0 底座复现）**：诊断多步 ReAct 走 `query_traceability`（关系型追溯，证据回查一律 `evidence_set_id` + `fetch_evidence_set`）-> `query_pass_records` -> 输出报告。

---

## 5. 多步 ReAct 的 tool_call_id 贯穿

**问题**（历史草稿排查）：provider 适配层 `_to_lc` 原丢弃 assistant 消息的 `tool_calls` 和 tool 消息的 `tool_call_id`。真实模型多步 ReAct 第二轮（喂回工具结果）时，`tool` 消息无对应 `tool_call_id` -> API 400。这是 **provider 侧事实**，换到 AgentScope `Model` 抽象后仍需保证。

**要求（设计目标）**：`tool_call_id` 贯穿全链路：

```
ModelResponse.tool_calls[i].id  (模型返回)
  -> assistant_msg(tool_calls=[{id,...}])        (Agent 节点透传)
  -> pending_tool_calls[i].id                    (AgentState)
  -> tool_msg(tool_call_id=id)                   (工具执行节点产出)
  -> provider 侧消息还原：assistant 带 tool_calls[{id,type="tool_call"}] + tool 消息带 tool_call_id
```

- `ToolCall` 加 `id: str = ""`（本项目 schemas 层字段，设计落点）。
- 工具执行节点（本项目封装）从 `pending_tool_calls` 提取 `id` 透传给 `_tool_msg`。
- provider 适配层还原 assistant 的 `tool_calls`（含 `id` + `type="tool_call"`）和 tool 消息的 `tool_call_id`。
- Mock 模型用确定性 id（`call_mock_{step}`），不影响 mock 行为（mock 不经 provider 适配层）。
- 回归测试**待建**：配对不变量（每条 `tool` 消息必须能匹配到一条带同 id 的 assistant `tool_calls`）需在 CI 里锁死。

---

## 6. 诊断能力输出 schema 对齐

**问题**：DeepSeek 自由发挥字段名（`m1e_dimension` 代替 `category`、`confidence` 用中文"高"），`json.loads` 解析后 `DiagnosisReport` 校验失败。

**修复（设计落点：诊断输出解析层，尚未落地）**：

1. **`_extract_json` 容错**：兼容 markdown ```` ```json ```` 代码块 + 前后解释文本，提取首个平衡 `{...}`。
2. **强化 `SYSTEM_PROMPT`**：明确字段定义 + 枚举（`category` 仅 Man/Machine/Material/Method/Measurement/Environment）+ 类型（`confidence` 0-1 浮点，禁止"高/中/低"）+ 示例 JSON。**注意红线**：LLM 自报的 `confidence` 只当原始信号入库，对外的置信度由规则计算并用 Platt/isotonic 在评测集上标定，**不采信模型自评**（ADR-11）。
3. **失败日志**：`_parse_report` 解析失败时 `get_logger("diagnosis").warning("llm.output.non_json", content_preview=...)`，便于排查模型实际输出。

> 思考模型擅长按明确指令输出，强化 prompt + 示例后 schema 遵循率显著提升。若仍不稳定，终极方案是完成步改用 `ainvoke_structured(DiagnosisReport)`（function calling / json_mode 强 schema）。

---

## 7. 并行 gate 的确认与续跑（设计目标，尚未落地）

**要解决的问题**：`fault_response` 场景里 `gate_repair ‖ gate_isolation` **并行**，两个 gate 各有一个待确认记录。两 gate 的 `step`（REPAIR/ISOLATION）与 `writes_via` 不同，**单个令牌里的 `action` 无法同时匹配两者**——必须为每个待确认 gate 分别签发与其动作匹配的一次性确认令牌。

> 历史草稿用图框架的"挂起/恢复"（一次调用恢复全部待确认点）实现，并踩过"恢复时必须指明是哪一个待确认点"的报错。该原语属于被替换掉的旧底座，**AgentScope 2.0 不提供图级挂起/恢复**，下面的口径是目标设计。

**目标做法**（`flows/` 请求内协同 + 框架已核实原语）：

```python
# 待确认 gate 由 AgentState 记录；每个 gate 对应一条独立的确认记录
pending = extract_waiting_gates(state)          # [(step, card), ...]
for step, card in pending:
    tok = await confirmation_store.issue(        # 令牌绑定"动作 + target + 用户"
        session_id, step, approved, user_id,
        action=card.writes_via_action(),         # 各 gate 各自匹配，互不覆盖
    )
    state.confirmations[step] = asdict(tok)      # dict 形态，便于 AgentState 经 SessionService 落 Redis
# 每个 gate 由各自的 RequireUserConfirmEvent 确认事件驱动续跑；
# 会话状态按 session_id 取回，任意副本可接管
```

- **逐 gate 签发匹配令牌**：`action=card.writes_via_action()`，确保各 gate 的 `valid_for` 校验独立通过；**令牌用后即废、过期 fail closed**，主闸在工具网关（ADR-04）。
- **dict 形态**：`asdict(token)` 便于序列化进 `AgentState`，`GateManager`（本项目封装）兼容 dict 解析。
- **单 gate 路径不变**：只有一个待确认 gate 时保持原语义，不破回归。
- **不做"一次确认覆盖多个 gate"**：并行 gate 各自独立确认、各自独立续跑——一个 confirm 只放行它绑定的那个动作。若业务上确实要"一键批准整步"，必须显式签发多枚令牌并逐条审计，不能合并成一枚。
- **跨天/跨进程的持久 gate 不在此列**：等待人工确认超过请求生命周期、需要 deadline 超时 `SUSPENDED` 与补偿的流程推进状态归工作流引擎（ADR-05）；`flows/` 只负责请求内协同，框架侧只提供 `event.RequireUserConfirmEvent` + `SessionService` + `BackgroundTaskManager`/`SchedulerManager` 这一组真实原语。

---

## 8. 配置与运行

**现状（已核实）**：`factorybot/pyproject.toml` 依赖为 `agentscope>=2.0.0` / `fastapi` / `redis` / `apscheduler` / `uvicorn`，**没有 `langchain-openai`，也没有 `[llm]` extra**；`.env.example` 当前只有 `DASHSCOPE_API_KEY` 与 Redis / MCP / Server 若干注释项，**尚无 `LLM_PROVIDER` / `RUN_MODE` 配置族**。因此本节全部是**设计目标，尚未落地**。

设计目标配置形态（待加）：

```env
LLM_PROVIDER=deepseek            # provider 选择（经 AgentScope Model 抽象的 OpenAI 兼容 provider）
LLM_API_KEY=sk-...               # 2.0 需显式 Credential，直传 api_key 的 1.x 写法作废
LLM_MODEL=deepseek-v4-flash      # 或 deepseek-chat（通用，无思考模型限制）
LLM_BASE_URL=https://api.deepseek.com
RUN_MODE=mock                    # ACL/存储/Kafka 走 mock；LLM 由 provider 决定（真实 DeepSeek）
```

- 启动：`python main.py`（现存入口，端口取 `PORT`，注释默认 8100）。
- 复测：Swagger UI `/docs` 或 curl；**历史请求体文件已不在仓库中，不再引用**。

**测试隔离（设计目标，仓库当前无 `tests/`）**：conftest 顶部强制 provider=mock（env var 优先 `.env`），避免真实 provider 污染测试（既失败又烧 token）。

---

## 9. 踩坑记录（面试可讲）

> 以下现象均为 **provider 侧事实**（DeepSeek 思考模型的协议限制），换底座后依然成立；但**修复动作对应的代码尚未落地**，历史排查是在旧底座草稿上做的，故一律按"设计目标/历史排查记录"表述。

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | 诊断能力返回 `LLM 输出非合法 JSON` | DeepSeek 输出 markdown fence / 带解释文本 | `_extract_json` 容错 + 强化 prompt |
| 2 | 诊断能力返回 `DiagnosisReport 校验失败` | DeepSeek 自创字段名（`m1e_dimension`）、`confidence` 用中文 | prompt 明确字段 + 枚举 + 示例 |
| 3 | 草稿能力 `500: response_format type unavailable` | 思考模型不支持 `json_schema` | `ainvoke_structured` 改 `json_mode` |
| 4 | 草稿能力 `500: Thinking mode does not support tool_choice` | 思考模型不支持强制 `tool_choice`（function_calling） | json_mode 不涉及 tool_choice |
| 5 | 草稿能力 `500: Prompt must contain the word 'json'` | `json_object` 模式要求 prompt 含 "json" | 注入含 "JSON" 的 schema 描述 |
| 6 | 诊断能力多步 ReAct 第二轮 API 400 | `_to_lc` 丢弃 `tool_calls` / `tool_call_id` | id 贯穿全链路（§5） |
| 7 | 并行 gate 确认时"恢复必须指明是哪一个待确认点" | 单枚令牌无法同时匹配两个 gate 的不同动作 | **目标设计**：逐 gate 签发匹配令牌，各自独立确认与续跑（§7） |
| 8 | 测试失败 + 烧 token | `.env` 真实 provider 污染测试 | conftest 强制 mock（§8） |

---

## 10. 演进方向

- **结构化输出统一**：诊断能力完成步可改 `ainvoke_structured(DiagnosisReport)`，用 function calling/json_mode 强 schema，替代 prompt + `_extract_json`。
- **cascading 降级**：`ModelRouter` 已留 `fallback: deepseek` 路由，主模型失败可降级。⚠️ `ModelRouter` 与分层路由/早停/缓存标记一样，是**本项目自定义 Middleware**，不是框架功能；框架侧只提供 `Model` 抽象（provider 可插拔）与真实的 `classifier` 模型路由入口。
- **EvalGate 门禁**：真实模型上线前须过评测门禁（`ModelRouter.validate_on_startup` 当前 mock 模式豁免，生产需取消注释 raise）。⚠️ **评测栈自建**：AgentScope 2.0 已删除 `agentscope.evaluate` 且未回归，不存在"内生评测"；门禁跑在自研 `EvalRunner` + 三层判定 + pass^k + cost-per-successful-task + CI 上，judge 用独立包 `py-openjudge`。
- **思考模型分离**：若思考模型（v4-flash）用于推理、通用模型（deepseek-chat）用于结构化输出，可按 capability 路由不同模型（本项目 `ModelRouter` 支持）。
