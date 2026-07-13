"""入口 Settings 加载。"""
from __future__ import annotations

from app.shared.config.rag_settings import RagSettings


def load_settings() -> RagSettings:
    """加载 RagSettings（环境变量前缀 ``RAG_``、嵌套分隔符 ``__``）。"""
    return RagSettings()
