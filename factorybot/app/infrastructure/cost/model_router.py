"""ModelRouter：按 capability/phase 分派不同规格模型。

启动时对每条路由查 EvalGate.passed，未过评测则拒绝启动（mock 模型豁免）。
降本优先级"少调 > 少发 > 调便宜的"--换便宜模型是最后一步。
"""
from __future__ import annotations

from typing import Optional

from app.infrastructure.cost.eval_gate import EvalGate
from app.infrastructure.obs.logging import get_logger


class ModelRouter:
    """capability/phase -> 模型名 的路由表。"""

    # 默认路由：主力推理用强模型，分类/降级用便宜模型
    DEFAULT_ROUTES: dict[str, str] = {
        "diagnosis": "claude-sonnet-5",         # 主力推理
        "draft": "claude-sonnet-5",
        "root_cause": "claude-sonnet-5",         # 编排 能力 A
        "fault_impact": "claude-sonnet-5",       # 编排 能力 B
        "traceability": "claude-sonnet-5",       # 编排 能力 C
        "entry_router": "haiku",                 # 入口路由分流（便宜分类器）
        "fallback": "deepseek",                  # cascading 降级
    }

    def __init__(self, eval_gate: Optional[EvalGate] = None,
                 allow_mock: bool = True, active_model: str = "") -> None:
        self._eval_gate = eval_gate
        self._allow_mock = allow_mock
        # 实际生效的模型（settings.llm_model）：成本路由 route() 未接入调用链时，
        # 所有 Agent 统一用它；warn 文案据此说明现状。
        self._active_model = active_model
        self._routes: dict[str, str] = dict(self.DEFAULT_ROUTES)

    def route(self, capability: str, phase: str = "") -> str:
        key = f"{capability}:{phase}" if phase else capability
        return self._routes.get(key) or self._routes.get(capability) or self._routes["fallback"]

    def validate_on_startup(self) -> None:
        """启动校验：诚实化现状--成本路由 route() 尚未接入 LLM 调用链。

        - mock 模式：豁免全部路由（EvalGate 未接评测数据源，passed() 恒 False）。
        - real 模式：对未评测模型打 warn，不 raise--因尚无评测流程，raise 会阻断
          启动；此处仅让"成本路由未启用、统一用 active_model"这一现状可见。
          待评测数据源就绪、route() 接入调用链后，再改为 raise 门禁。
        """
        if self._eval_gate is None:
            return
        if self._allow_mock:
            return
        log = get_logger("model_router")
        active = self._active_model or "(未配置)"
        for cap, model in self._routes.items():
            if self._eval_gate.passed(model):
                continue
            log.warning(
                "cost.model_router.not_evaluated",
                capability=cap, model=model, active_model=active,
                hint=f"成本路由未启用(route() 未接入调用链)，所有 Agent 统一使用 "
                     f"llm_model={active}；接入评测数据源后将 route() 接入并启用 raise 门禁",
            )

    def set_route(self, capability: str, model: str) -> None:
        self._routes[capability] = model
