"""返工单草稿生成器：诊断 + 图证据 -> BatchReworkOrder 草稿。"""
from __future__ import annotations

from app.application.builders.base import (
    BaseDraftBuilder,
    extract_node,
    extract_sn_list,
)
from app.domain.draft import DraftKind
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor


class ReworkOrderDraftBuilder(BaseDraftBuilder):
    draft_kind = DraftKind.REWORK_ORDER

    def __init__(self, rag_acl, process_acl, llm) -> None:
        super().__init__(llm)
        self._rag = rag_acl
        self._process = process_acl

    async def _fetch_context(self, report: DiagnosisReport,
                             anchor: VersionAnchor | None, tenant: TenantContext):
        # 草稿 按 subgraph_ref 回查图节点，不重查图
        return await self._rag.fetch_subgraph_nodes(report.subgraph_ref, tenant)

    def _build_prompts(self, report: DiagnosisReport, anchor: VersionAnchor | None,
                       context) -> tuple[str, str]:
        nodes = context or []
        wo_node = extract_node(nodes, "WorkOrder") or {}
        source_wo = wo_node.get("work_order_id", "")
        sn_list = extract_sn_list(nodes) or [report.subgraph_ref]
        prompt = (
            "你是 MES 返工单草拟助手。基于 诊断 + 图证据草拟 BatchReworkOrder。\n"
            "约束：1. 只能基于提供的证据，不得编造 SN 或工单。"
            "2. intent 一句话说明要返工什么、再入点在哪。"
            "3. payload 含 source_work_order_id / affected_sn_list / reentry_point / rework_route_ref。"
            "4. 输出严格遵循 Draft 结构，requires_confirmation 必须为 true。"
        )
        ver = anchor.version if anchor else None
        kind = anchor.kind.value if anchor else None
        user = (f"诊断: {report.summary}\n"
                f"证据: source_work_order_id={source_wo}, affected_sn_list={sn_list}, "
                f"reentry_point=OP-REFLOW, rework_route_ref=RR-RW-1, version={ver} (kind={kind})")
        return prompt, user
