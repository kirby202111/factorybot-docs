"""EvalGate：评测门禁。比对准确率/ECE 阈值，不放行退化模型。

任何模型降级（换便宜模型）必须过 EvalGate.passed，否则拒绝启动/降级。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EvalResult:
    model: str
    accuracy: float
    ece: float              # Expected Calibration Error
    evidence_recall: float

    @property
    def passed(self) -> bool:
        return (
            self.accuracy >= EvalGate.ACCURACY_THRESHOLD
            and self.ece <= EvalGate.ECE_THRESHOLD
            and self.evidence_recall >= EvalGate.RECALL_THRESHOLD
        )


class EvalGate:
    """评测门禁阈值（MES 根因准确率影响返工/隔离决策，省钱不能牺牲准确率）。"""

    ACCURACY_THRESHOLD = 0.85
    ECE_THRESHOLD = 0.10
    RECALL_THRESHOLD = 0.80

    def __init__(self) -> None:
        self._results: dict[str, EvalResult] = {}

    def register(self, result: EvalResult) -> None:
        self._results[result.model] = result

    def passed(self, model: str) -> bool:
        r = self._results.get(model)
        if r is None:
            # 未评测过的模型默认不放行（生产严格）；mock 模式下由 ModelRouter 放行
            return False
        return r.passed
