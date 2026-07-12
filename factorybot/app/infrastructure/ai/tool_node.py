"""ToolNode：执行 pending_tool_calls，记 trace，产出 tool 消息。

- 调用前按 capability + TenantContext scope 过滤（权限隔离）。
- 工具结果双路：模型看摘要（ResultCompactor），trace 落全文（证据链）。
  本实现把全文 + trace_id 放进 tool 消息，模型可读；ResultCompactor 可在入模型前压缩。
- 每次调用落一行 tool_call_trace（OK/DENIED/ERROR）。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from app.domain.tenant import TenantContext
from app.domain.tool import ToolRegistry
from app.infrastructure.persistence.repos import ToolCallTraceRepo


def tool_to_schema(descriptor) -> dict:
    """ToolDescriptor -> OpenAI function schema（传给真实 LLM）。"""
    params = {}
    if descriptor.args_schema is not None:
        try:
            params = descriptor.args_schema.model_json_schema()
        except Exception:
            params = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": descriptor.name,
            "description": descriptor.description,
            "parameters": params,
        },
    }


class ToolNode:
    """ReAct 工具执行节点。state 须含 pending_tool_calls/tenant/obs_ctx/step_no。"""

    def __init__(
        self,
        registry: ToolRegistry,
        trace_repo: ToolCallTraceRepo,
        obs=None,
        capability: str = "l1",
    ) -> None:
        self._registry = registry
        self._trace_repo = trace_repo
        self._obs = obs
        self._capability = capability

    async def __call__(self, state: dict) -> dict:
        tenant: TenantContext = state["tenant"]
        obs_ctx = state.get("obs_ctx")
        step_no = state.get("step_no", 0)
        new_tool_msgs: list[dict] = []
        for call in state.get("pending_tool_calls", []):
            name = call.get("name") if isinstance(call, dict) else call.name
            args = call.get("args", {}) if isinstance(call, dict) else call.args
            # 透传 tool_call_id，与 assistant 消息里的 tool_calls 配对（真实 API 多步 ReAct 必需）
            tool_call_id = call.get("id", "") if isinstance(call, dict) else getattr(call, "id", "")
            descriptor = self._registry.get(name)
            # 权限：必须在 capability 工具集内 + 租户 scope
            allowed = descriptor and descriptor.capability == self._capability \
                and tenant.can_access(descriptor.required_tenant_scopes)
            if not allowed:
                tid = await self._trace_repo.save_denied(
                    tool_name=name, session_id=_sid(state), step_no=step_no,
                    tenant_id=tenant.tenant_id,
                )
                if self._obs:
                    self._obs.tool_denied(name)
                new_tool_msgs.append(_tool_msg(name, {"trace_id": tid, "error": "DENIED"}, tool_call_id))
                continue
            t0 = time.perf_counter()
            try:
                # 入参校验
                validated = args
                if descriptor.args_schema is not None:
                    validated = descriptor.args_schema.model_validate(args).model_dump()
                view = await descriptor.handler(**validated, tenant=tenant)
                view_dict = _serialize(view)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                tid = await self._trace_repo.save_ok(
                    tool_name=name, bounded_context=descriptor.bounded_context,
                    args=validated, view=view_dict, latency_ms=latency_ms,
                    session_id=_sid(state), step_no=step_no, tenant_id=tenant.tenant_id,
                )
                if self._obs:
                    self._obs.tool_ok(name, latency_ms / 1000.0)
                # 模型看 trace_id + 全文（证据链）；真实模式下可由 ResultCompactor 压缩
                new_tool_msgs.append(_tool_msg(name, {"trace_id": tid, "data": view_dict}, tool_call_id))
            except Exception as e:
                if self._obs:
                    self._obs.tool_error(name)
                tid = await self._trace_repo.save_error(
                    tool_name=name, session_id=_sid(state), step_no=step_no,
                    tenant_id=tenant.tenant_id, error=e,
                )
                new_tool_msgs.append(_tool_msg(name, {"trace_id": tid, "error": str(e)}, tool_call_id))
        return {"pending_tool_calls": [], "messages": new_tool_msgs}


def _tool_msg(name: str, payload: dict, tool_call_id: str = "") -> dict:
    return {"role": "tool", "name": name, "tool_call_id": tool_call_id,
            "content": json.dumps(payload, ensure_ascii=False, default=str)}


def _serialize(view: Any) -> Any:
    """把 Pydantic 模型 / 嵌套 list[Pydantic] 转为纯 dict/list，便于 JSON 序列化。"""
    if hasattr(view, "model_dump"):
        return view.model_dump()
    if isinstance(view, list):
        return [_serialize(v) for v in view]
    if isinstance(view, dict):
        return {k: _serialize(v) for k, v in view.items()}
    return view


def _sid(state: dict) -> str:
    o = state.get("obs_ctx")
    return getattr(o, "session_id", "") if o else ""
