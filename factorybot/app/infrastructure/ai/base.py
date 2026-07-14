"""LLM 抽象层：ChatModel Protocol + 消息/响应模型。

设计意图（对齐 诊断/草稿 实现方案 §2.1）：模型可插拔，ObservableChatModel 包装任意模型
统一埋观测（token/延迟/模型/prompt_version），provider 无关。结构化输出经 ainvoke_structured。

mock 模式用 MockChatModel（确定性，离线可跑）；real 模式经 llm_factory 切真实 provider。
消息用 plain dict（role/content），避免与具体框架消息类型耦合。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str = ""  # 真实模型返回的 tool_call_id；多步 ReAct 喂回 API 必需，mock 留空
    name: str
    args: dict = Field(default_factory=dict)


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"  # stop | tool_calls


@runtime_checkable
class ChatModel(Protocol):
    """LLM 抽象。ainvoke 驱动 ReAct；ainvoke_structured 产出 Pydantic 实例。"""

    name: str

    async def ainvoke(
        self, messages: list[dict], tools: Optional[list[dict]] = None,
    ) -> ModelResponse: ...

    async def ainvoke_structured(
        self, messages: list[dict], schema: type,
    ) -> Any: ...


# ---- 消息构造助手 ----
def sys_msg(content: str) -> dict:
    return {"role": "system", "content": content}


def user_msg(content: str) -> dict:
    return {"role": "user", "content": content}


def assistant_msg(content: str = "", tool_calls: Optional[list[dict]] = None) -> dict:
    return {"role": "assistant", "content": content, "tool_calls": tool_calls or []}


def tool_msg(name: str, content: str, tool_call_id: str = "") -> dict:
    return {"role": "tool", "name": name, "tool_call_id": tool_call_id, "content": content}
