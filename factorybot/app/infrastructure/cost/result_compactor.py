"""ResultCompactor：工具结果回灌前压缩（字段裁剪 + 列表截断 + 摘要）。

模型看摘要，trace 落全文。截断标注 _truncated / _omitted_count 让模型知道数据不全。
降本不牺牲证据链完整性。
"""
from __future__ import annotations

from typing import Any

# 每类工具结果的保留字段白名单（其余裁剪）
FIELD_WHITELIST: dict[str, list[str]] = {
    "query_pass_records": ["sn", "work_order_id", "station_id", "equipment_id", "route_version", "decision", "blocking_reason"],
    "query_test_results": ["test_id", "station_id", "test_type", "raw_verdict"],
    "query_traceability_graph": ["serial_no", "subgraph_ref", "route_version"],
}

LIST_TRUNCATE = 5  # 列表最多保留前 5 项


class ResultCompactor:
    def __init__(self, truncate: int = LIST_TRUNCATE) -> None:
        self._truncate = truncate

    def compact(self, tool_name: str, view: Any) -> dict:
        """返回压缩后的摘要 dict，带 _truncated/_omitted_count 标注。"""
        if not isinstance(view, dict):
            return {"_summary": str(view)[:200]}
        whitelist = FIELD_WHITELIST.get(tool_name)
        omitted = 0
        out: dict = {}
        if whitelist:
            for k in whitelist:
                if k in view:
                    out[k] = view[k]
            omitted = len(view) - len(out)
        else:
            out = dict(view)
        # 列表截断
        for k, v in list(out.items()):
            if isinstance(v, list) and len(v) > self._truncate:
                omitted += len(v) - self._truncate
                out[k] = v[:self._truncate]
                out[f"_{k}_truncated"] = True
        if omitted:
            out["_omitted_count"] = omitted
        return out
