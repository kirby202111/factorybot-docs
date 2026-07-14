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
    """从 L1 报告提取版本锚点（三段链第三段透传源）。"""
    return report.version_anchor()


def apply_version_anchor(draft: Draft, anchor: VersionAnchor | None) -> None:
    """把版本锚点写回草稿（三段链第三段，覆盖 LLM 可能的空缺/错填）。"""
    if anchor:
        draft.version = anchor.version
        draft.version_kind = anchor.kind.value
        draft.version_ref_id = anchor.ref_id
