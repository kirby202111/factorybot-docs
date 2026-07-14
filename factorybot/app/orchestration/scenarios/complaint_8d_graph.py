"""客诉 8D 场景：嵌入 TraceabilityAgent (C, 复用 诊断) + DraftAgents.draft_8d (D)。（骨架）

  plan -> traceability(C) -> [supplier_trace ‖ isolation_scope] -> gate_isolation_8d
        -> draft_8d(D) -> gate_8d_publish -> done
版本钉死由 ACL 代码做（route_version 强制过滤），C 在此之上做 5M1E 假设排序。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.domain.orchestration_state import OrchestrationState
from app.orchestration.supervisor_graph import SupervisorGraph


def build_complaint_8d_graph(sup: SupervisorGraph, checkpointer):
    g = StateGraph(OrchestrationState)
    g.add_node("plan", sup.plan)
    g.add_node("traceability", sup.run_agent("traceability"))
    g.add_node("supplier_trace", sup.qc.query_supplier_batch_trace)
    g.add_node("isolation_scope", sup.qc.determine_isolation_scope)
    g.add_node("gate_isolation_8d", sup.gate("ISOLATION_8D"))
    g.add_node("draft_8d", sup.run_agent("draft_8d"))
    g.add_node("gate_8d_publish", sup.gate("8D_PUBLISH", capability="draft_8d"))
    g.add_node("done", sup.done)

    g.add_edge(START, "plan")
    g.add_edge("plan", "traceability")
    g.add_conditional_edges("traceability", lambda s: ["supplier_trace", "isolation_scope"])
    g.add_edge("supplier_trace", "gate_isolation_8d")
    g.add_edge("isolation_scope", "gate_isolation_8d")
    g.add_edge("gate_isolation_8d", "draft_8d")
    g.add_edge("draft_8d", "gate_8d_publish")
    g.add_edge("gate_8d_publish", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=checkpointer)
