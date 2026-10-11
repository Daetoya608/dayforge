"""Сессии агентов: токены MCP, идентификатор сессии CLI, исход (docs/mcp-tools.md)."""

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import AgentRole, SessionOutcome, SessionStatus
from dayforge.db.base import utcnow
from dayforge.db.models import AgentSession


class SessionError(ValueError):
    pass


@dataclass(frozen=True)
class SessionInfo:
    """То, что MCP-сервер знает о вызывающем агенте после проверки токена."""

    id: int
    role: AgentRole
    project_id: int | None
    task_id: int | None
    run_id: int | None
    outcome: SessionOutcome | None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], logs_dir: Path) -> None:
        self._session_factory = session_factory
        self._logs_dir = logs_dir

    async def create(
        self,
        role: AgentRole,
        *,
        agent: dict[str, Any],
        project_id: int | None = None,
        task_id: int | None = None,
        run_id: int | None = None,
    ) -> tuple[AgentSession, str]:
        token = secrets.token_urlsafe(32)
        async with self._session_factory() as session, session.begin():
            row = AgentSession(
                role=role,
                project_id=project_id,
                task_id=task_id,
                run_id=run_id,
                agent=agent["agent"],
                model=agent["model"],
                effort=agent.get("effort"),
                mcp_token_hash=hash_token(token),
                log_path="",
            )
            session.add(row)
            await session.flush()
            row.log_path = str(self._logs_dir / f"{row.id}.jsonl")
        return row, token

    async def reactivate(self, session_id: int) -> str:
        """Продолжение сессии новым процессом CLI: новый токен, исход сброшен."""
        token = secrets.token_urlsafe(32)
        async with self._session_factory() as session, session.begin():
            row = await _get(session, session_id)
            row.mcp_token_hash = hash_token(token)
            row.status = SessionStatus.RUNNING
            row.outcome = None
            row.ended_at = None
        return token

    async def authenticate(self, token: str) -> SessionInfo | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(AgentSession).where(AgentSession.mcp_token_hash == hash_token(token))
            )
        if row is None or row.status is not SessionStatus.RUNNING:
            return None
        return _info(row)

    async def info(self, session_id: int) -> SessionInfo:
        async with self._session_factory() as session:
            return _info(await _get(session, session_id))

    async def get(self, session_id: int) -> AgentSession:
        async with self._session_factory() as session:
            return await _get(session, session_id)

    async def record_outcome(self, session_id: int, outcome: SessionOutcome) -> None:
        """Завершающий вызов MCP. Второй завершающий вызов в той же сессии — ошибка."""
        async with self._session_factory() as session, session.begin():
            row = await _get(session, session_id)
            if row.outcome is not None:
                raise SessionError(f"сессия уже завершается ({row.outcome})")
            row.outcome = outcome

    async def set_cli_session_id(self, session_id: int, cli_session_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            (await _get(session, session_id)).cli_session_id = cli_session_id

    async def touch(self, session_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            (await _get(session, session_id)).last_event_at = utcnow()

    async def end(
        self,
        session_id: int,
        status: SessionStatus,
        *,
        outcome: SessionOutcome | None = None,
        usage: dict[str, Any] | None = None,
    ) -> AgentSession:
        async with self._session_factory() as session, session.begin():
            row = await _get(session, session_id)
            row.status = status
            if outcome is not None:
                row.outcome = outcome
            if usage:
                row.usage = row.usage | usage
            row.ended_at = utcnow()
            return row

    async def for_run(self, run_id: int) -> list[AgentSession]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(AgentSession)
                    .where(AgentSession.run_id == run_id)
                    .order_by(AgentSession.id)
                )
            )

    async def latest_for_task(self, task_id: int) -> AgentSession | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(AgentSession)
                .where(AgentSession.task_id == task_id)
                .order_by(AgentSession.id.desc())
                .limit(1)
            )

    async def running(self) -> list[AgentSession]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(AgentSession).where(AgentSession.status == SessionStatus.RUNNING)
                )
            )


def _info(row: AgentSession) -> SessionInfo:
    return SessionInfo(
        id=row.id,
        role=row.role,
        project_id=row.project_id,
        task_id=row.task_id,
        run_id=row.run_id,
        outcome=row.outcome,
    )


async def _get(session: AsyncSession, session_id: int) -> AgentSession:
    row = await session.get(AgentSession, session_id)
    if row is None:
        raise SessionError(f"сессии {session_id} нет")
    return row
