"""ResultCompactor 接入 ReAct 的压缩边界单测（思路 D）。

验证：_compact_tool_history 压缩 tool 消息的 data（trace_id 顶层保留），且只作用于
返回副本--state.messages 全文不被污染，护栏 _guard_no_evidence 仍能从原文检出不良证据。
"""
from __future__ import annotations

import json

from app.infrastructure.ai.react_graph import _compact_tool_history
from app.infrastructure.cost.result_compactor import ResultCompactor


def _tool_msg(name: str, payload: dict) -> dict:
    return {"role": "tool", "name": name, "tool_call_id": "c1",
            "content": json.dumps(payload, ensure_ascii=False)}


class TestCompactToolHistory:
    def test_none_compactor_returns_history_unchanged(self):
        history = [{"role": "user", "content": "hi"}]
        assert _compact_tool_history(history, None) is history

    def test_non_tool_messages_untouched(self):
        history = [{"role": "system", "content": "s"},
                   {"role": "user", "content": "u"},
                   {"role": "assistant", "content": "a"}]
        assert _compact_tool_history(history, ResultCompactor()) == history

    def test_tool_data_compressed_trace_id_kept(self):
        # query_pass_records 有白名单：保留 decision，裁掉 extra
        payload = {"trace_id": "T-101", "data": {
            "sn": "SN-1", "work_order_id": "WO-1",
            "decision": "BLOCK", "extra": "drop"}}
        history = [_tool_msg("query_pass_records", payload)]
        out = _compact_tool_history(history, ResultCompactor())
        compacted = json.loads(out[0]["content"])
        assert compacted["trace_id"] == "T-101"            # trace_id 顶层保留
        assert compacted["data"]["decision"] == "BLOCK"    # 白名单字段保留
        assert "extra" not in compacted["data"]            # 非白名单裁掉
        assert compacted["data"]["_omitted_count"] == 1

    def test_invalid_json_tool_message_passthrough(self):
        history = [{"role": "tool", "name": "x", "tool_call_id": "c",
                    "content": "not-json"}]
        assert _compact_tool_history(history, ResultCompactor()) == history

    def test_original_history_not_mutated(self):
        """压缩只作用于返回副本，原 history（state.messages 全文）保持不变。"""
        payload = {"trace_id": "T-1", "data": {
            "sn": "SN-1", "decision": "BLOCK", "extra": "x"}}
        history = [_tool_msg("query_pass_records", payload)]
        original = history[0]["content"]
        _compact_tool_history(history, ResultCompactor())
        assert history[0]["content"] == original


class TestGuardNoEvidenceUnaffected:
    """护栏 _guard_no_evidence 读 state.messages 全文；压缩副本不污染原文。"""

    def test_defect_still_detected_from_full_messages(self):
        from app.infrastructure.ai.graph_builder import _has_defect_evidence
        # 追溯图含 BLOCK 节点（query_traceability_graph 白名单不含 nodes）
        payload = {"trace_id": "T-1", "data": {
            "serial_no": "SN-1", "subgraph_ref": "SUB-A1",
            "nodes": [{"id": "n1", "properties": {"decision": "BLOCK"}}]}}
        history = [_tool_msg("query_traceability_graph", payload)]
        # 压缩副本：nodes 会被裁（白名单不含 nodes）
        compacted = _compact_tool_history(history, ResultCompactor())
        compacted_data = json.loads(compacted[0]["content"])["data"]
        assert "nodes" not in compacted_data          # 压缩确实裁掉 nodes
        # 但原文 history 仍含 nodes -> 护栏仍能检出 BLOCK（核心安全属性）
        assert _has_defect_evidence(history) is True

    def test_no_defect_in_full_messages(self):
        from app.infrastructure.ai.graph_builder import _has_defect_evidence
        payload = {"trace_id": "T-1", "data": {
            "serial_no": "SN-1", "subgraph_ref": "SUB-A1",
            "nodes": [{"id": "n1", "properties": {"decision": "PASS"}}]}}
        history = [_tool_msg("query_traceability_graph", payload)]
        assert _has_defect_evidence(history) is False
