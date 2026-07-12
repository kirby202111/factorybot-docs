"""L2 草稿应用服务：分派 + 取证据 + 综合。不重查图（按 subgraph_ref 回查）。

L2 不用 LangGraph：步骤固定（取证据 -> 检索文档 -> 综合），用 async 函数编排 + 策略模式
更简洁。requires_confirmation 恒 True（L2 不落库）。
"""
from __future__ import annotations

import time
import uuid

from app.domain.draft import Draft, DraftKind
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext
from app.infrastructure.obs.observability import Observability


class DraftService:
    def __init__(self, builders: dict[DraftKind, "DraftBuilder"], draft_repo,
                 draft_trace_repo, obs: Observability) -> None:
        self._builders = builders
        self._draft_repo = draft_repo
        self._trace_repo = draft_trace_repo
        self._obs = obs

    async def draft(self, report: DiagnosisReport, draft_kind: DraftKind,
                    tenant: TenantContext) -> Draft:
        builder = self._builders.get(draft_kind)
        if builder is None:
            raise ValueError(f"无对应草稿生成器: {draft_kind}")
        t0 = time.perf_counter()
        draft = await builder.build(report, tenant)
        # 硬性约束：requires_confirmation 恒 True；低置信度需复核
        draft.draft_id = f"D-{uuid.uuid4().hex[:8]}"
        draft.requires_confirmation = True
        if draft.confidence < 0.5:
            draft.needs_review = True
        # 版本一致性三段链第三段：透传 L1 的 route_version
        if not draft.route_version:
            draft.route_version = report.route_version
        await self._draft_repo.archive(draft)
        await self._trace_repo.save_ok(draft_kind.value, draft, t0)
        self._obs.session_finished("L2", "DONE")
        return draft

    async def get_evidence(self, draft_id: str) -> list[dict]:
        return await self._draft_repo.get_evidence(draft_id)
