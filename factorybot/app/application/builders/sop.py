"""SOP 草稿生成器：支持同步草拟 + 主动触发（订阅 ProcessRouteActivated 事件）。"""
from __future__ import annotations

from app.application.builders.base import BaseDraftBuilder, apply_version_anchor
from app.domain.draft import Draft, DraftKind
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor, VersionKind


class SopDraftBuilder(BaseDraftBuilder):
    draft_kind = DraftKind.SOP

    def __init__(self, doc_rag_acl, llm) -> None:
        super().__init__(llm)
        self._doc_rag = doc_rag_acl

    async def _fetch_context(self, report: DiagnosisReport,
                             anchor: VersionAnchor | None, tenant: TenantContext):
        return await self._doc_rag.search_docs("SOP", tenant, anchor)

    def _build_prompts(self, report: DiagnosisReport, anchor: VersionAnchor | None,
                       context) -> tuple[str, str]:
        existing = context or []
        prompt = (
            "你是 MES SOP 草拟助手。基于工艺版本草拟新 SOP。\n"
            "约束：1. 只能基于提供的证据。2. payload 含 工序步骤/参数/版本。"
            "3. 输出严格遵循 Draft 结构，requires_confirmation 必须为 true。"
        )
        ver = anchor.version if anchor else None
        kind = anchor.kind.value if anchor else None
        user = (f"诊断: {report.summary}\n参考SOP: {[d.get('title') for d in existing[:3]]}\n"
                f"version={ver} (kind={kind})")
        return prompt, user

    async def build_from_route_activated(self, route_id: str, route_version: str,
                                         tenant: TenantContext) -> Draft:
        """主动触发：订阅 ProcessRouteActivated 事件后草拟新 SOP。

        事件 payload 是 route-specific（route_id/version），此处构造 ROUTE 锚点写入草稿。
        不走 build 模板（无 report/subgraph_ref/evidence_refs）。
        """
        anchor = VersionAnchor(kind=VersionKind.ROUTE, ref_id=route_id, version=route_version)
        existing = await self._doc_rag.search_docs(f"SOP route={route_id}", tenant, None)
        prompt = (
            "你是 MES SOP 草拟助手。基于工艺升版草拟新 SOP。\n"
            "约束同上。输出严格遵循 Draft 结构。"
        )
        user = (f"工艺升版: route_id={route_id}, version={route_version}\n"
                f"旧SOP: {[d.get('title') for d in existing[:3]]}")
        draft = await self._llm.ainvoke_structured(
            [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            Draft,
        )
        draft.intent = f"基于工艺升版 {route_id} v{route_version} 草拟新 SOP"
        apply_version_anchor(draft, anchor)
        return draft
