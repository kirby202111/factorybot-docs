"""cost 横切模块边界单测：早停 / 结果压缩 / 缓存标记 / 评测门禁 / 模型路由 / 阶段工具绑定。"""
from __future__ import annotations

import pytest

from app.infrastructure.cost.cache_control import (
    CacheControl,
    mark_system_prompt_cache,
    mark_tools_cache,
)
from app.infrastructure.cost.early_stop import EarlyStopDetector
from app.infrastructure.cost.eval_gate import EvalGate, EvalResult
from app.infrastructure.cost.model_router import ModelRouter
from app.infrastructure.cost.phase_tool_binder import PhaseToolBinder
from app.infrastructure.cost.result_compactor import ResultCompactor


# ---- EarlyStopDetector ----
class TestEarlyStop:
    def test_stop_at_max_tool_calls(self):
        es = EarlyStopDetector(max_tool_calls=8, min_evidence=2)
        stop, reason = es.should_stop(8, 0)
        assert stop and "上限" in reason

    def test_stop_on_model_self_assess_with_enough_evidence(self):
        es = EarlyStopDetector(max_tool_calls=8, min_evidence=2)
        stop, reason = es.should_stop(3, 2, model_self_assess=True)
        assert stop and "enough_evidence" in reason

    def test_no_stop_when_self_assess_but_insufficient_evidence(self):
        es = EarlyStopDetector(max_tool_calls=8, min_evidence=2)
        assert not es.should_stop(3, 1, model_self_assess=True)[0]

    def test_stop_on_evidence_redundancy(self):
        # min_evidence * 2 = 4 -> 冗余停
        es = EarlyStopDetector(max_tool_calls=8, min_evidence=2)
        stop, reason = es.should_stop(3, 4)
        assert stop and "冗余" in reason

    def test_no_stop_below_all_thresholds(self):
        es = EarlyStopDetector(max_tool_calls=8, min_evidence=2)
        assert not es.should_stop(3, 1)[0]

    def test_self_assess_false_below_redundancy_does_not_stop(self):
        es = EarlyStopDetector(max_tool_calls=8, min_evidence=2)
        # evidence=3 < min*2=4，self_assess=False -> 不停
        assert not es.should_stop(3, 3, model_self_assess=False)[0]


# ---- ResultCompactor ----
class TestResultCompactor:
    def test_non_dict_returns_summary(self):
        assert ResultCompactor().compact("x", "some string") == {"_summary": "some string"}

    def test_long_non_dict_truncated_to_200(self):
        out = ResultCompactor().compact("x", "a" * 300)
        assert len(out["_summary"]) == 200

    def test_whitelist_keeps_only_listed_fields(self):
        view = {"sn": "SN-1", "work_order_id": "WO-1", "extra": "drop", "decision": "PASS"}
        out = ResultCompactor().compact("query_pass_records", view)
        assert out["sn"] == "SN-1"
        assert out["decision"] == "PASS"
        assert "extra" not in out
        assert out["_omitted_count"] == 1

    def test_no_whitelist_keeps_all(self):
        out = ResultCompactor().compact("unknown_tool", {"a": 1, "b": 2})
        assert out == {"a": 1, "b": 2}
        assert "_omitted_count" not in out

    def test_list_truncation_marks_and_counts(self):
        out = ResultCompactor(truncate=2).compact("unknown_tool", {"items": [1, 2, 3, 4]})
        assert out["items"] == [1, 2]
        assert out["_items_truncated"] is True
        assert out["_omitted_count"] == 2

    def test_no_whitelist_warns_and_passes_through(self, monkeypatch):
        """#15: 无白名单工具发 warning（缺口可见），但维持透传不裁剪。"""
        from unittest.mock import MagicMock
        import app.infrastructure.cost.result_compactor as rc
        logger = MagicMock()
        monkeypatch.setattr(rc, "get_logger", lambda name: logger)
        out = rc.ResultCompactor().compact("unknown_tool", {"a": 1, "b": 2})
        assert out == {"a": 1, "b": 2}  # 透传不变
        logger.warning.assert_called_once()
        assert logger.warning.call_args.args[0] == "cost.result_compactor.no_whitelist"
        assert logger.warning.call_args.kwargs["tool_name"] == "unknown_tool"


# ---- CacheControl ----
class TestCacheControl:
    def test_to_dict_structure(self):
        assert CacheControl(ttl="1h").to_dict() == {
            "cache_control": {"type": "ephemeral", "ttl": "1h"}}

    def test_mark_system_prompt_cache(self):
        out = mark_system_prompt_cache("hello", ttl="5m")
        assert out["role"] == "system"
        assert out["content"] == "hello"
        assert out["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    def test_mark_tools_cache_adds_to_each(self):
        out = mark_tools_cache([{"name": "t1"}, {"name": "t2"}], ttl="1h")
        assert all("cache_control" in t for t in out)
        assert out[0]["cache_control"]["ttl"] == "1h"


# ---- EvalGate ----
class TestEvalGate:
    @staticmethod
    def _result(acc=0.9, ece=0.05, recall=0.9):
        return EvalResult(model="m", accuracy=acc, ece=ece, evidence_recall=recall)

    def test_passed_when_all_thresholds_met(self):
        assert self._result().passed is True

    def test_not_passed_low_accuracy(self):
        assert self._result(acc=0.80).passed is False

    def test_not_passed_high_ece(self):
        assert self._result(ece=0.15).passed is False

    def test_not_passed_low_recall(self):
        assert self._result(recall=0.70).passed is False

    def test_gate_unregistered_returns_false(self):
        assert EvalGate().passed("unknown") is False

    def test_gate_registered_passes(self):
        g = EvalGate()
        g.register(self._result())
        assert g.passed("m") is True


# ---- ModelRouter.route ----
class TestModelRouterRoute:
    def test_route_by_capability(self):
        assert ModelRouter(allow_mock=True).route("diagnosis") == "claude-sonnet-5"

    def test_route_capability_phase_prefers_specific(self):
        r = ModelRouter(allow_mock=True)
        r.set_route("diagnosis:phase1", "haiku")
        assert r.route("diagnosis", "phase1") == "haiku"

    def test_route_falls_back_for_unknown(self):
        assert ModelRouter(allow_mock=True).route("nonexistent") == "deepseek"


# ---- PhaseToolBinder ----
class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeRegistry:
    def tools_for(self, capability, tenant):
        return [_FakeTool("query_traceability_graph"),
                _FakeTool("query_pass_records"),
                _FakeTool("query_test_results"),
                _FakeTool("other")]


class TestPhaseToolBinder:
    def test_disabled_returns_all(self):
        binder = PhaseToolBinder(enabled=False)
        tools = binder.bind(_FakeRegistry(), "diagnosis", None, phase="init")
        assert len(tools) == 4

    def test_enabled_no_phase_returns_all(self):
        binder = PhaseToolBinder(enabled=True)
        tools = binder.bind(_FakeRegistry(), "diagnosis", None, phase=None)
        assert len(tools) == 4

    def test_enabled_init_phase_filters_to_phase_tools(self):
        binder = PhaseToolBinder(enabled=True)
        tools = binder.bind(_FakeRegistry(), "diagnosis", None, phase="init")
        names = {t.name for t in tools}
        # init 阶段仅 query_traceability_graph + query_pass_records
        assert names == {"query_traceability_graph", "query_pass_records"}
