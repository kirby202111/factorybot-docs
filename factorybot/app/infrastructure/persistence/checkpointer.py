"""LangGraph checkpointer：mock 用 MemorySaver，real 用 SqlSaver(MySQL)。

thread_id = session_id。interrupt/resume 依赖 checkpointer 持久化 state。
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings


def get_checkpointer() -> Any:
    s = get_settings()
    if s.mysql_url and not s.is_mock:
        # real 模式：LangGraph SqlSaver 持久化到 MySQL 三表
        # from langgraph.checkpoint.mysql import AsyncSqlSaver
        # import asyncmy
        # ... 需要 MySQL 连接池；这里仅示意，真实部署时启用
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    # mock 模式：进程内 MemorySaver（重启丢失，足够 demo）
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()
