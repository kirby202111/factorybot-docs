"""统一配置 (pydantic-settings)。

所有外部依赖都有"留空即 mock"的回退：
- LLM_API_KEY 空 -> MockChatModel
- MYSQL_URL 空 -> MemorySaver + 进程内仓库
- REDIS_URL 空 -> FakeRedis
- KAFKA_BOOTSTRAP_SERVERS 空 -> MockActionCardProducer
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ----- LLM -----
    llm_provider: str = "mock"  # mock | openai | deepseek | anthropic | dashscope
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    llm_base_url: str = ""

    # ----- 运行模式 -----
    run_mode: str = "mock"  # mock | real

    # ----- 存储 -----
    mysql_url: str = ""
    redis_url: str = ""

    # ----- Kafka -----
    kafka_bootstrap_servers: str = ""
    kafka_group_id: str = "agent-service"

    # ----- OTel -----
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "agent-service"

    # ----- 业务默认值（mock 场景示例租户）-----
    default_tenant_id: str = "WS-A"
    default_workshop: str = "SMT-1"
    default_line: str = "L-01"

    # ----- 租户权限（real 模式 scopes 来源；对应待办 #34 方案 B）-----
    # tenant_id -> scope 列表。real 模式下 resolve_tenant_context 据此解析 scopes；
    # 未配置的租户默认 scopes=[]（fail-closed，拒绝一切写操作），不再回退 WS-A 全量。
    # 可用 scope 见 TenantContext.default()：read 类(pass/workorder/process/material/wip/
    # device/equipment/rework/quality/tooling/rag/doc:read) + write 类(rework/process/pass:write)。
    # 保守起步(2026-07-15)：WS-A 主线全权(含写)，WS-B 只读，其它租户 fail-closed。
    # 生产用 env 覆盖真实车间 ID 与权限：TENANT_SCOPES='{"WS-A":[...],"WS-B":[...]}'
    tenant_scopes: dict[str, list[str]] = Field(default_factory=lambda: {
        "WS-A": [
            "pass:read", "workorder:read", "process:read", "material:read", "wip:read",
            "device:read", "equipment:read", "rework:read", "quality:read", "tooling:read",
            "rag:read", "doc:read",
            "rework:write", "process:write", "pass:write",
        ],
        "WS-B": [
            "pass:read", "workorder:read", "process:read", "material:read", "wip:read",
            "device:read", "equipment:read", "rework:read", "quality:read", "tooling:read",
            "rag:read", "doc:read",
        ],
    })

    # ----- Agent 控制参数 -----
    diagnosis_recursion_limit: int = 20
    diagnosis_session_timeout: float = 60.0
    diagnosis_confidence_threshold: float = 0.5
    orchestration_recursion_limit: int = 40
    orchestration_session_timeout: float = 3600.0
    confirmation_token_ttl: int = 1800  # 30min
    failure_threshold: int = 2          # agent 连续失败 >=2 -> SUSPENDED

    # ----- fixtures 根目录（相对项目根）-----
    data_dir: str = Field(default="data")

    @property
    def is_mock(self) -> bool:
        return self.run_mode == "mock" or self.llm_provider == "mock"


@lru_cache
def get_settings() -> Settings:
    return Settings()
