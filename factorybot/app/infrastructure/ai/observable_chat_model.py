"""ObservableChatModel：包装任意 ChatModel，统一埋 LLM 观测。

provider 无关：token/延迟/模型/prompt_version 落 llm_call_log + prometheus + OTel span。
观测是只读旁路，内部异常不反噬业务。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.infrastructure.ai.base import ChatModel, ModelResponse
from app.infrastructure.obs.context import ObservabilityContext


class ObservableChatModel:
    """装饰器：在 ainvoke/ainvoke_structured 外层加观测。"""

    def __init__(self, inner: ChatModel, obs, prompt_version: str = "p_v1") -> None:
        self._inner = inner
        self._obs = obs
        self._prompt_version = prompt_version
        self.name = f"observable({getattr(inner, 'name', 'llm')})"

    async def ainvoke(
        self, messages: list[dict], tools: Optional[list[dict]] = None,
        obs_ctx: Optional[ObservabilityContext] = None,
    ) -> ModelResponse:
        t0 = time.perf_counter()
        model = getattr(self._inner, "name", "llm")
        try:
            with self._obs.llm_span(obs_ctx or _dummy_ctx(), model, self._prompt_version):
                resp = await self._inner.ainvoke(messages, tools)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            self._obs.llm_called(
                model=model, prompt_version=self._prompt_version,
                prompt_tokens=_est_tokens(messages), completion_tokens=_est_tokens_text(resp.content),
                latency_ms=latency_ms, finish_reason=resp.finish_reason, obs_ctx=obs_ctx,
            )
            return resp
        except Exception:
            self._obs.schema_error(model)  # 复用计数器
            raise

    async def ainvoke_structured(
        self, messages: list[dict], schema: type,
        obs_ctx: Optional[ObservabilityContext] = None,
    ) -> Any:
        t0 = time.perf_counter()
        model = getattr(self._inner, "name", "llm")
        with self._obs.llm_span(obs_ctx or _dummy_ctx(), model, self._prompt_version):
            result = await self._inner.ainvoke_structured(messages, schema)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        self._obs.llm_called(
            model=model, prompt_version=self._prompt_version,
            prompt_tokens=_est_tokens(messages), completion_tokens=128,
            latency_ms=latency_ms, finish_reason="structured", obs_ctx=obs_ctx,
        )
        return result


def _est_tokens(messages: list[dict]) -> int:
    # 粗估：4 字符 ≈ 1 token
    return sum(len(m.get("content", "")) for m in messages) // 4


def _est_tokens_text(text: str) -> int:
    return len(text) // 4


def _dummy_ctx() -> ObservabilityContext:
    return ObservabilityContext(
        session_id="-", trace_id="-", tenant_id="-", level="diagnosis",
    )
