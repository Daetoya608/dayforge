"""Действия системного агента, которые ждут подтверждения пользователя (docs/telegram-bot.md)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import PendingActionStatus
from dayforge.core.events import Notifier, PendingActionCreated
from dayforge.db.base import utcnow
from dayforge.db.models import PendingAction

# Исполнитель подтверждённого действия: получает payload, возвращает текст результата.
ActionHandler = Callable[[dict[str, Any]], Awaitable[str]]


class PendingActionError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    action_id: int
    status: PendingActionStatus
    result: str | None


class PendingActionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        *,
        ttl: timedelta,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._ttl = ttl
        self._handlers: dict[str, ActionHandler] = {}

    def register(self, kind: str, handler: ActionHandler) -> None:
        self._handlers[kind] = handler

    async def create(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        description: str,
        session_id: int | None = None,
    ) -> int:
        if kind not in self._handlers:
            raise PendingActionError(f"неизвестное действие {kind}")
        async with self._session_factory() as session, session.begin():
            action = PendingAction(
                kind=kind,
                payload=payload | {"description": description},
                session_id=session_id,
                expires_at=utcnow() + self._ttl,
            )
            session.add(action)
            await session.flush()
        await self._notifier.send(
            PendingActionCreated(action_id=action.id, description=description)
        )
        return action.id

    async def set_message_id(self, action_id: int, message_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            action = await _get(session, action_id)
            action.tg_message_id = message_id

    async def decide(self, action_id: int, *, confirm: bool) -> Decision:
        async with self._session_factory() as session, session.begin():
            action = await _get(session, action_id)
            if action.status is not PendingActionStatus.PENDING:
                raise PendingActionError(f"действие уже {action.status}")
            if action.expires_at <= utcnow():
                action.status = PendingActionStatus.EXPIRED
                return Decision(action_id, PendingActionStatus.EXPIRED, None)
            action.status = (
                PendingActionStatus.CONFIRMED if confirm else PendingActionStatus.REJECTED
            )
            action.decided_at = utcnow()
            kind, payload = action.kind, dict(action.payload)

        if not confirm:
            return Decision(action_id, PendingActionStatus.REJECTED, None)
        result = await self._handlers[kind](payload)
        return Decision(action_id, PendingActionStatus.CONFIRMED, result)

    async def expire_due(self, now: datetime | None = None) -> list[PendingAction]:
        now = now or utcnow()
        async with self._session_factory() as session, session.begin():
            due = list(
                await session.scalars(
                    select(PendingAction).where(
                        PendingAction.status == PendingActionStatus.PENDING,
                        PendingAction.expires_at <= now,
                    )
                )
            )
            for action in due:
                action.status = PendingActionStatus.EXPIRED
                action.decided_at = now
        return due


async def _get(session: AsyncSession, action_id: int) -> PendingAction:
    action = await session.get(PendingAction, action_id)
    if action is None:
        raise PendingActionError(f"действия {action_id} нет")
    return action
