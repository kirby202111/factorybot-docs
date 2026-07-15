"""草稿应用服务：分派 + 取证据 + 综合。不重查图（按 subgraph_ref 回查）。

草稿 不用 LangGraph：步骤固定（取证据 -> 检索文档 -> 综合），用 async 函数编排 + 策略模式
更简洁。requires_confirmation 恒 True（草稿 不落库）。
"""
from __future__ import annotations

import time
import uuid

from app.domain.draft import Draft, DraftKind
from app.domain.errors import ResourceAccessError
from app.domain.report import DiagnosisReport
from app.domain.tenant import TenantContext
from app.infrastructure.obs.logging import get_logger
from app.infrastructure.obs.observability import Observability

_log = get_logger("draft")


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
        # 版本一致性三段链第三段：透传 诊断 的版本锚点
        if not draft.version:
            draft.version = report.version
            draft.version_kind = report.version_kind
            draft.version_ref_id = report.version_ref_id
        await self._draft_repo.archive(draft, tenant.tenant_id)
        await self._trace_repo.save_ok(draft_kind.value, draft, t0)
        self._obs.session_finished("draft", "DONE")
        return draft

    async def get_evidence(self, draft_id: str, tenant: TenantContext) -> list[dict]:
        owner = await self._draft_repo.owner_tenant_id(draft_id)
        if owner is None:
            # 草稿不存在：benign 404，不记安全日志
            raise ResourceAccessError(f"草稿不存在或不属于当前租户: {draft_id}")
        if owner != tenant.tenant_id:
            # 跨租户访问企图：记 warning 供安全审计（对外仍统一 404 隐藏存在性）
            _log.warning(
                "draft.tenant_access_denied",
                draft_id=draft_id, owner_tenant=owner, caller_tenant=tenant.tenant_id,
            )
            raise ResourceAccessError(f"草稿不存在或不属于当前租户: {draft_id}")
        return await self._draft_repo.get_evidence(draft_id)
