"""DraftBuilder 协议 + 证据提取助手。"""
from __future__ import annotations

from typing import Protocol

from app.domain.draft import Draft
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor


class DraftBuilder(Protocol):
    draft_kind: str  # DraftKind

    async def build(self, report: DiagnosisReport, tenant: TenantContext) -> Draft: ...


# ---- 节点提取助手（从 subgraph 节点列表里按 label 找字段）----
def extract_node(nodes: list[dict], label: str) -> dict | None:
    for n in nodes:
        if n.get("label") == label:
            return n.get("props", n)
    return None


def extract_sn_list(nodes: list[dict]) -> list[str]:
    out = []
    for n in nodes:
        if n.get("label") == "WipUnit":
            sn = n.get("props", {}).get("sn") or n.get("node_id")
            if sn:
                out.append(sn)
    return out


def extract_version_anchor(report: DiagnosisReport) -> VersionAnchor | None:
    """从 诊断 报告提取版本锚点（三段链第三段透传源）。"""
    return report.version_anchor()


def apply_version_anchor(draft: Draft, anchor: VersionAnchor | None) -> None:
    """把版本锚点写回草稿（三段链第三段，覆盖 LLM 可能的空缺/错填）。"""
    if anchor:
        draft.version = anchor.version
        draft.version_kind = anchor.kind.value
        draft.version_ref_id = anchor.ref_id


class BaseDraftBuilder:
    """草稿生成器模板方法基类。

    build() 固化 fetch->anchor->prompt->LLM->evidence_refs->apply_anchor 骨架；
    子类只实现 _fetch_context（取额外证据）+ _build_prompts（构造 system/user）。
    满足 DraftBuilder Protocol。
    """

    draft_kind: str  # DraftKind，子类设置

    def __init__(self, llm) -> None:
        self._llm = llm

    async def build(self, report: DiagnosisReport, tenant: TenantContext) -> Draft:
        anchor = extract_version_anchor(report)
        context = await self._fetch_context(report, anchor, tenant)
        prompt, user = self._build_prompts(report, anchor, context)
        draft = await self._llm.ainvoke_structured(
            [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            Draft,
        )
        draft.evidence_refs = [f"subgraph_ref={report.subgraph_ref}"] + report.evidence_refs
        apply_version_anchor(draft, anchor)
        return draft

    async def _fetch_context(self, report: DiagnosisReport,
                             anchor: VersionAnchor | None, tenant: TenantContext):
        """子类覆写：取额外证据（图节点/历史文档等）。默认无。"""
        return None

    def _build_prompts(self, report: DiagnosisReport, anchor: VersionAnchor | None,
                       context) -> tuple[str, str]:
        """子类覆写：返回 (system_prompt, user_message)。"""
        raise NotImplementedError
