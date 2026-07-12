"""llm_factory：按配置产出 ChatModel。

- mock（默认）：ObservableChatModel(MockChatModel)
- real：ObservableChatModel(LangChainChatModel)（需 LLM_API_KEY，可选依赖）
任何模型降级必须过评测门禁 EvalGate（ModelRouter 启动时查）。
"""
from __future__ import annotations

from app.config import get_settings
from app.infrastructure.ai.base import ChatModel
from app.infrastructure.ai.mock_chat_model import MockChatModel
from app.infrastructure.ai.observable_chat_model import ObservableChatModel
from app.infrastructure.obs.logging import get_logger


def _build_inner_model() -> ChatModel:
    s = get_settings()
    # LLM 真实与否仅由 provider + api_key 决定，与 RUN_MODE 解耦：
    # 可 RUN_MODE=mock（ACL/存储/Kafka 走 mock fixtures）同时 LLM 走真实 DeepSeek
    if s.llm_provider == "mock" or not s.llm_api_key:
        return MockChatModel()
    # real provider 适配（可选依赖，按 provider 加载）
    if s.llm_provider == "openai":
        try:
            from langchain_openai import ChatOpenAI  # type: ignore
            return _LangChainAdapter(ChatOpenAI(model=s.llm_model, api_key=s.llm_api_key,
                                                base_url=s.llm_base_url or None))
        except ImportError as e:
            _warn_provider_fallback("openai", e)
            return MockChatModel()
    if s.llm_provider == "deepseek":
        # DeepSeek 兼容 OpenAI 协议，复用 ChatOpenAI，仅替换 base_url；无需额外依赖
        # model 未显式配置（或仍是 claude 默认值）时回退 deepseek-chat，避免误发模型名
        try:
            from langchain_openai import ChatOpenAI  # type: ignore
            model = s.llm_model if s.llm_model and not s.llm_model.startswith("claude") else "deepseek-chat"
            return _LangChainAdapter(ChatOpenAI(
                model=model, api_key=s.llm_api_key,
                base_url=s.llm_base_url or "https://api.deepseek.com",
            ))
        except ImportError as e:
            _warn_provider_fallback("deepseek", e)
            return MockChatModel()
    if s.llm_provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic  # type: ignore
            return _LangChainAdapter(ChatAnthropic(model=s.llm_model, api_key=s.llm_api_key))
        except ImportError as e:
            _warn_provider_fallback("anthropic", e)
            return MockChatModel()
    return MockChatModel()


def _warn_provider_fallback(provider: str, err: Exception) -> None:
    """配置了真实 provider 但依赖缺失/损坏 -> 回退 mock 时告警。

    避免静默伪装成真实模型：ImportError 可能是未装 [llm] extra，也可能是依赖损坏
    （如 jiter/openai 版本不匹配导致 `from jiter import from_json` 失败）。后者会让
    `ChatOpenAI(...)` 构造抛 ImportError，被这里捕获后悄悄回退 mock，表面看 provider
    配置正确实则跑 MockChatModel。告警让这种情形可见。
    """
    get_logger("llm_factory").warning(
        "llm.provider.fallback_to_mock",
        provider=provider, error=str(err),
        hint="配置了真实 LLM 但实际回退 mock：检查 langchain-openai/anthropic 及其依赖"
             "(openai、jiter 等)是否完整安装且版本兼容",
    )


class _LangChainAdapter:
    """把 langchain BaseChatModel 适配到 ChatModel Protocol。"""

    def __init__(self, lc_model) -> None:
        self._lc = lc_model
        self.name = getattr(lc_model, "model_name", "langchain-llm")

    async def ainvoke(self, messages, tools=None):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        lc_msgs = [_to_lc(m) for m in messages]
        # 绑定工具 schema（OpenAI function 格式），真实模型才能发出 tool_calls
        bound = self._lc.bind_tools(tools) if tools else self._lc
        resp = await bound.ainvoke(lc_msgs)
        tool_calls = []
        for i, tc in enumerate(getattr(resp, "tool_calls", []) or []):
            # 保留模型返回的 id（多步 ReAct 喂回 API 必需）；缺失时兜底，保证与 tool 消息配对
            tool_calls.append({"id": tc.get("id") or f"call_{i}",
                               "name": tc["name"], "args": tc["args"]})
        from app.infrastructure.ai.base import ModelResponse, ToolCall
        return ModelResponse(
            content=resp.content or "",
            tool_calls=[ToolCall(**tc) for tc in tool_calls],
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    async def ainvoke_structured(self, messages, schema):
        import json as _json
        from langchain_core.messages import HumanMessage, SystemMessage
        lc_msgs = [_to_lc(m) for m in messages]
        # DeepSeek 思考模型不支持 json_schema response_format 也不支持 tool_choice（function_calling）；
        # ChatOpenAI 系改用 json_mode（response_format=json_object）。该模式要求 prompt 含 'json'，
        # 故注入 schema 描述（含 'JSON' 字样）满足约束并引导结构化输出
        kwargs = {}
        try:
            from langchain_openai import ChatOpenAI  # type: ignore
            if isinstance(self._lc, ChatOpenAI):
                kwargs["method"] = "json_mode"
                schema_hint = (
                    "请严格输出符合如下 JSON Schema 的 JSON 对象，仅输出 JSON，不要解释或 markdown：\n"
                    + _json.dumps(schema.model_json_schema(), ensure_ascii=False)
                )
                lc_msgs.append(SystemMessage(content=schema_hint))
        except ImportError:
            pass
        structured = self._lc.with_structured_output(schema, **kwargs)
        return await structured.ainvoke(lc_msgs)


def _to_lc(m):
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage
    role = m.get("role")
    content = m.get("content", "")
    if role == "system":
        return SystemMessage(content=content)
    if role == "user":
        return HumanMessage(content=content)
    if role == "tool":
        return ToolMessage(content=content, tool_call_id=m.get("tool_call_id") or "0")
    # assistant：恢复 tool_calls（含 id + type），多步 ReAct 喂回真实 API 必需
    tcs = m.get("tool_calls") or []
    if tcs:
        lc_tcs = [
            {"id": tc.get("id") or "0", "name": tc["name"],
             "args": tc.get("args", {}), "type": "tool_call"}
            for tc in tcs
        ]
        return AIMessage(content=content, tool_calls=lc_tcs)
    return AIMessage(content=content)


_llm: ChatModel | None = None


def get_llm(obs=None) -> ChatModel:
    """单例 LLM（ObservableChatModel 包装）。obs 由容器注入。"""
    global _llm
    if _llm is None:
        inner = _build_inner_model()
        _llm = ObservableChatModel(inner, obs) if obs is not None else inner
    return _llm
