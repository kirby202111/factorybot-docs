"""设备故障复产场景：代码骨架 + 嵌入 FaultImpactAgent (B)。（骨架，可编译运行）

  plan -> [draft_repair_order ‖ fault_impact(B)] -> barrier
        -> [gate_repair ‖ gate_isolation] -> write(维修单+隔离) -> gate_recalibration
        -> gate_restart_first_article -> done
隔离范围判定嵌 B（非确定），复校/复产 gate 是代码节点（agent 不碰红线）。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.domain.l3_state import L3State
from app.orchestration.supervisor_graph import SupervisorGraph, _token_from_dict


def _fault_barrier(state: dict) -> str:
    if state.get("agent_hypothesis") is not None:
        return "gates"
    return "suspend"


def build_fault_response_graph(sup: SupervisorGraph, checkpointer):
    g = StateGraph(L3State)
    g.add_node("plan", sup.plan)
    g.add_node("draft_repair_order", sup.qc.draft_repair_order)
    g.add_node("fault_impact", sup.run_agent("fault_impact"))
    g.add_node("gate_repair", sup.gate("REPAIR"))
    g.add_node("gate_isolation", sup.gate("ISOLATION", capability="fault_impact"))
    g.add_node("gate_recalibration", sup.gate("RECALIBRATION"))
    g.add_node("gate_restart_first_article", sup.gate("RESTART_FIRST_ARTICLE"))
    g.add_node("done", sup.done)

    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda s: ["draft_repair_order", "fault_impact"])
    g.add_edge("draft_repair_order", "gate_repair")
    g.add_edge("fault_impact", "gate_isolation")
    g.add_edge("gate_repair", "gate_recalibration")
    g.add_edge("gate_isolation", "gate_recalibration")
    g.add_edge("gate_recalibration", "gate_restart_first_article")
    g.add_edge("gate_restart_first_article", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=checkpointer)
