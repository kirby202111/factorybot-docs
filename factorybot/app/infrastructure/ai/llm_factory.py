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


def _build_inner_model() -> ChatModel:
    s = get_settings()
    if s.is_mock or s.llm_provider == "mock" or not s.llm_api_key:
        return MockChatModel()
    # real provider 适配（可选依赖，按 provider 加载）
    if s.llm_provider == "openai":
        try:
            from langchain_openai import ChatOpenAI  # type: ignore
            return _LangChainAdapter(ChatOpenAI(model=s.llm_model, api_key=s.llm_api_key,
                                                base_url=s.llm_base_url or None))
        except ImportError:
            return MockChatModel()
    if s.llm_provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic  # type: ignore
            return _LangChainAdapter(ChatAnthropic(model=s.llm_model, api_key=s.llm_api_key))
        except ImportError:
            return MockChatModel()
    return MockChatModel()


class _LangChainAdapter:
    """把 langchain BaseChatModel 适配到 ChatModel Protocol。"""

    def __init__(self, lc_model) -> None:
        self._lc = lc_model
        self.name = getattr(lc_model, "model_name", "langchain-llm")

    async def ainvoke(self, messages, tools=None):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        lc_msgs = [_to_lc(m) for m in messages]
        resp = await self._lc.ainvoke(lc_msgs)
        tool_calls = []
        for tc in getattr(resp, "tool_calls", []) or []:
            tool_calls.append({"name": tc["name"], "args": tc["args"]})
        from app.infrastructure.ai.base import ModelResponse, ToolCall
        return ModelResponse(
            content=resp.content or "",
            tool_calls=[ToolCall(**tc) for tc in tool_calls],
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    async def ainvoke_structured(self, messages, schema):
        from langchain_core.messages import HumanMessage, SystemMessage
        lc_msgs = [_to_lc(m) for m in messages]
        structured = self._lc.with_structured_output(schema)
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
        return ToolMessage(content=content, tool_call_id=m.get("tool_call_id", "0"))
    return AIMessage(content=content)


_llm: ChatModel | None = None


def get_llm(obs=None) -> ChatModel:
    """单例 LLM（ObservableChatModel 包装）。obs 由容器注入。"""
    global _llm
    if _llm is None:
        inner = _build_inner_model()
        _llm = ObservableChatModel(inner, obs) if obs is not None else inner
    return _llm
