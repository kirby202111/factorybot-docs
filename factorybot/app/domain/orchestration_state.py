"""编排领域模型：OrchestrationSession / OrchestrationStep / ActionCard / GateDecision / OrchestrationState。

OrchestrationState 是 supervisor StateGraph 的 channel schema；每个字段是一个 channel。
纯代码编排器（supervisor）不持工具、不调 LLM，只做 plan/dispatch/barrier/gate。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Optional, TypedDict

from pydantic import BaseModel, Field


def last_wins(left: Any, right: Any) -> Any:
    """reducer：并发分支同写一channel时取后者（None 不覆盖）。"""
    return right if right is not None else left


class ScenarioType(str, Enum):
    CHANGEOVER = "CHANGEOVER"                  # 换线
    FAULT_RESPONSE = "FAULT_RESPONSE"          # 设备故障复产
    COMPLAINT_8D = "COMPLAINT_8D"              # 客诉 8D


class SessionStatus(str, Enum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"   # gate 超时 / agent 连续失败 / 物料不齐
    DONE = "DONE"
    FAILED = "FAILED"


class NodeType(str, Enum):
    CODE = "CODE"      # 代码节点，不调 LLM
    AGENT = "AGENT"    # agent 节点，调 LLM


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class GateDecisionValue(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    RETRY = "RETRY"


# ---------------------------------------------------------------------------
# ActionCard：动作卡（gate 暂停时推动作卡，人确认后 resume）
# ---------------------------------------------------------------------------

class ActionCard(BaseModel):
    card_id: str
    session_id: str
    step: str                          # FIRST_ARTICLE | PROCESS_SWITCH | RELEASE | DISPOSITION | ...
    capability: Optional[str] = None   # root_cause | fault_impact | ... | None(代码节点)
    intent: str                        # "激活工艺路线 RR-B v4"
    draft_payload: dict = Field(default_factory=dict)
    writes_via: str                    # "工艺管理上下文.application.activate_route"
    requires_confirmation: bool = True
    evidence: list[str] = Field(default_factory=list)
    agent_hypothesis: Optional[dict] = None
    confidence: Optional[str] = None   # high | medium | low
    risk_note: str = ""
    deadline: Optional[datetime] = None

    def writes_via_action(self) -> str:
        """token 绑定的 action 字符串，如 'activate_route:S-xxx'。"""
        parts = self.writes_via.split(".")
        action = parts[-1] if parts else self.writes_via
        return f"{action}:{self.session_id}"


class GateDecision(BaseModel):
    session_id: str
    step: str
    decision: str                      # PASS | REJECT | RETRY
    decided_by: str
    decided_at: datetime = Field(default_factory=datetime.now)
    card_id: str = ""
    token_id: str = ""


class OrchestrationSession(BaseModel):
    session_id: str
    scenario: ScenarioType
    work_order_id: Optional[str] = None
    batch_id: Optional[str] = None
    asset_id: Optional[str] = None
    target_route_id: Optional[str] = None
    target_route_version: Optional[str] = None
    tenant_context: dict = Field(default_factory=dict)
    status: SessionStatus = SessionStatus.PLANNING
    current_step: str = ""
    failure_count: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    suspend_reason: str = ""


class OrchestrationStep(BaseModel):
    record_id: str
    session_id: str
    step: str
    node_type: NodeType
    capability: Optional[str] = None
    status: str = "PENDING"            # PENDING|RUNNING|GATE_WAITING|CONFIRMED|FAILED
    action_card_payload: Optional[dict] = None
    agent_hypothesis: Optional[dict] = None
    agent_confidence: Optional[str] = None
    gate_decision: Optional[str] = None
    tool_call_traces: list[str] = Field(default_factory=list)
    occurred_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# OrchestrationState：supervisor StateGraph 的 channel schema
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# OrchestrationState：supervisor StateGraph 的 channel schema（TypedDict，节点收 dict）
# ---------------------------------------------------------------------------

class OrchestrationState(TypedDict, total=False):
    """每个字段是一个 LangGraph channel。换线/故障/客诉共用一套 schema，
    各场景只装配自己用到的子集。total=False：所有字段可选，节点用 state.get() 取。"""

    # 会话标识
    session_id: str
    scenario: str

    # 租户上下文（dict 形式以便 LangGraph 序列化）
    tenant: Optional[dict]

    # 业务上下文
    work_order_id: Optional[str]
    batch_id: Optional[str]
    asset_id: Optional[str]
    target_route_id: Optional[str]
    target_route_version: Optional[str]

    # 会话状态（并发分支可能同写，用 last_wins reducer）
    status: Annotated[Optional[str], last_wins]
    current_step: Annotated[Optional[str], last_wins]

    # 换线场景步骤结果
    first_article_result: Optional[dict]
    gate_first_article: Optional[str]
    process_switch_result: Optional[dict]
    gate_process_switch: Optional[str]
    tooling_result: Optional[dict]
    kitting_result: Optional[dict]
    barrier_route: Optional[str]
    mismatch_code: Optional[str]
    expected: Optional[str]
    actual: Optional[str]
    agent_hypothesis: Optional[dict]
    agent_confidence: Optional[str]
    action_card: Optional[dict]
    gate_disposition: Optional[str]
    gate_release: Optional[str]
    retry_tooling: bool
    skip_current_step: bool

    # 故障复产场景
    fault_time: Optional[str]
    repair_order_result: Optional[dict]
    gate_repair: Optional[str]
    gate_isolation: Optional[str]
    gate_recalibration: Optional[str]
    gate_restart_first_article: Optional[str]

    # 客诉 8D 场景
    complaint_batch_id: Optional[str]
    traceability_result: Optional[dict]
    supplier_trace_result: Optional[dict]
    isolation_scope_result: Optional[dict]
    gate_8d_publish: Optional[str]

    # agent 调用相关
    pending_tool_calls: list[dict]
    tool_results: list[dict]

    # 写动作透传：gate 确认后把 token 存此，write 节点取用
    confirmation: Optional[dict]
    isolation_batches: list[str]
    isolation_reason: str
    fault_description: str

    # 时间戳
    created_at: str
    updated_at: str
