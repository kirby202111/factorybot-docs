"""shared/config -- 统一配置。

公共配置项归 ``BaseSettings``，环境变量前缀 ``RAG_``、嵌套分隔符 ``__``。
口径见《rag-service-整体结构设计》§3.4、《技术选型和实现方案》§2.4。
"""
from app.shared.config.base import (
    BaseSettings,
    ChromaSettings,
    EmbeddingSettings,
    KafkaSettings,
    LlmSettings,
    MesSettings,
    MinioSettings,
    MysqlSettings,
    Neo4jSettings,
    OtelSettings,
    RedisSettings,
)
from app.shared.config.rag_settings import (
    AgenticSettings,
    DocSettings,
    RagSettings,
    TraceSettings,
)

__all__ = [
    "BaseSettings",
    "LlmSettings",
    "EmbeddingSettings",
    "Neo4jSettings",
    "ChromaSettings",
    "MysqlSettings",
    "RedisSettings",
    "KafkaSettings",
    "MesSettings",
    "MinioSettings",
    "OtelSettings",
    "RagSettings",
    "TraceSettings",
    "DocSettings",
    "AgenticSettings",
]
