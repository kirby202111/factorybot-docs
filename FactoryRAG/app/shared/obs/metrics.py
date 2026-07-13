"""prometheus 指标集中定义，统一前缀 ``rag_``。

指标清单见《rag-service-技术选型和实现方案》§9.1。
prometheus_client 不可用时降级为 no-op，观测失败不反噬业务。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram

    _HAS_PROM = True
except Exception:  # pragma: no cover
    _HAS_PROM = False


class _NoopMetric:
    """prometheus_client 不可用时的 no-op 指标。"""

    def labels(self, *a: Any, **kw: Any) -> "_NoopMetric":
        return self

    def inc(self, *a: Any, **kw: Any) -> None:  # noqa: D401
        pass

    def observe(self, *a: Any, **kw: Any) -> None:
        pass

    def set(self, *a: Any, **kw: Any) -> None:
        pass


def _histogram(name: str, desc: str, buckets: list[float] | None = None) -> Any:
    if not _HAS_PROM:
        return _NoopMetric()
    return Histogram(name, desc, buckets=buckets or [0.01, 0.05, 0.1, 0.3, 0.5, 1, 2, 5, 10])


def _counter(name: str, desc: str) -> Any:
    if not _HAS_PROM:
        return _NoopMetric()
    return Counter(name, desc)


def _gauge(name: str, desc: str) -> Any:
    if not _HAS_PROM:
        return _NoopMetric()
    return Gauge(name, desc)


class MetricsCollector:
    """Counter/Histogram/Gauge 集中定义，统一前缀 ``rag_``。

    业务节点经 ``ObservabilityPort`` 间接调用；此处是具体实现。
    """

    def __init__(self) -> None:
        # ── shared ──
        self.llm_call_duration = _histogram(
            "rag_llm_call_duration_seconds", "LLM 调用延迟(秒)", ["model", "prompt_version"] if _HAS_PROM else None
        )
        self.llm_tokens = _counter("rag_llm_tokens_total", "LLM token 总数")
        self.kafka_consumer_lag = _gauge("rag_kafka_consumer_lag", "Kafka 消费者位点滞后")
        self.storage_health = _gauge("rag_storage_health", "存储健康(0/1)")

        # ── A 追溯型 ──
        self.trace_query_duration = _histogram("rag_trace_query_duration_seconds", "A 检索延迟(秒)")
        self.projection_lag = _histogram("rag_projection_lag_ms", "事件->入图滞后(毫秒)")
        self.projection_events = _counter("rag_projection_events_total", "A 投影事件计数")

        # ── B 文档型 ──
        self.doc_search_hits = _histogram("rag_doc_search_hits", "B 检索 top-k 命中数")
        self.doc_ingest_chunks = _counter("rag_doc_ingest_chunks_total", "B 摄入 chunk 计数")
        self.doc_reindex_lag = _histogram("rag_doc_reindex_lag_ms", "B 重索引滞后(毫秒)")
        self.doc_chroma_query_duration = _histogram("rag_doc_chroma_query_duration_seconds", "ChromaDB 检索延迟(秒)")
        self.deprecated_leak = _counter("rag_doc_deprecated_leak_total", "DEPRECATED 泄漏计数")

        # ── E Agentic ──
        self.agent_route = _counter("rag_agent_route_total", "E 意图路由计数")
        self.agent_delegation_duration = _histogram("rag_agent_delegation_duration_seconds", "E L1/L2 委托延迟(秒)")
