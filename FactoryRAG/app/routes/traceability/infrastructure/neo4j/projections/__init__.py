"""A 图投影 handler（每上下文一个，MERGE 幂等，禁 DELETE/REMOVE）。"""
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
from app.routes.traceability.infrastructure.neo4j.projections.registry import (
    TRACE_TOPICS,
    build_projection_registry,
    get_projection_handler_classes,
)

__all__ = [
    "CheckpointProjectionHandler",
    "ProcessRouteProjectionHandler",
    "MaterialProjectionHandler",
    "QualityProjectionHandler",
    "build_projection_registry",
    "get_projection_handler_classes",
    "TRACE_TOPICS",
]
