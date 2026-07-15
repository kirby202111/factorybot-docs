"""obs 横切模块边界单测：脱敏纯函数 + 可观测异常吞没 + metrics 埋点。"""
from __future__ import annotations

import asyncio

import pytest

from app.infrastructure.obs.metrics import MetricsCollector
from app.infrastructure.obs.observability import build_observability
from app.infrastructure.obs.redactor import (
    redact_batch,
    redact_payload,
    redact_serial,
)


class _FakeLog:
    """structlog 替身：按 (level, event, kwargs) 记录调用，便于断言日志可见性。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, level: str, event: str, kw: dict) -> None:
        self.calls.append((level, event, kw))

    def debug(self, e, **k): self._record("debug", e, k)
    def info(self, e, **k): self._record("info", e, k)
    def warning(self, e, **k): self._record("warning", e, k)
    def error(self, e, **k): self._record("error", e, k)
    def critical(self, e, **k): self._record("critical", e, k)



# ---- redactor ----
class TestRedactSerial:
    def test_regex_path_masks_middle(self):
        # SN-AB1234CD -> 前 SN-AB 后 CD 中间 ****
        assert redact_serial("SN-AB1234CD") == "SN-AB****CD"

    def test_fallback_for_dashed_serial(self):
        # 含多个 "-" 不匹配 regex，走兜底前 4 后 2（len=14 -> 8 个 *）
        assert redact_serial("SN-2026-001234") == "SN-2********34"

    def test_short_unchanged(self):
        assert redact_serial("SN-AB") == "SN-AB"

    def test_empty_unchanged(self):
        assert redact_serial("") == ""


class TestRedactBatch:
    def test_long_masks_middle(self):
        # len=11 -> 前 2 后 2，中间 7 个 *
        assert redact_batch("B-2026-0701") == "B-*******01"

    def test_short_unchanged(self):
        assert redact_batch("B-12") == "B-12"

    def test_empty_unchanged(self):
        assert redact_batch("") == ""


class TestRedactPayload:
    def test_recurses_and_redacts_by_key_name(self):
        payload = {
            "serial_no": "SN-AB1234CD",
            "batch_no": "B-2026-0701",
            "other": "keep",
            "nested": {"sn": "SN-AB1234CD"},
            "items": [{"batch": "B-2026-0701"}, "x"],
        }
        out = redact_payload(payload)
        assert out["serial_no"] == "SN-AB****CD"
        assert out["batch_no"] == "B-*******01"
        assert out["other"] == "keep"
        assert out["nested"]["sn"] == "SN-AB****CD"
        assert out["items"][0]["batch"] == "B-*******01"
        # 列表中非 dict 元素不被脱敏
        assert out["items"][1] == "x"


# ---- observability：异常吞没（观测是只读旁路，不反噬业务）----
class TestObservabilitySafe:
    def test_safe_swallows_exception(self):
        obs = build_observability()

        def boom():
            raise RuntimeError("观测内部故障")

        # 不抛即通过：观测异常不得反噬业务
        obs._safe(boom)

    def test_public_methods_do_not_raise(self):
        obs = build_observability()
        # 各埋点方法均经 _safe，正常路径不抛
        obs.tool_ok("t1", 0.5)
        obs.tool_denied("t1")
        obs.tool_error("t1")
        obs.low_confidence("diagnosis")
        obs.session_started("diagnosis")
        obs.session_ended("diagnosis")
        obs.session_finished("diagnosis", "DONE")


# ---- metrics：埋点不抛 + cost 零值不计 ----
class TestMetrics:
    def test_calls_do_not_raise(self):
        m = MetricsCollector()
        m.tool_ok("t1", 0.1)
        m.tool_denied("t1")
        m.tool_error("t1")
        m.llm_called("claude-sonnet-5", "v1", 100, 50, 200, "stop")
        m.session_finished("diagnosis", "DONE")

    def test_cost_zero_not_counted(self):
        # usd <= 0 不应 inc（COST_USD_TOTAL），不抛即通过
        m = MetricsCollector()
        m.cost("claude-sonnet-5", "diagnosis", 0.0)
        m.cost("claude-sonnet-5", "diagnosis", -1.0)


# ---- _safe_async：任务异常必须可见，不得反噬业务、不得静默丢失 ----
class TestSafeAsync:
    def test_task_exception_logged_not_silent(self, monkeypatch):
        from app.infrastructure.obs import observability as mod

        fake = _FakeLog()
        monkeypatch.setattr(mod, "_log", fake)
        obs = build_observability()

        async def boom():
            raise RuntimeError("llm log write failed")

        async def _run():
            obs._safe_async(boom)  # 不得向调用方抛
            await asyncio.sleep(0.02)  # 让 task + done-callback 跑完

        asyncio.run(_run())

        # 任务内部异常被 done-callback 取出记 ERROR，不再"never retrieved"静默丢失
        assert any(lvl == "error" and evt == "obs.background_task_failed"
                   for lvl, evt, _ in fake.calls)

    def test_no_running_loop_logs_warning(self, monkeypatch):
        from app.infrastructure.obs import observability as mod

        fake = _FakeLog()
        monkeypatch.setattr(mod, "_log", fake)
        obs = build_observability()

        async def noop():
            return None

        async def _run():
            # 在无事件循环的工作线程里调用 -> get_running_loop() 抛 RuntimeError -> 记 warning
            await asyncio.to_thread(obs._safe_async, noop)

        asyncio.run(_run())

        assert any(lvl == "warning" and evt == "obs.safe_async.no_running_loop"
                   for lvl, evt, _ in fake.calls)

