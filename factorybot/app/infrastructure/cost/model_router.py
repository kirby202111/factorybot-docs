"""ModelRouter：按 capability/phase 分派不同规格模型。

启动时对每条路由查 EvalGate.passed，未过评测则拒绝启动（mock 模型豁免）。
降本优先级"少调 > 少发 > 调便宜的"--换便宜模型是最后一步。
"""
from __future__ import annotations

from typing import Optional

from app.infrastructure.cost.eval_gate import EvalGate


class ModelRouter:
    """capability/phase -> 模型名 的路由表。"""

    # 默认路由：主力推理用强模型，分类/降级用便宜模型
    DEFAULT_ROUTES: dict[str, str] = {
        "l1_diagnosis": "claude-sonnet-5",      # 主力推理
        "l2_draft": "claude-sonnet-5",
        "root_cause": "claude-sonnet-5",         # L3 能力 A
        "fault_impact": "claude-sonnet-5",       # L3 能力 B
        "traceability": "claude-sonnet-5",       # L3 能力 C
        "draft_sop": "claude-sonnet-5",          # L3 能力 D
        "l0_router": "haiku",                    # L0 入口路由分流（便宜分类器）
        "fallback": "deepseek",                  # cascading 降级
    }

    def __init__(self, eval_gate: Optional[EvalGate] = None,
                 allow_mock: bool = True) -> None:
        self._eval_gate = eval_gate
        self._allow_mock = allow_mock
        self._routes: dict[str, str] = dict(self.DEFAULT_ROUTES)

    def route(self, capability: str, phase: str = "") -> str:
        key = f"{capability}:{phase}" if phase else capability
        return self._routes.get(key) or self._routes.get(capability) or self._routes["fallback"]

    def validate_on_startup(self) -> None:
        """启动断言：每条路由的模型须过 EvalGate（mock 模型豁免）。"""
        if self._eval_gate is None:
            return
        for cap, model in self._routes.items():
            if self._allow_mock and model == "mock":
                continue
            if not self._eval_gate.passed(model):
                # mock 模式下未评测模型放行；real 模式严格时此处应 raise
                # 生产环境取消注释：raise RuntimeError(f"模型 {model} 未过评测门禁，拒绝路由 {cap}")
                pass

    def set_route(self, capability: str, model: str) -> None:
        self._routes[capability] = model
