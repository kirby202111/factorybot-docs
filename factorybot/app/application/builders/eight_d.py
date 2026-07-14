"""8D 报告草稿生成器：图证据 5M1E 聚类 + 历史 8D 文档检索 -> 8D 草稿。"""
from __future__ import annotations

from app.application.builders.base import BaseDraftBuilder
from app.domain.draft import DraftKind
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor


class EightDDraftBuilder(BaseDraftBuilder):
    draft_kind = DraftKind.EIGHT_D

    def __init__(self, rag_acl, doc_rag_acl, llm) -> None:
        super().__init__(llm)
        self._rag = rag_acl          # 图节点 5M1E 聚类留待 phase2（当前用报告 hypotheses）
        self._doc_rag = doc_rag_acl

    async def _fetch_context(self, report: DiagnosisReport,
                             anchor: VersionAnchor | None, tenant: TenantContext):
        # 检索历史同类 8D（版本由 anchor 锁定）
        return await self._doc_rag.search_docs(report.summary, tenant, anchor)

    def _build_prompts(self, report: DiagnosisReport, anchor: VersionAnchor | None,
                       context) -> tuple[str, str]:
        history = context or []
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
        return prompt, user
