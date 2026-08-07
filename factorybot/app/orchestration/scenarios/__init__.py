"""编排 场景图装配：声明式 spec + ScenarioGraphBuilder。

3 个场景（changeover/fault_response/complaint_8d）由 SCENARIO_SPECS 声明，
ScenarioGraphBuilder 统一装配，取代原先各自手写的 build_*_graph。
"""
from app.orchestration.scenarios.specs import SCENARIO_SPECS, ScenarioGraphBuilder, ScenarioSpec

__all__ = ["SCENARIO_SPECS", "ScenarioGraphBuilder", "ScenarioSpec"]
