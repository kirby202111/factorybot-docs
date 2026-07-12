"""MetricsCollector：所有 Counter/Histogram/Gauge 集中定义与埋点。

指标命名前缀 agent_*，labels 与可观测文档对齐。prometheus-client 在 /metrics 暴露。
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# ---- 指标集中定义 ----
SESSION_TOTAL = Counter(
    "agent_session_total", "Agent 会话计数", ["level", "status"]
)
SESSION_LATENCY = Histogram(
    "agent_session_latency_seconds", "Agent 会话延迟", ["level"]
)
TOOL_CALL_TOTAL = Counter(
    "agent_tool_call_total", "工具调用计数", ["tool", "status"]
)
TOOL_CALL_LATENCY = Histogram(
    "agent_tool_call_latency_seconds", "工具调用延迟", ["tool"]
)
LLM_INVOCATION_TOTAL = Counter(
    "agent_llm_invocation_total", "LLM 调用计数", ["model", "level"]
)
LLM_LATENCY = Histogram(
    "agent_llm_latency_seconds", "LLM 调用延迟", ["model"]
)
TOKEN_TOTAL = Counter(
    "agent_token_total", "token 用量", ["model", "direction", "level"]
)
COST_USD_TOTAL = Counter(
    "agent_cost_usd_total", "估算成本 USD", ["model", "level"]
)
LOW_CONFIDENCE_TOTAL = Counter(
    "agent_low_confidence_total", "低置信度转人工计数", ["level"]
)
RECURSION_LIMIT_TOTAL = Counter(
    "agent_recursion_limit_hit_total", "递归上限命中", ["level"]
)
SCHEMA_ERROR_TOTAL = Counter(
    "agent_llm_schema_error_total", "结构化输出解析失败", ["model"]
)
ACTIVE_SESSIONS = Gauge(
    "agent_active_sessions", "活跃会话数", ["level"]
)


class MetricsCollector:
    """业务节点通过它埋点，集中管理指标。"""

    def tool_ok(self, tool: str, latency_s: float) -> None:
        TOOL_CALL_TOTAL.labels(tool=tool, status="OK").inc()
        TOOL_CALL_LATENCY.labels(tool=tool).observe(latency_s)

    def tool_denied(self, tool: str) -> None:
        TOOL_CALL_TOTAL.labels(tool=tool, status="DENIED").inc()

    def tool_error(self, tool: str) -> None:
        TOOL_CALL_TOTAL.labels(tool=tool, status="ERROR").inc()

    def llm_called(self, model: str, prompt_version: str, prompt_tokens: int,
                   completion_tokens: int, latency_ms: int, finish_reason: str,
                   obs_ctx=None) -> None:
        level = obs_ctx.level if obs_ctx else "L1"
        LLM_INVOCATION_TOTAL.labels(model=model, level=level).inc()
        LLM_LATENCY.labels(model=model).observe(latency_ms / 1000.0)
        TOKEN_TOTAL.labels(model=model, direction="prompt", level=level).inc(prompt_tokens)
        TOKEN_TOTAL.labels(model=model, direction="completion", level=level).inc(completion_tokens)

    def low_confidence(self, level: str) -> None:
        LOW_CONFIDENCE_TOTAL.labels(level=level).inc()

    def recursion_limit_hit(self, level: str) -> None:
        RECURSION_LIMIT_TOTAL.labels(level=level).inc()

    def schema_error(self, model: str) -> None:
        SCHEMA_ERROR_TOTAL.labels(model=model).inc()

    def session_finished(self, level: str, status: str) -> None:
        SESSION_TOTAL.labels(level=level, status=status).inc()

    def session_started(self, level: str) -> None:
        ACTIVE_SESSIONS.labels(level=level).inc()

    def session_ended(self, level: str) -> None:
        ACTIVE_SESSIONS.labels(level=level).dec()

    def cost(self, model: str, level: str, usd: float) -> None:
        if usd > 0:
            COST_USD_TOTAL.labels(model=model, level=level).inc(usd)


def start_metrics_server(port: int = 9100) -> None:
    """启动 prometheus /metrics 端口（独立于 FastAPI）。mock 模式下不启动。"""
    try:
        start_http_server(port)
    except OSError:
        # 端口已占用等，忽略（不反噬业务）
        pass
