"""场景图声明式装配：ScenarioSpec + ScenarioGraphBuilder。

3 个场景（changeover/fault_response/complaint_8d）的 build_*_graph 曾各自
手写 StateGraph 装配（~80% 雷同）。此处用 dataclass spec 声明节点/边，由 ScenarioGraphBuilder
统一装配，消除重复；新增场景只需追加一个 ScenarioSpec。

纯装配去重，不改运行时行为（节点/边与原图一一对应）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from langgraph.graph import END, START, StateGraph

from app.domain.orchestration_state import OrchestrationState
from app.orchestration.code_nodes.barrier import barrier_route

# spec 里用 "START"/"END" 字符串代表 langgraph START/END，装配时解析
_START = "__START__"
_END = "__END__"


def _resolve(node: str):
    return START if node == _START else (END if node == _END else node)


@dataclass
class NodeSpec:
    """节点声明：name + factory(sup)->节点可调用对象。"""
    name: str
    factory: Callable[[Any], Callable]


@dataclass
class EdgeSpec:
    """边声明。三种形态互斥：
    - 普通边：to=str（含 _START/_END）
    - 并行派发：to=list[str]（langgraph 并行执行）
    - 分流：to=None + cond + path_map（条件边，path_map 值可为 _END）
    """
    from_node: str
    to: Union[str, list[str], None] = None
    cond: Optional[Callable] = None
    path_map: Optional[dict] = None


@dataclass
class ScenarioSpec:
    name: str
    nodes: list[NodeSpec] = field(default_factory=list)
    edges: list[EdgeSpec] = field(default_factory=list)


class ScenarioGraphBuilder:
    """按 ScenarioSpec 装配 StateGraph(OrchestrationState) 并 compile。"""

    def __init__(self, sup, checkpointer) -> None:
        self._sup = sup
        self._checkpointer = checkpointer

    def build(self, spec: ScenarioSpec):
        g = StateGraph(OrchestrationState)
        for n in spec.nodes:
            g.add_node(n.name, n.factory(self._sup))
        for e in spec.edges:
            if e.cond is not None:
                # 分流条件边
                pm = {k: _resolve(v) for k, v in (e.path_map or {}).items()}
                g.add_conditional_edges(_resolve(e.from_node), e.cond, pm)
            elif isinstance(e.to, list):
                # 并行派发（默认参数捕获，避免闭包陷阱）
                g.add_conditional_edges(_resolve(e.from_node), lambda s, t=e.to: t)
            else:
                # 普通边
                g.add_edge(_resolve(e.from_node), _resolve(e.to))
        return g.compile(checkpointer=self._checkpointer)


# ===========================================================================
# 4 个场景 spec（忠实迁移自原 build_*_graph，节点/边一一对应）
# ===========================================================================

CHANGEOVER = ScenarioSpec(
    name="CHANGEOVER",
    nodes=[
        NodeSpec("plan", lambda sup: sup.plan),
        NodeSpec("first_article", lambda sup: sup.qc.query_first_article),
        NodeSpec("gate_first_article", lambda sup: sup.gate("FIRST_ARTICLE")),
        NodeSpec("process_switch", lambda sup: sup.qc.query_active_route),
        NodeSpec("gate_process_switch", lambda sup: sup.gate("PROCESS_SWITCH")),
        NodeSpec("tooling_check", lambda sup: sup.qc.query_and_compare_tooling),
        NodeSpec("kitting_check", lambda sup: sup.qc.query_and_compare_kitting),
        NodeSpec("barrier", lambda sup: sup.barrier),
        NodeSpec("draft_release", lambda sup: sup.qc.draft_release_card),
        NodeSpec("gate_release", lambda sup: sup.gate("RELEASE")),
        NodeSpec("root_cause", lambda sup: sup.run_agent("root_cause")),
        NodeSpec("gate_disposition", lambda sup: sup.gate("DISPOSITION", capability="root_cause")),
        NodeSpec("done", lambda sup: sup.done),
    ],
    edges=[
        EdgeSpec(_START, "plan"),
        EdgeSpec("plan", "first_article"),
        EdgeSpec("first_article", "gate_first_article"),
        EdgeSpec("gate_first_article", "process_switch"),
        EdgeSpec("process_switch", "gate_process_switch"),
        # 并行派发
        EdgeSpec("gate_process_switch", ["tooling_check", "kitting_check"]),
        EdgeSpec("tooling_check", "barrier"),
        EdgeSpec("kitting_check", "barrier"),
        # barrier 确定性分流（suspend -> END）
        EdgeSpec("barrier", cond=barrier_route, path_map={
            "draft_release": "draft_release", "root_cause": "root_cause", "suspend": _END,
        }),
        EdgeSpec("draft_release", "gate_release"),
        EdgeSpec("gate_release", "done"),
        EdgeSpec("root_cause", "gate_disposition"),
        # gate_disposition retry 条件边
        EdgeSpec("gate_disposition",
                 cond=lambda s: "tooling_check" if s.get("retry_tooling") else "done",
                 path_map={"tooling_check": "tooling_check", "done": "done"}),
        EdgeSpec("done", _END),
    ],
)


FAULT_RESPONSE = ScenarioSpec(
    name="FAULT_RESPONSE",
    nodes=[
        NodeSpec("plan", lambda sup: sup.plan),
        NodeSpec("draft_repair_order", lambda sup: sup.qc.draft_repair_order),
        NodeSpec("fault_impact", lambda sup: sup.run_agent("fault_impact")),
        NodeSpec("gate_repair", lambda sup: sup.gate("REPAIR")),
        NodeSpec("gate_isolation", lambda sup: sup.gate("ISOLATION", capability="fault_impact")),
        NodeSpec("gate_recalibration", lambda sup: sup.gate("RECALIBRATION")),
        NodeSpec("gate_restart_first_article", lambda sup: sup.gate("RESTART_FIRST_ARTICLE")),
        NodeSpec("done", lambda sup: sup.done),
    ],
    edges=[
        EdgeSpec(_START, "plan"),
        EdgeSpec("plan", ["draft_repair_order", "fault_impact"]),
        EdgeSpec("draft_repair_order", "gate_repair"),
        EdgeSpec("fault_impact", "gate_isolation"),
        EdgeSpec("gate_repair", "gate_recalibration"),
        EdgeSpec("gate_isolation", "gate_recalibration"),
        EdgeSpec("gate_recalibration", "gate_restart_first_article"),
        EdgeSpec("gate_restart_first_article", "done"),
        EdgeSpec("done", _END),
    ],
)


COMPLAINT_8D = ScenarioSpec(
    name="COMPLAINT_8D",
    nodes=[
        NodeSpec("plan", lambda sup: sup.plan),
        NodeSpec("traceability", lambda sup: sup.run_agent("traceability")),
        NodeSpec("supplier_trace", lambda sup: sup.qc.query_supplier_batch_trace),
        NodeSpec("isolation_scope", lambda sup: sup.qc.determine_isolation_scope),
        NodeSpec("gate_isolation_8d", lambda sup: sup.gate("ISOLATION_8D")),
        NodeSpec("draft_8d", lambda sup: sup.run_agent("draft_8d")),
        NodeSpec("gate_8d_publish", lambda sup: sup.gate("8D_PUBLISH", capability="draft_8d")),
        NodeSpec("done", lambda sup: sup.done),
    ],
    edges=[
        EdgeSpec(_START, "plan"),
        EdgeSpec("plan", "traceability"),
        EdgeSpec("traceability", ["supplier_trace", "isolation_scope"]),
        EdgeSpec("supplier_trace", "gate_isolation_8d"),
        EdgeSpec("isolation_scope", "gate_isolation_8d"),
        EdgeSpec("gate_isolation_8d", "draft_8d"),
        EdgeSpec("draft_8d", "gate_8d_publish"),
        EdgeSpec("gate_8d_publish", "done"),
        EdgeSpec("done", _END),
    ],
)


SCENARIO_SPECS: dict[str, ScenarioSpec] = {
    "CHANGEOVER": CHANGEOVER,
    "FAULT_RESPONSE": FAULT_RESPONSE,
    "COMPLAINT_8D": COMPLAINT_8D,
}
