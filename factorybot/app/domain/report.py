"""L1 诊断报告：5M1E 根因假设排序 + 证据链 + 置信度兜底。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class FiveM1ECategory(str, Enum):
    """5M1E 根因维度。模型只能从枚举取，不得自造类别。"""

    MAN = "Man"                  # 人
    MACHINE = "Machine"          # 机
    MATERIAL = "Material"        # 料
    METHOD = "Method"            # 法
    MEASUREMENT = "Measurement"  # 测
    ENVIRONMENT = "Environment"  # 环


class Hypothesis(BaseModel):
    """一条根因假设。evidence 必须非空（引用工具返回的 trace_id）。"""

    category: FiveM1ECategory
    rank: int = 1                       # 1 = 最可能
    statement: str
    evidence: list[str] = Field(default_factory=list)   # ["trace_id=T-101", ...]
    suggested_action: str = ""

    @model_validator(mode="after")
    def _evidence_non_empty(self) -> "Hypothesis":
        if not self.evidence:
            raise ValueError("每条假设必须引用至少一条证据 (trace_id)")
        return self


class DiagnosisReport(BaseModel):
    """L1 诊断产出。透传 subgraph_ref + route_version 给 L2。"""

    summary: str
    confidence: float                       # 0.0 ~ 1.0
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    subgraph_ref: str = ""                  # 指向 RAG 追溯子图，L2 据此回查
    route_version: str | None = None        # 版本一致性三段链的第二段
    evidence_refs: list[str] = Field(default_factory=list)
    disclaimer: str = "本报告为辅助诊断假设，最终处置需工程师确认"
    needs_human_review: bool = False        # confidence < 阈值 或 证据不足

    @model_validator(mode="after")
    def _at_least_one_hypothesis_if_confident(self) -> "DiagnosisReport":
        # 转人工报告允许空假设；否则至少一条
        if not self.needs_human_review and not self.hypotheses:
            raise ValueError("非转人工报告必须至少一条假设")
        return self

    @classmethod
    def partial(cls, reason: str, subgraph_ref: str = "") -> "DiagnosisReport":
        """转人工：诊断未完成（超时/步数超限/解析失败）。"""
        return cls(
            summary=f"诊断未完成，已转人工: {reason}",
            confidence=0.0,
            hypotheses=[],
            subgraph_ref=subgraph_ref,
            needs_human_review=True,
        )
