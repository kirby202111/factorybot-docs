"""tool_call_id 配对回归：真实模型多步 ReAct 喂回 API 的硬不变量。

assistant 消息里 tool_calls[i].id 必须与对应 tool 消息的 tool_call_id 一致，
否则 OpenAI/DeepSeek 兼容 API 在第二轮（喂回工具结果时）返回 400。
覆盖 ToolCall -> assistant_msg -> ToolNode._tool_msg -> _to_lc 全链路，
无需真实 API（仅依赖主依赖 langchain-core）。
"""
from app.infrastructure.ai.base import ToolCall, assistant_msg
from app.infrastructure.ai.llm_factory import _to_lc
from app.infrastructure.ai.tool_node import _tool_msg


def test_tool_call_id_pairs_across_react_round_trip():
    tc = ToolCall(id="call_abc", name="query_traceability_graph", args={"serial_no": "SN-1"})
    # 模型响应 -> assistant 消息（agent_node 用 tc.model_dump() 透传 id）
    assistant = assistant_msg("thinking", [tc.model_dump()])
    # ToolNode 执行后产 tool 消息（透传同一 id）
    tool = _tool_msg("query_traceability_graph", {"trace_id": "T-1", "data": {}}, tc.id)

    lc_assistant = _to_lc(assistant)
    lc_tool = _to_lc(tool)

    assert lc_assistant.tool_calls, "assistant 消息应还原出 tool_calls"
    assert lc_assistant.tool_calls[0]["id"] == "call_abc"
    assert lc_tool.tool_call_id == "call_abc"
    # 配对不变量：assistant 的 tool_call id == tool 消息的 tool_call_id
    assert lc_assistant.tool_calls[0]["id"] == lc_tool.tool_call_id


def test_tool_call_id_defaults_empty_and_falls_back():
    # mock 不带 id 时仍可构造（向后兼容）；_to_lc 兜底为 "0" 不报错
    tc = ToolCall(name="x", args={})
    assert tc.id == ""
    assistant = assistant_msg("", [tc.model_dump()])
    lc = _to_lc(assistant)
    assert lc.tool_calls[0]["id"] == "0"
