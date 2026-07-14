"""obs 横切模块边界单测：脱敏纯函数 + 可观测异常吞没 + metrics 埋点。"""
from __future__ import annotations

import pytest

from app.infrastructure.obs.metrics import MetricsCollector
from app.infrastructure.obs.observability import build_observability
from app.infrastructure.obs.redactor import (
    redact_batch,
    redact_payload,
    redact_serial,
)


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
