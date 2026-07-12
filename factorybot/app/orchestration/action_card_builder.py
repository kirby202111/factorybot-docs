"""动作卡构造：确定性拼装，gate 节点与 orchestrator resume 共用。

gate 节点调 interrupt(value=card) 暂停；orchestrator 检测到 interrupt 后把 card 推给
责任人（WebSocket + Kafka），人确认 -> Command(resume=token) 续跑。
token action = card.writes_via_action() = f"{verb}:{session_id}"，与写 ACL 校验一致。
"""
from __future__ import annotations

import uuid
from typing import Optional

from app.domain.l3_state import ActionCard

# 各 gate 步骤对应的写动作（writes_via）。none.* 表示纯审批无写。
STEP_WRITES_VIA: dict[str, str] = {
    "FIRST_ARTICLE": "none.application.none",
    "PROCESS_SWITCH": "none.application.none",
    "RELEASE": "过点执行上下文.application.release",
    "DISPOSITION": "none.application.none",
    "REPAIR": "设备管理上下文.application.create_repair",
    "ISOLATION": "返工上下文.application.issue_isolation",
    "RECALIBRATION": "none.application.none",
    "RESTART_FIRST_ARTICLE": "none.application.none",
    "ISOLATION_8D": "返工上下文.application.issue_isolation",
    "8D_PUBLISH": "工艺管理上下文.application.publish_sop",
    "SOP_PUBLISH": "工艺管理上下文.application.publish_sop",
    "NEW_ROUTE_FIRST_ARTICLE": "过点执行上下文.application.release",
}


def build_action_card(state: dict, step: str, capability: Optional[str] = None) -> ActionCard:
    """按 step + state 确定性拼装动作卡。"""
    session_id = state.get("session_id", "")
    wo = state.get("work_order_id", "")
    route_id = state.get("target_route_id", "")
    route_ver = state.get("target_route_version", "")
    writes_via = STEP_WRITES_VIA.get(step, "none.application.none")

    intent_map = {
        "FIRST_ARTICLE": f"工单 {wo} 首件核对确认",
        "PROCESS_SWITCH": f"确认工艺路线切换 {route_id} v{route_ver}",
        "RELEASE": f"工单 {wo} 换线核对完成，放行生产",
        "DISPOSITION": "确认根因处置建议（归还/更换钢网后重检）",
        "REPAIR": f"创建设备 {state.get('asset_id','')} 维修单",
        "ISOLATION": "下达批次隔离",
        "RECALIBRATION": "确认设备复校完成",
        "RESTART_FIRST_ARTICLE": "确认复产首件",
        "ISOLATION_8D": "下达同批次隔离",
        "8D_PUBLISH": "发布 8D 报告",
        "SOP_PUBLISH": f"发布 {route_id} v{route_ver} 新 SOP",
        "NEW_ROUTE_FIRST_ARTICLE": "新工艺路线首件放行",
    }
    intent = intent_map.get(step, f"确认 {step}")

    draft_payload: dict = {}
    if step == "RELEASE":
        draft_payload = {"work_order_id": wo}
    elif step == "PROCESS_SWITCH":
        draft_payload = {"route_id": route_id, "version": route_ver}
    elif step == "ISOLATION" or step == "ISOLATION_8D":
        draft_payload = {"batches": state.get("isolation_batches", [])}

    return ActionCard(
        card_id=str(uuid.uuid4()),
        session_id=session_id,
        step=step,
        capability=capability,
        intent=intent,
        draft_payload=draft_payload,
        writes_via=writes_via,
        evidence=state.get("agent_hypothesis", {}).get("evidence", []) if isinstance(state.get("agent_hypothesis"), dict) else [],
        agent_hypothesis=state.get("agent_hypothesis"),
        confidence=state.get("agent_confidence"),
        risk_note="确认后将走应用服务落库（聚合根不变式 + 事务发件箱）" if writes_via != "none.application.none" else "纯审批，无写动作",
    )
