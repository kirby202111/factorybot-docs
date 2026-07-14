"""确定性查询比对节点：不调 LLM，ACL 调用 + 结构化比对。

换线钢网/程序/齐套比对是确定性规则，代码节点即可，非确定分支才交 agent。
"""
from __future__ import annotations

from app.domain.tenant import TenantContext
from app.infrastructure.acl.material import MaterialAclClient
from app.infrastructure.acl.process_management import ProcessManagementAclClient
from app.infrastructure.acl.tooling import ToolingAclClient
from app.infrastructure.persistence.repos import OrchestrationRepo


class QueryCompareNodes:
    def __init__(
        self,
        tooling_acl: ToolingAclClient,
        route_acl: ProcessManagementAclClient,
        material_acl: MaterialAclClient,
        repo: OrchestrationRepo,
    ) -> None:
        self._tooling_acl = tooling_acl
        self._route_acl = route_acl
        self._material_acl = material_acl
        self._repo = repo

    def _tenant(self, state: dict) -> TenantContext | None:
        t = state.get("tenant")
        if t is None or isinstance(t, TenantContext):
            return t
        return TenantContext.model_validate(t)

    # ---- 换线场景 ----
    async def query_first_article(self, state: dict) -> dict:
        tenant = self._tenant(state)
        fa = await self._route_acl.query_first_article_status(state["work_order_id"], tenant)
        res = {"status": fa.status, "article_id": fa.article_id}
        await self._repo.save_step(state["session_id"], "FIRST_ARTICLE", "CODE", res)
        return {"first_article_result": res, "current_step": "FIRST_ARTICLE"}

    async def query_active_route(self, state: dict) -> dict:
        tenant = self._tenant(state)
        route = await self._route_acl.query_route(
            state["target_route_id"], state["target_route_version"], tenant,
        )
        res = {"route_id": route.route_id, "version": route.version, "status": route.status}
        await self._repo.save_step(state["session_id"], "PROCESS_SWITCH", "CODE", res)
        return {"process_switch_result": res, "current_step": "PROCESS_SWITCH"}

    async def query_and_compare_tooling(self, state: dict) -> dict:
        """钢网/程序结构化比对（代码节点）。

        expected 来自工艺路线（ACL 强制 route_version），actual 来自产线扫码 + 设备本地程序。
        """
        tenant = self._tenant(state)
        route = await self._route_acl.query_route(
            state["target_route_id"], state["target_route_version"], tenant,
        )
        expected_stencil = route.tooling.stencil_id
        expected_program = route.tooling.program_id
        actual_stencil = (await self._tooling_acl.query_current_stencil(state["asset_id"], tenant)).stencil_id
        actual_program = (await self._tooling_acl.query_local_program_version(state["asset_id"], tenant)).program_id

        if actual_stencil != expected_stencil:
            result = {"status": "FAIL", "code": "TOOLING_STENCIL_MISMATCH",
                      "expected": expected_stencil, "actual": actual_stencil}
        elif actual_program != expected_program:
            result = {"status": "FAIL", "code": "PROGRAM_VERSION_NOT_ACTIVE",
                      "expected": expected_program, "actual": actual_program}
        else:
            result = {"status": "PASS"}
        await self._repo.save_step(state["session_id"], "TOOLING_CHECK", "CODE", result)
        return {"tooling_result": result, "current_step": "TOOLING_CHECK"}

    async def query_and_compare_kitting(self, state: dict) -> dict:
        """齐套比对：kit_rate == 100% ? PASS : FAIL(缺料)。"""
        tenant = self._tenant(state)
        kit = await self._material_acl.query_kit_status(state["work_order_id"], tenant)
        if kit.kit_rate >= 100 and not kit.missing_items:
            result = {"status": "PASS"}
        else:
            result = {"status": "FAIL", "kit_rate": kit.kit_rate,
                      "missing_materials": kit.missing_material_ids}
        await self._repo.save_step(state["session_id"], "KITTING_CHECK", "CODE", result)
        return {"kitting_result": result, "current_step": "KITTING_CHECK"}

    async def draft_release_card(self, state: dict) -> dict:
        """结构化拼装放行卡（代码节点，非 LLM）。"""
        card = {"intent": f"工单 {state['work_order_id']} 换线核对完成，放行生产",
                "writes_via": "过点执行上下文.application.release",
                "requires_confirmation": True}
        return {"action_card": card, "current_step": "DRAFT_RELEASE"}

    # ---- 故障复产场景 ----
    async def draft_repair_order(self, state: dict) -> dict:
        res = {"asset_id": state.get("asset_id"), "fault_time": state.get("fault_time"),
               "status": "DRAFT"}
        return {"repair_order_result": res, "current_step": "DRAFT_REPAIR"}

    # ---- 客诉场景 ----
    async def query_supplier_batch_trace(self, state: dict) -> dict:
        tenant = self._tenant(state)
        bid = state.get("complaint_batch_id") or state.get("batch_id")
        res = await self._material_acl.query_supplier_trace(bid, tenant)
        return {"supplier_trace_result": res.model_dump()}

    async def determine_isolation_scope(self, state: dict) -> dict:
        return {"isolation_scope_result": {"batches": [], "reason": "determined_by_code"}}

    # ---- 工艺变更场景 ----
    async def check_operator_qualification(self, state: dict) -> dict:
        tenant = self._tenant(state)
        res = await self._route_acl.check_qualification(
            state["target_route_id"], state["target_route_version"], tenant,
        )
        return {"qualification_result": res.model_dump()}
