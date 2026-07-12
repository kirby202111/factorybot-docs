"""工艺变更落地场景：嵌入 DraftAgents.draft_sop (D)。（骨架）

  plan -> [draft_sop(D) ‖ qualification_check] -> barrier -> gate_sop_publish
        -> gate_new_route_first_article -> done
SOP 草拟嵌 D（开放生成），资质核对是确定性查询（代码节点）。触发源：ProcessRouteActivated。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.domain.l3_state import L3State
from app.orchestration.supervisor_graph import SupervisorGraph


def build_process_change_graph(sup: SupervisorGraph, checkpointer):
    g = StateGraph(L3State)
    g.add_node("plan", sup.plan)
    g.add_node("draft_sop", sup.run_agent("draft_sop"))
    g.add_node("qualification_check", sup.qc.check_operator_qualification)
    g.add_node("barrier", sup.barrier)
    g.add_node("gate_sop_publish", sup.gate("SOP_PUBLISH", capability="draft_sop"))
    g.add_node("gate_new_route_first_article", sup.gate("NEW_ROUTE_FIRST_ARTICLE"))
    g.add_node("done", sup.done)

    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda s: ["draft_sop", "qualification_check"])
    g.add_edge("draft_sop", "barrier")
    g.add_edge("qualification_check", "barrier")
    g.add_edge("barrier", "gate_sop_publish")
    g.add_edge("gate_sop_publish", "gate_new_route_first_article")
    g.add_edge("gate_new_route_first_article", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=checkpointer)
