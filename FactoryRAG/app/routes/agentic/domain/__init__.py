"""路线 E 领域层。"""
from app.routes.agentic.domain.answer import AgentAnswer, AnswerSource
from app.routes.agentic.domain.intent import IntentCategory
from app.routes.agentic.domain.tool import (
    ReadOnlyToolGate,
    ToolDescriptor,
    ToolRegistry,
)

__all__ = [
    "IntentCategory",
    "AgentAnswer",
    "AnswerSource",
    "ToolDescriptor",
    "ToolRegistry",
    "ReadOnlyToolGate",
]
