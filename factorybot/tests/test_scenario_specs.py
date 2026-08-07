"""scenario spec 装配回归：3 个 spec 都能 build+compile，拓扑完整。

保护 scenario 去重（ScenarioGraphBuilder + 声明式 spec）不退化。
"""
from __future__ import annotations

import pytest

from app.container import get_container
from app.orchestration.scenarios import SCENARIO_SPECS, ScenarioGraphBuilder


def test_scenario_specs_cover_all_three():
    assert set(SCENARIO_SPECS.keys()) == {
        "CHANGEOVER", "FAULT_RESPONSE", "COMPLAINT_8D",
    }


def test_all_scenario_specs_build_without_error():
    """3 个 spec 各自 build()+compile() 不抛（装配正确性）。"""
    c = get_container()
    builder = ScenarioGraphBuilder(c.supervisor, c.checkpointer)
    for name, spec in SCENARIO_SPECS.items():
        graph = builder.build(spec)
        assert graph is not None, f"{name} build 返回 None"


def test_container_builds_all_three_graphs():
    """container 装配产出 3 个 graph（隐式验证 spec 装配不抛）。"""
    c = get_container()
    assert set(c.orchestration_service._graphs.keys()) == {
        "CHANGEOVER", "FAULT_RESPONSE", "COMPLAINT_8D",
    }


def test_changeover_spec_topology_complete():
    """changeover spec 节点 13 / 边 14（防迁移漏节点/边）。"""
    spec = SCENARIO_SPECS["CHANGEOVER"]
    assert len(spec.nodes) == 13
    assert len(spec.edges) == 14
    node_names = {n.name for n in spec.nodes}
    assert {"plan", "barrier", "root_cause", "gate_disposition", "done"} <= node_names
    # 两条分流条件边：barrier_route + gate_disposition retry
    cond_edges = [e for e in spec.edges if e.cond is not None]
    assert len(cond_edges) == 2
