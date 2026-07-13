"""shared/obs -- 可观测底座。

对齐 agent-service 五层可观测模型与 trace 双存储（Tempo 火焰图 + MySQL 平铺表）。
rag-service 复用同一套，仅指标前缀为 ``rag_``。

口径见《rag-service-整体结构设计》§3.3、《技术选型和实现方案》§2.3/§9。
"""
from app.shared.obs.context import ObservabilityContext
from app.shared.obs.logging_ctx import LoggingContext
from app.shared.obs.metrics import MetricsCollector
from app.shared.obs.observability import Observability
from app.shared.obs.port import ObservabilityPort
from app.shared.obs.redactor import Redactor
from app.shared.obs.tracing import Tracing

__all__ = [
    "ObservabilityContext",
    "ObservabilityPort",
    "Observability",
    "Tracing",
    "MetricsCollector",
    "LoggingContext",
    "Redactor",
]
