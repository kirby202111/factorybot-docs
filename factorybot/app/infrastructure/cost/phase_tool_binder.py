"""PhaseToolBinder：按推理阶段动态裁剪工具集（默认关闭，工具集极大时启用）。"""
from __future__ import annotations

from typing import Optional

from app.domain.tenant import TenantContext
from app.domain.tool import ToolRegistry


class PhaseToolBinder:
    """阶段 -> 该阶段可见工具名。默认关闭（返回全量）。"""

    PHASE_TOOLS: dict[str, list[str]] = {
        # 示例：诊断初期只看图 + 过点；取证期开放物料/设备
        "init": ["query_traceability_graph", "query_pass_records"],
        "evidence": [
            "query_test_results", "query_material_batch", "query_bom_version",
            "query_device_params", "query_asset_status", "query_defect_rate",
        ],
    }

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled

    def bind(self, registry: ToolRegistry, capability: str, tenant: TenantContext,
             phase: Optional[str] = None) -> list:
        all_tools = registry.tools_for(capability, tenant)
        if not self._enabled or not phase:
            return all_tools
        allowed = set(self.PHASE_TOOLS.get(phase, []))
        return [t for t in all_tools if t.name in allowed]
