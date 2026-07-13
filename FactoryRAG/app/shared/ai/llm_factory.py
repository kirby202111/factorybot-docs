"""按 config 创建 ObservableChatModel，适配 Claude/通义千问/DeepSeek/本地模型。"""
from __future__ import annotations

from typing import Any

from app.shared.ai.observable_chat_model import ObservableChatModel
from app.shared.ai.port import LlmPort
from app.shared.config.base import LlmSettings
from app.shared.obs.port import ObservabilityPort


def llm_factory(settings: LlmSettings, obs: ObservabilityPort) -> LlmPort:
    """按 ``settings.provider`` 创建 ``ObservableChatModel``。

    provider 无关：Claude / 通义千问 / DeepSeek / 本地模型均经 langchain-core
    ``BaseChatModel`` 适配；模型可插拔。具体 provider 包按需选装
    (langchain-anthropic / langchain-community / 本地)。

    inner 模型**懒构造**：此处只传入 factory 闭包，不在启动期 import provider 库，
    单服务可在未选装某 provider 时照常启动，调用时才暴露缺失。
    """
    return ObservableChatModel(
        inner_factory=lambda: _build_inner(settings),
        obs=obs,
        model_name=settings.model_name,
        prompt_version=settings.prompt_version,
    )


def _build_inner(settings: LlmSettings) -> Any:
    provider = settings.provider.lower()
    if provider == "claude":
        return _build_anthropic(settings)
    if provider == "qwen":
        return _build_qwen(settings)
    if provider == "deepseek":
        return _build_deepseek(settings)
    if provider == "local":
        return _build_local(settings)
    raise ValueError(f"不支持的 LLM provider: {settings.provider}")


def _build_anthropic(settings: LlmSettings) -> Any:
    from langchain_anthropic import ChatAnthropic  # type: ignore

    return ChatAnthropic(
        model=settings.model_name,
        api_key=settings.api_key,
        temperature=settings.temperature,
        timeout=settings.timeout,
        max_tokens=4096,
    )


def _build_qwen(settings: LlmSettings) -> Any:
    # 通义千问经 langchain-community 的 ChatTongyi 适配。
    from langchain_community.chat_models.tongyi import ChatTongyi  # type: ignore

    return ChatTongyi(
        model=settings.model_name,
        api_key=settings.api_key,
        temperature=settings.temperature,
    )


def _build_deepseek(settings: LlmSettings) -> Any:
    # DeepSeek 兼容 OpenAI 接口。
    from langchain_openai import ChatOpenAI  # type: ignore

    return ChatOpenAI(
        model=settings.model_name,
        api_key=settings.api_key,
        base_url=settings.base_url or "https://api.deepseek.com",
        temperature=settings.temperature,
        timeout=settings.timeout,
    )


def _build_local(settings: LlmSettings) -> Any:
    # 本地模型（vLLM/Ollama 等）经 OpenAI 兼容接口。
    from langchain_openai import ChatOpenAI  # type: ignore

    return ChatOpenAI(
        model=settings.model_name,
        api_key=settings.api_key or "not-needed",
        base_url=settings.base_url,
        temperature=settings.temperature,
        timeout=settings.timeout,
    )
