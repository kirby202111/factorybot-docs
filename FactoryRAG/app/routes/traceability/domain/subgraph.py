"""A 子图领域模型（5M1E 聚类）。

核心安全契约 INV-CX-02：``CheckpointRecord`` 节点带 ``route_version`` 属性；
``[:SNAPSHOT_OF_ROUTE {route_version}]`` 边物理锁定生产时版本。工艺升版只追加新
``RouteVersion`` 节点 + 老节点置 DEPRECATED，历史快照边永不改。
检索必须用快照边属性上的版本，**而非**当前 ``status=ACTIVATED`` 的 RouteVersion。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.routes.traceability.domain.seed import Seed


class FiveM1ECategory(str, Enum):
    """5M1E 维度。"""

    MAN = "Man"
    MACHINE = "Machine"
    MATERIAL = "Material"
    METHOD = "Method"
    MEASUREMENT = "Measurement"
    ENVIRONMENT = "Environment"


class TraceNode(BaseModel):
    """子图节点。每个节点带 ``source_event_id`` 供证据回溯。"""

    label: str                          # "CheckpointRecord" / "RouteVersion" / "InventoryBatch" ...
    bounded_context: str                # "在制品执行" / "工艺管理" ...
    node_id: str                        # 上下文唯一: "CheckpointRecord:{checkpoint_id}"
    props: dict[str, Any] = Field(default_factory=dict)
    source_event_id: str = ""


class TraceEdge(BaseModel):
    """子图边。``version`` 锁定 route_version/bom_version/rule_version。"""

    rel: str                            # "SNAPSHOT_OF_ROUTE" / "CONSUMED_BATCH" / "FOR_UNIT" ...
    from_id: str
    to_id: str
    version: str | None = None          # route_version/bom_version/rule_version；非版本边为 None


class FiveM1ECluster(BaseModel):
    """5M1E 维度聚类。"""

    man: list[TraceNode] = Field(default_factory=list)            # CheckpointRecord(scanned_by)/FirstArticle/ReworkTask
    machine: list[TraceNode] = Field(default_factory=list)       # Asset/RepairOrder/MaintenanceTask (MVP 空)
    material: list[TraceNode] = Field(default_factory=list)      # InventoryBatch/Supplier/SubstituteRule/Bom/Material
    method: list[TraceNode] = Field(default_factory=list)        # RouteVersion 快照/RouteStep/QualityGateRule
    measurement: list[TraceNode] = Field(default_factory=list)   # TestResult/QualityVerdict/DefectCatalog
    environment: list[TraceNode] = Field(default_factory=list)   # EquipmentChannel 语义采样 (MVP 空)

    def total_nodes(self) -> int:
        return sum(len(v) for v in (
            self.man, self.machine, self.material, self.method, self.measurement, self.environment
        ))


class TraceSubgraph(BaseModel):
    """GraphRetriever 返回的结构化子图。seed = 入口节点。"""

    seed: TraceNode
    clusters: FiveM1ECluster = Field(default_factory=FiveM1ECluster)
    edges: list[TraceEdge] = Field(default_factory=list)
    as_of: datetime
    projection_lag_ms: int = 0          # 事件 occurred_at -> 入图滞后

    @property
    def subgraph_ref(self) -> str:
        """``<seed_kind>:<seed_value>@<as_of_iso>``，L2 用此回查不重跑 Cypher。"""
        seed_kind = self.seed.props.get("seed_kind", self.seed.label)
        seed_value = self.seed.props.get("seed_value", self.seed.node_id)
        return f"{seed_kind}:{seed_value}@{self.as_of.isoformat()}"

    def route_version_locked(self) -> str | None:
        """从 Method 维度的 RouteVersion 快照节点取物理锁定的版本（而非当前 ACTIVE）。"""
        for n in self.clusters.method:
            rv = n.props.get("route_version")
            if rv:
                return str(rv)
        return None
