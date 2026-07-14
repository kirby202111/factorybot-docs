"""SessionManager：编排 会话生命周期薄封装（mock 下复用 OrchestrationRepo）。

real 模式下可扩展为 Redis-backed 会话恢复（任意副本恢复任意会话上下文）。
"""
from __future__ import annotations

from app.infrastructure.persistence.repos import OrchestrationRepo, get_orchestration_repo


class SessionManager:
    def __init__(self, repo: OrchestrationRepo | None = None) -> None:
        self._repo = repo or get_orchestration_repo()

    async def create(self, session) -> object:
        await self._repo.create_session(session)
        return session

    async def get(self, session_id: str):
        return await self._repo.get_session(session_id)

    async def mark_failed(self, session_id: str, reason: str) -> None:
        await self._repo.mark_failed(session_id, reason)

    async def mark_done(self, session_id: str) -> None:
        await self._repo.mark_done(session_id)
