"""诊断应用服务：构建 LangGraph 图、驱动 ReAct 循环、置信度兜底转人工。

诊断 全程只读（ReadOnlyToolGate 在注册期 + 启动期已断言）。recursion_limit=20 硬上限，
asyncio.wait_for 整体超时兜底。confidence < 阈值 -> needs_human_review。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from langgraph.errors import GraphRecursionError

from app.config import get_settings
from app.domain.report import DiagnosisReport
from app.domain.session import DiagnosisSession, SessionStatus
from app.domain.tenant import TenantContext
from app.domain.version import VersionAnchor
from app.infrastructure.ai.graph_builder import build_diagnosis_graph
from app.infrastructure.ai.tool_node import ToolNode
from app.infrastructure.obs.context import ObservabilityContext
from app.infrastructure.obs.observability import Observability


class DiagnosisService:
    def __init__(
        self,
        registry,
        llm,
        trace_repo,
        obs: Observability,
        recursion_limit: int | None = None,
        timeout: float | None = None,
        conf_threshold: float | None = None,
    ) -> None:
        self._registry = registry
        self._llm = llm
        self._trace_repo = trace_repo
        self._obs = obs
        s = get_settings()
        self._recursion_limit = recursion_limit or s.diagnosis_recursion_limit
        self._timeout = timeout or s.diagnosis_session_timeout
        self._conf_threshold = conf_threshold or s.diagnosis_confidence_threshold

    async def diagnose(self, question: str, tenant: TenantContext,
                       serial_no: str | None = None,
                       work_order_id: str | None = None,
                       version_anchor: VersionAnchor | None = None,
                       subgraph_ref: str | None = None) -> DiagnosisReport:
        anchor_flat = version_anchor.to_flat() if version_anchor else {}
        session = DiagnosisSession(
            session_id=f"S-DIAG-{uuid.uuid4().hex[:8]}",
            tenant=tenant, question=question,
            serial_no=serial_no, work_order_id=work_order_id,
            version=anchor_flat.get("version"),
            version_kind=anchor_flat.get("version_kind"),
            version_ref_id=anchor_flat.get("version_ref_id"),
            subgraph_ref=subgraph_ref,
        )
        obs_ctx = ObservabilityContext(
            session_id=session.session_id, trace_id=uuid.uuid4().hex[:32],
            tenant_id=tenant.tenant_id, workshop=tenant.workshop, line=tenant.line,
            level="diagnosis", prompt_version=session.prompt_version, step_no=0,
        )
        self._obs.session_started("diagnosis")
        graph = build_diagnosis_graph(
            self._llm, self._registry, self._trace_repo, self._obs,
            capability="diagnosis", recursion_limit=self._recursion_limit,
        )
        initial = {
            "tenant": tenant, "obs_ctx": obs_ctx, "question": question,
            "serial_no": serial_no, "work_order_id": work_order_id,
            "version": anchor_flat.get("version"),
            "version_kind": anchor_flat.get("version_kind"),
            "version_ref_id": anchor_flat.get("version_ref_id"),
            "subgraph_ref": subgraph_ref,
            "step_no": 0, "messages": [], "pending_tool_calls": [],
        }
        report: DiagnosisReport
        try:
            with self._obs.session_span(obs_ctx):
                final = await asyncio.wait_for(
                    graph.ainvoke(initial, config={"recursion_limit": self._recursion_limit}),
                    timeout=self._timeout,
                )
            report_dict = final.get("report")
            report = DiagnosisReport.model_validate(report_dict) if report_dict \
                else DiagnosisReport.partial("图未产出报告", subgraph_ref or "")
        except GraphRecursionError:
            self._obs.recursion_limit_hit("diagnosis")
            report = DiagnosisReport.partial("步数超限 (recursion_limit)", subgraph_ref or "")
            session.status = SessionStatus.TIMEOUT
        except asyncio.TimeoutError:
            report = DiagnosisReport.partial("整体超时", subgraph_ref or "")
            session.status = SessionStatus.TIMEOUT
        except Exception as e:
            report = DiagnosisReport.partial(f"异常: {e}", subgraph_ref or "")
            session.status = SessionStatus.FAILED

        # 置信度兜底
        if report.confidence < self._conf_threshold:
            report.needs_human_review = True
            self._obs.low_confidence("diagnosis")
        if not report.needs_human_review:
            session.status = SessionStatus.DONE
        self._obs.session_finished("diagnosis", session.status.value)
        session.touch()
        return report
