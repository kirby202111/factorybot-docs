"""LLM 抽象接口（DIP）：业务层依赖它而非具体 provider。"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LlmPort(Protocol):
    """LLM 抽象接口。

    所有路线（A/B/E）的 LLM 调用都走它。provider 无关、可插拔；
    任何模型降级须过 mes-eval ``EvalGate``（成本优化横切归 agent-service，
    rag-service 仅 E 路由图按需引用）。
    """

    model_name: str
    prompt_version: str

    async def achat(self, messages: list[Any], **kwargs: Any) -> "ChatResult":  # noqa: D401
        """异步聊天补全。"""
        ...

    def with_structured_output(self, schema: type) -> "LlmPort":
        """绑定结构化输出 schema（Pydantic 模型），返回新的 LlmPort 视图。"""
        ...


# ChatResult 在 observable_chat_model 中定义，此处仅用于类型字符串。
# 为避免循环导入，使用 TYPE_CHECKING 注解。
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from app.shared.ai.observable_chat_model import ChatResult
