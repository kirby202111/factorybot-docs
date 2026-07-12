"""write_via_appservice 代码节点：拿 confirmation token 调各上下文应用服务 REST 写落库。

走聚合根不变式 + 事务发件箱，与人工下达同路径。Agent 不碰 MES 原始表。
"""
from __future__ import annotations

from app.domain.tool import ToolRegistry


class WriteViaAppService:
    """通过 L3 工具注册表的写工具（capability=write）调应用服务。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def _invoke(self, tool_name: str, confirmation, tenant, **args) -> dict:
        descriptor = self._registry.get(tool_name)
        if descriptor is None:
            raise RuntimeError(f"写工具未注册: {tool_name}")
        return await descriptor.handler(confirmation=confirmation, tenant=tenant, **args)

    async def write_release(self, state: dict, confirmation) -> dict:
        res = await self._invoke(
            "write_pass_release", confirmation, state["tenant"],
            work_order_id=state["work_order_id"],
        )
        return {"gate_release": "PASS", "release_result": res}

    async def write_isolation(self, state: dict, confirmation) -> dict:
        batches = state.get("isolation_batches", [])
        reason = state.get("isolation_reason", "agent 草拟隔离")
        res = await self._invoke(
            "write_isolation", confirmation, state["tenant"],
            batches=batches, reason=reason,
        )
        return {"isolation_result": res}

    async def write_route_activate(self, state: dict, confirmation) -> dict:
        res = await self._invoke(
            "write_route_activate", confirmation, state["tenant"],
            route_id=state["target_route_id"], version=state["target_route_version"],
        )
        return {"process_switch_result": res}

    async def write_sop_publish(self, state: dict, confirmation) -> dict:
        sop = (state.get("action_card") or {}).get("draft_payload", {})
        res = await self._invoke(
            "write_sop_publish", confirmation, state["tenant"],
            route_id=state["target_route_id"], version=state["target_route_version"],
            sop_content=sop,
        )
        return {"sop_result": res}

    async def write_repair_order(self, state: dict, confirmation) -> dict:
        res = await self._invoke(
            "write_repair_order", confirmation, state["tenant"],
            asset_id=state.get("asset_id", ""), fault_time=state.get("fault_time", ""),
            description=state.get("fault_description", ""),
        )
        return {"repair_order_result": res}
