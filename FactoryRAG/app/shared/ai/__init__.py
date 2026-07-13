"""shared/ai -- LLM 抽象（DIP）。

业务层依赖 ``LlmPort`` 而非具体 provider；``ObservableChatModel`` 包装任意
langchain-core ``BaseChatModel``，统一埋 token/延迟/模型/prompt_version，provider 无关。

口径见《rag-service-整体结构设计》§3.1、《技术选型和实现方案》§2.1。
"""
from app.shared.ai.llm_factory import llm_factory
from app.shared.ai.observable_chat_model import ChatResult, ObservableChatModel
from app.shared.ai.port import LlmPort

__all__ = ["LlmPort", "ObservableChatModel", "ChatResult", "llm_factory"]
