"""包装任意 langchain-core BaseChatModel，统一埋点，provider 无关。

inner 模型**懒构造**：服务启动期不依赖具体 provider 库（langchain-anthropic /
langchain-community / langchain-openai），仅在首次 ``achat`` 时按 provider 构造。
这让单服务可在未选装某 provider 时照常启动，调用时才暴露缺失。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.shared.obs.port import ObservabilityPort


class ChatResult(BaseModel):
    """LLM 调用的统一返回结构（屏蔽 langchain AIMessage 细节）。"""

    content: str = Field(description="模型输出的文本内容")
    total_tokens: int = Field(default=0, description="本次调用总 token 数")
    model: str = Field(default="", description="实际使用的模型名")
    raw: Any = Field(default=None, description="原始响应对象（调试用）")

    def as_messages(self) -> list[dict[str, str]]:
        """以 assistant message 形式返回，便于拼接到后续上下文。"""
        return [{"role": "assistant", "content": self.content}]


class ObservableChatModel:
    """包装任意 langchain-core ``BaseChatModel``。

    职责（SRP）：在 LLM 调用外围统一埋 token/延迟/模型/prompt_version，
    并把 langchain 的返回结构归一化为 ``ChatResult``。provider 无关：
    inner 可以是 langchain-anthropic / langchain-community / 本地模型。
    """

    def __init__(
        self,
        inner_factory: Callable[[], Any],
        obs: ObservabilityPort,
        model_name: str,
        prompt_version: str,
    ) -> None:
        self._inner_factory = inner_factory
        self._inner: Any = None
        self._obs = obs
        self.model_name = model_name
        self.prompt_version = prompt_version

    def _get_inner(self) -> Any:
        if self._inner is None:
            self._inner = self._inner_factory()
        return self._inner

    async def achat(self, messages: list[Any], **kwargs: Any) -> ChatResult:
        started = time.perf_counter()
        with self._obs.llm_span(model=self.model_name, prompt_version=self.prompt_version):
            resp = await self._get_inner().ainvoke(messages, **kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        tokens = self._extract_tokens(resp)
        content = self._extract_content(resp)
        self._obs.record_llm(
            model=self.model_name,
            tokens=tokens,
            latency_ms=latency_ms,
            prompt_version=self.prompt_version,
        )
        return ChatResult(content=content, total_tokens=tokens, model=self.model_name, raw=resp)

    def with_structured_output(self, schema: type) -> "ObservableChatModel":
        """绑定结构化输出 schema（Pydantic 模型），返回新的包装视图。"""
        outer = self

        def bound_factory() -> Any:
            return outer._get_inner().with_structured_output(schema)

        return ObservableChatModel(
            inner_factory=bound_factory,
            obs=self._obs,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
        )

    @staticmethod
    def _extract_content(resp: Any) -> str:
        content = getattr(resp, "content", resp)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        return str(content)

    @staticmethod
    def _extract_tokens(resp: Any) -> int:
        usage = getattr(resp, "usage_metadata", None) or getattr(resp, "response_metadata", {})
        if isinstance(usage, dict):
            return int(usage.get("total_tokens") or usage.get("totalTokens") or 0)
        return int(getattr(usage, "total_tokens", 0) or 0)
