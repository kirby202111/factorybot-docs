"""GateManager：confirmation gate。interrupt(value=card) 暂停 -> 人确认 -> resume。

核心机制：interrupt 保存 checkpoint（MemorySaver/SqlSaver）-> 抛 GraphInterrupt（控制流
信号，非错误）-> ainvoke 等待；Command(resume=token) 加载 checkpoint 续跑。
动作卡推送由 orchestrator 在检测到 interrupt 后负责（避免 resume 时重复推送）。
"""
from __future__ import annotations

from langgraph.types import interrupt

from app.domain.orchestration_state import ActionCard
from app.infrastructure.persistence.repos import OrchestrationRepo


class GateManager:
    def __init__(self, repo: OrchestrationRepo) -> None:
        self._repo = repo

    async def await_confirmation(
        self, session_id: str, step: str, card: ActionCard,
    ) -> str:
        """在 gate 处暂停，等人确认。返回 PASS | REJECT。

        interrupt(value=card) 内部：保存 checkpoint -> 抛 GraphInterrupt -> ainvoke 等待；
        Command(resume=token) 到达时，interrupt 返回 token，本方法继续执行。
        """
        confirmation = interrupt(value=card)
        # checkpointer 可能序列化 resume value，这里兼容 dict 形态
        if isinstance(confirmation, dict):
            from app.infrastructure.redis_.confirmation_store import ConfirmationToken
            try:
                confirmation = ConfirmationToken(
                    id=confirmation["id"], session_id=confirmation["session_id"],
                    step=confirmation["step"], action=confirmation["action"],
                    approved=confirmation["approved"], user_id=confirmation["user_id"],
                    issued_at=confirmation["issued_at"],
                )
            except (KeyError, TypeError):
                confirmation = None
        expected = card.writes_via_action()
        if confirmation is None:
            decision = "REJECT"
        elif not confirmation.valid_for(expected):
            decision = "REJECT"
        else:
            decision = "PASS" if confirmation.approved else "REJECT"
        await self._repo.record_gate(session_id, step, decision)
        return decision
