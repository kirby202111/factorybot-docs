"""composition root：构建全部单例服务并装配。

依赖注入在此完成：ACL clients -> 工具注册表 -> LLM -> 可观测 -> 仓库 ->
诊断 服务 / 草稿 服务 / 编排。FastAPI 的 api/deps.py 从这里取单例。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import httpx

from app.application.action_card_dispatcher import ActionCardDispatcher, WebSocketManager
from app.application.diagnosis_service import DiagnosisService
from app.application.draft_service import DraftService
from app.application.orchestration_service import OrchestrationService
from app.application.tools import build_diagnosis_tool_registry, build_orchestration_tool_registry
from app.application.builders.eight_d import EightDDraftBuilder
from app.application.builders.rework_order import ReworkOrderDraftBuilder
from app.application.builders.sop import SopDraftBuilder
from app.config import get_settings
from app.domain.draft import DraftKind
from app.domain.tenant import TenantContext
from app.infrastructure.acl.wiring import build_acl_clients
from app.infrastructure.ai.llm_factory import get_llm
from app.infrastructure.cost.eval_gate import EvalGate
from app.infrastructure.cost.model_router import ModelRouter
from app.infrastructure.cost.result_compactor import ResultCompactor
from app.infrastructure.longtask.session_manager import SessionManager
from app.infrastructure.obs.observability import build_observability
from app.infrastructure.persistence.checkpointer import get_checkpointer
from app.infrastructure.persistence.repos import (
    DraftRepo, DraftTraceRepo, OrchestrationRepo, NodeTraceRepo, ToolCallTraceRepo,
    get_orchestration_repo, get_tool_call_trace_repo,
)
from app.infrastructure.redis_.confirmation_store import ConfirmationStore
from app.infrastructure.redis_.fake_redis import get_redis
from app.orchestration.agents import build_agent_registry
from app.orchestration.code_nodes.barrier import FailureTracker
from app.orchestration.code_nodes.gate import GateManager
from app.orchestration.code_nodes.query_compare import QueryCompareNodes
from app.orchestration.code_nodes.write_via_appservice import WriteViaAppService
from app.orchestration.scenarios import SCENARIO_SPECS, ScenarioGraphBuilder
from app.orchestration.supervisor_graph import SupervisorGraph


class Container:
    """单例容器。首次访问时惰性装配。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        # 可观测
        self.obs = build_observability()
        # 存储
        self.redis = get_redis()
        self.confirmation_store = ConfirmationStore(self.redis, self.settings.confirmation_token_ttl)
        self.checkpointer = get_checkpointer()
        self.tool_trace_repo = get_tool_call_trace_repo()
        self.draft_repo = DraftRepo()
        self.draft_trace_repo = DraftTraceRepo()
        self.node_trace_repo = NodeTraceRepo()
        self.orchestration_repo = get_orchestration_repo()
        # LLM
        self.llm = get_llm(self.obs)
        # ACL（real 模式共享 httpx.AsyncClient；shutdown 时由 Container 统一 aclose）
        self._http = None if self.settings.is_mock else httpx.AsyncClient(timeout=3.0)
        self.acl = build_acl_clients(
            mock=self.settings.is_mock, fixtures=None, http=self._http,
            confirmation_store=self.confirmation_store,
        )
        # 工具注册表
        self.diagnosis_registry = build_diagnosis_tool_registry(self.acl)
        self.orchestration_registry = build_orchestration_tool_registry(self.acl)
        # 成本
        self.eval_gate = EvalGate()
        self.model_router = ModelRouter(
            self.eval_gate, allow_mock=self.settings.is_mock,
            active_model=self.settings.llm_model,
        )
        self.result_compactor = ResultCompactor()
        # 诊断 服务
        self.diagnosis_service = DiagnosisService(
            self.diagnosis_registry, self.llm, self.tool_trace_repo, self.obs,
            result_compactor=self.result_compactor,
        )
        # 草稿 服务
        self.builders = {
            DraftKind.REWORK_ORDER: ReworkOrderDraftBuilder(self.acl.rag, self.acl.process, self.llm),
            DraftKind.EIGHT_D: EightDDraftBuilder(self.acl.rag, self.acl.doc_rag, self.llm),
            DraftKind.SOP: SopDraftBuilder(self.acl.doc_rag, self.llm),
        }
        self.draft_service = DraftService(
            self.builders, self.draft_repo, self.draft_trace_repo, self.obs,
        )
        # 编排
        self.dispatcher = ActionCardDispatcher(WebSocketManager())
        self.failure_tracker = FailureTracker(self.orchestration_repo, self.settings.failure_threshold)
        self.gate_manager = GateManager(self.orchestration_repo)
        self.query_compare = QueryCompareNodes(
            self.acl.tooling, self.acl.process, self.acl.material, self.orchestration_repo,
        )
        self.write_service = WriteViaAppService(self.orchestration_registry)
        self.agents = build_agent_registry(
            self.llm, self.orchestration_registry, self.diagnosis_registry, self.tool_trace_repo, self.obs,
            self.result_compactor,
        )
        self.supervisor = SupervisorGraph(
            self.query_compare, self.agents, self.gate_manager, self.orchestration_repo,
            self.failure_tracker, self.dispatcher, self.write_service,
        )
        _scenario_builder = ScenarioGraphBuilder(self.supervisor, self.checkpointer)
        self.graphs = {
            name: _scenario_builder.build(spec) for name, spec in SCENARIO_SPECS.items()
        }
        self.session_manager = SessionManager(self.orchestration_repo)
        self.orchestration_service = OrchestrationService(
            self.graphs, self.session_manager, self.orchestration_repo,
            self.confirmation_store, self.dispatcher,
        )

    # ---- 启动断言 ----
    def validate_on_startup(self) -> None:
        """三层写防线启动断言。"""
        self.diagnosis_registry.validate_on_startup()
        self.orchestration_registry.validate_on_startup()
        self.model_router.validate_on_startup()

    async def shutdown(self) -> None:
        """释放资源：httpx AsyncClient 连接池（real 模式下由 ACL 共享）。"""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def default_tenant(self) -> TenantContext:
        s = self.settings
        return TenantContext(
            tenant_id=s.default_tenant_id, workshop=s.default_workshop,
            line=s.default_line, role="ENGINEER", user_id="u_zhang",
            scopes=TenantContext.default().scopes,
        )


_container: Optional[Container] = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = Container()
    return _container


def reset_container() -> None:
    """测试用：重置容器（清进程内状态）。"""
    global _container
    _container = None
    # 重置 LLM 单例，确保下次用新容器的 obs
    from app.infrastructure.ai import llm_factory
    llm_factory._llm = None
