"""质量门规则事件 handler（决策 #2 双轨，评测后切换）。

MVP 阶段检验标准仍按 ``route_version`` 归属；``DocumentBinding`` 已预留
``rule_id``+``rule_version`` 字段并订阅 ``quality.gate.lifecycle``，评测后回填切换。
"""
from __future__ import annotations

import logging
from typing import Any

from app.routes.document.infrastructure.handlers.process_route import _RouteHandlerBase
from app.shared.kafka.domain_event import DomainEvent

logger = logging.getLogger(__name__)


class QualityGateRuleActivatedHandler(_RouteHandlerBase):
    """``QualityGateRuleActivated`` -> 按 rule_id+rule_version 绑定检验标准（预留）。"""

    event_type = "QualityGateRuleActivated"

    async def handle(self, event: DomainEvent, session: Any) -> None:
        payload = event.payload or {}
        rule_id = payload.get("rule_id")
        rule_version = payload.get("rule_version")
        if not rule_id or not rule_version:
            return
        # MVP：noop（检验标准仍按 route_version 归属）。
        # 评测后切换：按 rule_id+rule_version 找关联文档版本，联动 PUBLISHED（同决策 #3 模式）。
        logger.info(
            "QualityGateRuleActivated 预留：rule=%s@%s（决策 #2，评测后切换）",
            rule_id, rule_version,
        )
