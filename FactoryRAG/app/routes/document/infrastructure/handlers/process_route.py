"""工艺路线事件 handler（决策 #3 联动 PUBLISHED）。

- ``ProcessRouteActivated``：直接把关联文档版本置 PUBLISHED（无 SUBMITTED），
  同 doc 同类绑定的旧 PUBLISHED -> DEPRECATED。chunk 不可变：不更新 ChromaDB chunk metadata。
- ``ProcessRouteDeprecated``：关联文档版本 -> DEPRECATED，不删 chunk，靠版本锚点查询过滤隔离。
"""
from __future__ import annotations

import logging
from typing import Any

from app.routes.document.domain.document import VersionState
from app.shared.kafka.domain_event import DomainEvent

logger = logging.getLogger(__name__)


class _RouteHandlerBase:
    """工艺路线 handler 公共依赖。"""

    event_type: str = ""

    def __init__(self, **deps: Any) -> None:
        self._doc_repo = deps["doc_repo"]
        self._chunk_repo = deps["chunk_repo"]
        self._object_store = deps["object_store"]
        self._parser = deps["parser"]
        self._chunk_selector = deps["chunk_selector"]
        self._embedder = deps["embedder"]
        self._obs = deps["obs"]

    @staticmethod
    def _route(event: DomainEvent) -> tuple[str, str]:
        payload = event.payload or {}
        return payload.get("route_id", ""), payload.get("route_version", "")


class ProcessRouteActivatedHandler(_RouteHandlerBase):
    """决策 #3：工艺生效即文档生效，联动 PUBLISHED。"""

    event_type = "ProcessRouteActivated"

    async def handle(self, event: DomainEvent, session: Any) -> None:
        route_id, route_version = self._route(event)
        if not route_id or not route_version:
            logger.warning("ProcessRouteActivated 缺 route_id/route_version，跳过")
            return
        versions = await self._doc_repo.find_drafts_by_route(route_id, route_version)
        for v in versions:
            # 联动 PUBLISHED（无 SUBMITTED/PENDING_REBIND），责任归工艺 owner
            await self._doc_repo.update_state(
                session, v.version_id, VersionState.PUBLISHED, effective=True
            )
            # 同 doc 同类绑定的旧 PUBLISHED -> DEPRECATED
            await self._doc_repo.deprecate_old_published(session, v.document_id, v.version_id)
            logger.info(
                "联动 PUBLISHED: doc=%s version=%s route=%s@%s（决策 #3）",
                v.document_id, v.version_id, route_id, route_version,
            )
            # chunk 不可变：不更新 ChromaDB 中老 chunk；新版本会在后续摄入时追加新 chunk


class ProcessRouteDeprecatedHandler(_RouteHandlerBase):
    """工艺废弃：关联文档版本 -> DEPRECATED（chunk 不删，靠版本锚点隔离）。"""

    event_type = "ProcessRouteDeprecated"

    async def handle(self, event: DomainEvent, session: Any) -> None:
        route_id, route_version = self._route(event)
        if not route_id or not route_version:
            return
        versions = await self._doc_repo.find_published_by_route(route_id, route_version)
        for v in versions:
            await self._doc_repo.update_state(
                session, v.version_id, VersionState.DEPRECATED, deprecate=True
            )
            logger.info("联动 DEPRECATED: doc=%s version=%s route=%s@%s",
                        v.document_id, v.version_id, route_id, route_version)
