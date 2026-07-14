"""LangGraph checkpointer：当前 mock/real 均用 MemorySaver（real 模式打 warn）。

thread_id = session_id。interrupt/resume 依赖 checkpointer 持久化 state。
SqlSaver(MySQL) 持久化尚未接线（见 get_checkpointer 内 TODO），real 模式下
Pod 重启会丢失 interrupt/resume 会话。
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.infrastructure.obs.logging import get_logger


def get_checkpointer() -> Any:
    s = get_settings()
    from langgraph.checkpoint.memory import MemorySaver
    if s.mysql_url and not s.is_mock:
        # TODO(real): 接线 AsyncSqlSaver 持久化到 MySQL 三表（需 async setup()，
        #   在 lifespan startup 初始化；依赖 langgraph-checkpoint-mysql [mysql] extra）。
        #   当前 real 模式仍用 MemorySaver，Pod 重启会丢失 interrupt/resume 会话。
        get_logger("checkpointer").warning(
            "checkpointer.using_memory_saver_in_real_mode",
            hint="real 模式使用 MemorySaver，Pod 重启将丢失 interrupt/resume 编排会话；"
                 "启用 AsyncSqlSaver 持久化见 checkpointer.py TODO",
        )
        return MemorySaver()
    # mock 模式：进程内 MemorySaver（重启丢失，足够 demo）
    return MemorySaver()
