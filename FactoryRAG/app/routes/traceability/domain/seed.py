"""A seed + 请求 DTO。

Seed：图检索入口（WipUnit/WorkOrder/InventoryBatch/Defect/Asset）。
版本一致性：``TraceQuery.route_version`` 可选锁定具体版本做历史回溯；
不传则从图的 ``SNAPSHOT_OF_ROUTE{route_version}`` 快照边属性取生产时锁定版本。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class SeedKind(str, Enum):
    """图 seed 类型。"""

    WIP_UNIT = "WipUnit"               # 单件（SN）
    WORK_ORDER = "WorkOrder"
    INVENTORY_BATCH = "InventoryBatch"
    DEFECT = "Defect"
    ASSET = "Asset"                    # phase 2


class Seed(BaseModel):
    """图 seed（值对象）。"""

    kind: SeedKind
    value: str                          # "SN-001" / "B-77" / "WO-001" / "SW-001"


class TraceQuery(BaseModel):
    """``POST /rag/trace/query`` 请求（检索 + LLM 综合）。"""

    question: str
    seed: Seed | None = None            # 显式 seed，覆盖 NL 解析
    as_of: datetime | None = None       # 时间窗截止；默认 now()
    route_version: str | None = None    # 可选：锁定具体版本做历史回溯


class ExpandRequest(BaseModel):
    """``POST /rag/trace/expand`` 请求（只取子图不综合）。"""

    kind: SeedKind
    value: str
    as_of: datetime | None = None
    route_version: str | None = None
