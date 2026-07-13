"""A 投影 handler 注册表。

构造 ``GraphProjector``（持有 handler 字典 + 订阅主题），供 ConsumerGroup 路由与
``ReadOnlyProjectionGate``/``RawDataTopicGate`` 启动期扫描。
MVP 4 上下文：在制品执行 / 工艺管理 / 物料 / 质量。
"""
from __future__ import annotations

from typing import Any

from app.routes.traceability.domain.projection import GraphProjector
from app.routes.traceability.infrastructure.neo4j.projections.checkpoint import (
    CheckpointProjectionHandler,
)
from app.routes.traceability.infrastructure.neo4j.projections.material import (
    MaterialProjectionHandler,
)
from app.routes.traceability.infrastructure.neo4j.projections.process_route import (
    ProcessRouteProjectionHandler,
)
from app.routes.traceability.infrastructure.neo4j.projections.quality import (
    QualityProjectionHandler,
)

# A 订阅主题（MVP 4 上下文；无 dc.* 原始数据流，RawDataTopicGate 通过）。
TRACE_TOPICS = [
    "mes.checkpoint.lifecycle",
    "mes.testresult.structured",
    "mes.routing.progress",
    "process.route.lifecycle",
    "material.bom.lifecycle",
    "material.inventory.changed",
    "material.substitute.lifecycle",
    "quality.inspection.verdict",
    "quality.gate.lifecycle",
    "quality.defect.catalog",
]

_HANDLER_CLASSES = [
    CheckpointProjectionHandler,
    ProcessRouteProjectionHandler,
    MaterialProjectionHandler,
    QualityProjectionHandler,
]


def get_projection_handler_classes() -> list[type]:
    """供 ``ReadOnlyProjectionGate`` 静态扫描（无需实例化）。"""
    return list(_HANDLER_CLASSES)


def build_projection_registry(
    *, driver: Any, embedder: Any, trace_svc: Any
) -> GraphProjector:
    """构造 A 图投影协调器（GraphProjector）。"""
    handlers_instances = [
        CheckpointProjectionHandler(driver=driver, embedder=embedder, trace_svc=trace_svc),
        ProcessRouteProjectionHandler(driver=driver, embedder=embedder, trace_svc=trace_svc),
        MaterialProjectionHandler(driver=driver, embedder=embedder, trace_svc=trace_svc),
        QualityProjectionHandler(driver=driver, embedder=embedder, trace_svc=trace_svc),
    ]
    handlers: dict[str, Any] = {}
    for h in handlers_instances:
        for et in h.event_types:
            handlers[et] = h
    return GraphProjector(handlers=handlers, topics=TRACE_TOPICS)
