"""公共配置项，环境变量前缀 ``RAG_``、嵌套分隔符 ``__``。

子配置均为普通 ``BaseModel``：由顶层 ``BaseSettings`` 经嵌套分隔符统一映射，
如 ``RAG_LLM__PROVIDER`` -> ``llm.provider``、``RAG_NEO4J__URI`` -> ``neo4j.uri``。
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings as PydanticBaseSettings
from pydantic_settings import SettingsConfigDict


class LlmSettings(BaseModel):
    """LLM 抽象配置。provider 无关、可插拔。"""

    provider: str = "deepseek"           # claude | qwen | deepseek | local
    model_name: str = "deepseek-chat"
    api_key: str = ""
    base_url: str | None = None
    prompt_version: str = "v1"
    temperature: float = 0.2
    timeout: float = 30.0


class EmbeddingSettings(BaseModel):
    """向量化 + 精排配置。provider 无关、可插拔。

    - provider=bailian（默认）：百炼 text-embedding-v4（1024 维）+ gte-rerank-v2，走云 API，
      需 ``api_key``；``base_url`` 指百炼 OpenAI 兼容端点。
    - provider=bge：bge-m3 + bge-reranker-v2-m3 本地 sidecar（车间网隔离场景），
      ``base_url`` 指 sidecar（如 http://bge-inference:8080），无需 ``api_key``。
    """

    provider: str = "bailian"  # bailian | bge
    api_key: str = ""  # 百炼 DASHSCOPE_API_KEY（bailian 用）
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "text-embedding-v4"
    dim: int = 1024
    reranker_model: str = "gte-rerank-v2"
    batch_size: int = 32


class Neo4jSettings(BaseModel):
    """A 图存储。"""

    uri: str = "bolt://neo4j:7687"
    username: str = "neo4j"
    password: str = "rag"
    max_connections: int = 50


class ChromaSettings(BaseModel):
    """B 嵌入式向量库（Parquet 持久化）。"""

    persist_dir: str = "/data/chroma"
    collection: str = "document_chunks"


class MysqlSettings(BaseModel):
    """shared/A/E/B(幂等位点审计) 关系库。"""

    dsn: str = "mysql+asyncmy://rag:rag@mysql:3306/rag"
    pool_size: int = 10
    max_overflow: int = 20


class RedisSettings(BaseModel):
    """缓存（A 子图 / B 检索 / E 查询）。"""

    url: str = "redis://redis:6379/0"


class KafkaSettings(BaseModel):
    """领域事件消费。"""

    bootstrap: str = "kafka:9092"
    consumer_group_prefix: str = "rag"


class MinioSettings(BaseModel):
    """B 原始文档对象存储（S3 兼容，path-style-access）。"""

    endpoint: str = "minio:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "rag-docs"
    secure: bool = False


class OtelSettings(BaseModel):
    """可观测导出。"""

    exporter_endpoint: str = "http://otel-collector:4317"
    service_name: str = "rag-service"


class BaseSettings(PydanticBaseSettings):
    """公共配置项聚合。环境变量前缀 ``RAG_``、嵌套分隔符 ``__``。"""

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    llm: LlmSettings = Field(default_factory=LlmSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    chroma: ChromaSettings = Field(default_factory=ChromaSettings)
    mysql: MysqlSettings = Field(default_factory=MysqlSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    otel: OtelSettings = Field(default_factory=OtelSettings)
