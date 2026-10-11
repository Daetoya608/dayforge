"""Хранилище бота в SQLite: FSM-состояния диалогов и служебные значения переживают рестарт."""

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.db.models import BotValue


class BotKV:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._session_factory() as session:
            row = await session.get(BotValue, key)
        return row.value if row is not None else default

    async def set(self, key: str, value: Any) -> None:
        async with self._session_factory() as session, session.begin():
            await session.merge(BotValue(key=key, value=value))

    async def delete(self, key: str) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(BotValue, key)
            if row is not None:
                await session.delete(row)


class SqliteStorage(BaseStorage):
    """FSM-хранилище aiogram поверх BotKV."""

    def __init__(self, kv: BotKV) -> None:
        self._kv = kv

    @staticmethod
    def _key(key: StorageKey, part: str) -> str:
        scope = f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{key.destiny}"
        return f"fsm:{scope}:{part}"

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        if value is None:
            await self._kv.delete(self._key(key, "state"))
        else:
            await self._kv.set(self._key(key, "state"), value)

    async def get_state(self, key: StorageKey) -> str | None:
        value = await self._kv.get(self._key(key, "state"))
        return str(value) if value is not None else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if data:
            await self._kv.set(self._key(key, "data"), dict(data))
        else:
            await self._kv.delete(self._key(key, "data"))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(await self._kv.get(self._key(key, "data"), {}))

    async def close(self) -> None:
        return None
