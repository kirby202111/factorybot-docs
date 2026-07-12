"""SOP 草稿生成器：支持同步草拟 + 主动触发（订阅 ProcessRouteActivated 事件）。"""
from __future__ import annotations

from app.application.builders.base import extract_route_version
from app.domain.draft import Draft, DraftKind
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext


class SopDraftBuilder:
    draft_kind = DraftKind.SOP

    def __init__(self, doc_rag_acl, llm) -> None:
        self._doc_rag = doc_rag_acl
        self._llm = llm

    async def build(self, report: DiagnosisReport, tenant: TenantContext) -> Draft:
        route_version = extract_route_version(report)
        existing = await self._doc_rag.search_docs(f"SOP", tenant, route_version)
        prompt = (
            "你是 MES SOP 草拟助手。基于工艺版本草拟新 SOP。\n"
            "约束：1. 只能基于提供的证据。2. payload 含 工序步骤/参数/版本。"
            "3. 输出严格遵循 Draft 结构，requires_confirmation 必须为 true。"
        )
        user = f"L1 诊断: {report.summary}\n参考SOP: {[d.get('title') for d in existing[:3]]}\nroute_version={route_version}"
        draft = await self._llm.ainvoke_structured(
            [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            Draft,
        )
        draft.evidence_refs = [f"subgraph_ref={report.subgraph_ref}"] + report.evidence_refs
        draft.route_version = route_version
        return draft

    async def build_from_route_activated(self, route_id: str, route_version: str,
                                         tenant: TenantContext) -> Draft:
        """主动触发：订阅 ProcessRouteActivated 事件后草拟新 SOP。"""
        existing = await self._doc_rag.search_docs(f"SOP route={route_id}", tenant, None)
        prompt = (
            "你是 MES SOP 草拟助手。基于工艺升版草拟新 SOP。\n"
            "约束同上。输出严格遵循 Draft 结构。"
        )
        user = f"工艺升版: route_id={route_id}, version={route_version}\n旧SOP: {[d.get('title') for d in existing[:3]]}"
        draft = await self._llm.ainvoke_structured(
            [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            Draft,
        )
        draft.intent = f"基于工艺升版 {route_id} v{route_version} 草拟新 SOP"
        draft.route_version = route_version
        return draft
