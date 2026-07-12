"""ActionCardDispatcher：双通道推送动作卡（WebSocket 实时 + Kafka 持久兜底）。

带 W3C traceparent。mock 模式下 WebSocketManager 记录推送（供测试/前端轮询），
Kafka 通道用 MockActionCardProducer（仅日志）。
"""
from __future__ import annotations

from typing import Any, Optional

from app.domain.l3_state import ActionCard
from app.infrastructure.kafka.producer import (
    ActionCardProducer, MockActionCardProducer, get_action_card_producer,
)
from app.infrastructure.obs.logging import get_logger

_log = get_logger("dispatcher")


class WebSocketManager:
    """进程内 WebSocket 推送桩：记录推送过的动作卡（供在线责任人/测试消费）。"""

    def __init__(self) -> None:
        self._inbox: dict[str, list[dict]] = {}   # user_id -> [card]

    async def send_to_user(self, user_id: str, payload: str) -> None:
        self._inbox.setdefault(user_id, []).append(payload)
        _log.info("action_card.ws.push", user_id=user_id)

    def pop(self, user_id: str) -> list[dict]:
        return self._inbox.pop(user_id, [])

    def latest(self, user_id: str) -> Optional[dict]:
        cards = self._inbox.get(user_id, [])
        return cards[-1] if cards else None


class ActionCardDispatcher:
    def __init__(self, ws: Optional[WebSocketManager] = None,
                 kafka: Optional[ActionCardProducer | MockActionCardProducer] = None,
                 assignee_user_id: str = "u_zhang") -> None:
        self._ws = ws or WebSocketManager()
        self._kafka = kafka or get_action_card_producer()
        self._assignee = assignee_user_id

    async def _resolve_assignee(self, card: ActionCard) -> str:
        return self._assignee

    async def push(self, card: ActionCard) -> None:
        assignee = await self._resolve_assignee(card)
        payload = card.model_dump_json()
        # 通道 1：WebSocket 实时
        await self._ws.send_to_user(assignee, payload)
        # 通道 2：Kafka 持久兜底（离线仍送达 + 可审计）
        await self._kafka.send(
            topic="agent.action_cards", key=card.session_id, value=payload,
            headers={
                "card_id": card.card_id, "assignee": assignee,
                "deadline": str(card.deadline) if card.deadline else "",
                "traceparent": _current_traceparent(),
            },
        )

    async def push_exception_card(self, session_id: str, step: str, reason: str) -> None:
        from datetime import datetime
        import uuid
        card = ActionCard(
            card_id=str(uuid.uuid4()), session_id=session_id, step=step,
            intent=f"异常挂起: {reason}", writes_via="none.application.none",
            risk_note=reason, deadline=None,
        )
        await self.push(card)

    @property
    def ws(self) -> WebSocketManager:
        return self._ws


def _current_traceparent() -> str:
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"
    except Exception:
        pass
    return "00-00000000000000000000000000000000-0000000000000000-00"
