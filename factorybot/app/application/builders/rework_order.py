"""返工单草稿生成器：L1 诊断 + 图证据 -> BatchReworkOrder 草稿。"""
from __future__ import annotations

from app.application.builders.base import extract_node, extract_route_version, extract_sn_list
from app.domain.draft import Draft, DraftKind
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext


class ReworkOrderDraftBuilder:
    draft_kind = DraftKind.REWORK_ORDER

    def __init__(self, rag_acl, process_acl, llm) -> None:
        self._rag = rag_acl
        self._process = process_acl
        self._llm = llm

    async def build(self, report: DiagnosisReport, tenant: TenantContext) -> Draft:
        # L2 按 subgraph_ref 回查图节点，不重查图
        nodes = await self._rag.fetch_subgraph_nodes(report.subgraph_ref, tenant)
        wo_node = extract_node(nodes, "WorkOrder") or {}
        source_wo = wo_node.get("work_order_id", "")
        sn_list = extract_sn_list(nodes) or [report.subgraph_ref]
        route_version = extract_route_version(report)

        prompt = (
            "你是 MES 返工单草拟助手。基于 L1 诊断 + 图证据草拟 BatchReworkOrder。\n"
            "约束：1. 只能基于提供的证据，不得编造 SN 或工单。"
            "2. intent 一句话说明要返工什么、再入点在哪。"
            "3. payload 含 source_work_order_id / affected_sn_list / reentry_point / rework_route_ref。"
            "4. 输出严格遵循 Draft 结构，requires_confirmation 必须为 true。"
        )
        user = (f"L1 诊断: {report.summary}\n"
                f"证据: source_work_order_id={source_wo}, affected_sn_list={sn_list}, "
                f"reentry_point=OP-REFLOW, rework_route_ref=RR-RW-1, route_version={route_version}")
        draft = await self._llm.ainvoke_structured(
            [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            Draft,
        )
        draft.evidence_refs = [f"subgraph_ref={report.subgraph_ref}"] + report.evidence_refs
        draft.route_version = route_version
        return draft
