"""8D 报告草稿生成器：图证据 5M1E 聚类 + 历史 8D 文档检索 -> 8D 草稿。"""
from __future__ import annotations

from app.application.builders.base import apply_version_anchor, extract_version_anchor
from app.domain.draft import Draft, DraftKind
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext


class EightDDraftBuilder:
    draft_kind = DraftKind.EIGHT_D

    def __init__(self, rag_acl, doc_rag_acl, llm) -> None:
        self._rag = rag_acl
        self._doc_rag = doc_rag_acl
        self._llm = llm

    async def build(self, report: DiagnosisReport, tenant: TenantContext) -> Draft:
        nodes = await self._rag.fetch_subgraph_nodes(report.subgraph_ref, tenant)
        anchor = extract_version_anchor(report)
        # 检索历史同类 8D
        history = await self._doc_rag.search_docs(report.summary, tenant, anchor)
        prompt = (
            "你是 MES 8D 报告草拟助手。基于 诊断 + 图证据 + 历史 8D 草拟 8D 报告。\n"
            "约束：1. 只能基于提供的证据。2. payload 含 问题描述/根因/containment/纠正措施。"
            "3. 输出严格遵循 Draft 结构，requires_confirmation 必须为 true。"
        )
        ver = anchor.version if anchor else None
        kind = anchor.kind.value if anchor else None
        user = (f"诊断: {report.summary}\n假设: {[h.statement for h in report.hypotheses]}\n"
                f"历史8D: {[h.get('title') for h in history[:3]]}\n"
                f"version={ver} (kind={kind})")
        draft = await self._llm.ainvoke_structured(
            [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            Draft,
        )
        draft.evidence_refs = [f"subgraph_ref={report.subgraph_ref}"] + report.evidence_refs
        apply_version_anchor(draft, anchor)
        return draft
