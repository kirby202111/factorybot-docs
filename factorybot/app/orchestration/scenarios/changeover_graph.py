"""换线场景：代码骨架 + 仅 mismatch 分支嵌入 RootCauseAgent (A)。

全程 PASS 时 LLM 调用 = 0。步骤：
  plan -> first_article -> gate_first_article -> process_switch -> gate_process_switch
  -> [tooling_check ‖ kitting_check] -> barrier
     ├ 都 PASS -> draft_release -> gate_release -> done
     ├ tooling FAIL -> root_cause(A) -> gate_disposition -> done
     └ kitting FAIL -> SUSPENDED
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.domain.l3_state import L3State
from app.orchestration.code_nodes.barrier import barrier_route
from app.orchestration.supervisor_graph import SupervisorGraph


def build_changeover_graph(sup: SupervisorGraph, checkpointer):
    g = StateGraph(L3State)

    g.add_node("plan", sup.plan)
    g.add_node("first_article", sup.qc.query_first_article)
    g.add_node("gate_first_article", sup.gate("FIRST_ARTICLE"))
    g.add_node("process_switch", sup.qc.query_active_route)
    g.add_node("gate_process_switch", sup.gate("PROCESS_SWITCH"))
    g.add_node("tooling_check", sup.qc.query_and_compare_tooling)
    g.add_node("kitting_check", sup.qc.query_and_compare_kitting)
    g.add_node("barrier", sup.barrier)
    g.add_node("draft_release", sup.qc.draft_release_card)
    g.add_node("gate_release", sup.gate("RELEASE"))
    g.add_node("root_cause", sup.run_agent("root_cause"))
    g.add_node("gate_disposition", sup.gate("DISPOSITION", capability="root_cause"))
    g.add_node("done", sup.done)

    g.add_edge(START, "plan")
    g.add_edge("plan", "first_article")
    g.add_edge("first_article", "gate_first_article")
    g.add_edge("gate_first_article", "process_switch")
    g.add_edge("process_switch", "gate_process_switch")
    # 并行派发
    g.add_conditional_edges("gate_process_switch", lambda s: ["tooling_check", "kitting_check"])
    g.add_edge("tooling_check", "barrier")
    g.add_edge("kitting_check", "barrier")
    # barrier 确定性分流
    g.add_conditional_edges("barrier", barrier_route, {
        "draft_release": "draft_release",
        "root_cause": "root_cause",
        "suspend": END,
    })
    g.add_edge("draft_release", "gate_release")
    g.add_edge("gate_release", "done")
    g.add_edge("root_cause", "gate_disposition")
    g.add_conditional_edges("gate_disposition",
        lambda s: "tooling_check" if s.get("retry_tooling") else "done",
        {"tooling_check": "tooling_check", "done": "done"},
    )
    g.add_edge("done", END)
    return g.compile(checkpointer=checkpointer)
