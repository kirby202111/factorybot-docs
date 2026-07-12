"""持久化：业务可观测表 + L3 会话/步骤 + LangGraph checkpoint。

mock 模式：进程内仓库（InMemory*），无需 MySQL。
real 模式：SQLAlchemy 2.0 async + asyncmy 落 MySQL（models.py 定义表结构）。
SqlSaver checkpoint 三表由 LangGraph 管理（real 模式），mock 模式用 MemorySaver。
"""
from app.infrastructure.persistence.repos import (
    DraftRepo, DraftTraceRepo, L3Repo, NodeTraceRepo,
    ToolCallTraceRepo, get_l3_repo, get_tool_call_trace_repo,
)
from app.infrastructure.persistence.checkpointer import get_checkpointer

__all__ = [
    "DraftRepo", "DraftTraceRepo", "L3Repo", "NodeTraceRepo",
    "ToolCallTraceRepo", "get_l3_repo", "get_tool_call_trace_repo",
    "get_checkpointer",
]
