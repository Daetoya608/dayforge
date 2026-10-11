"""Глобальные настройки, которые пользователь меняет через /settings.

Список и значения по умолчанию — docs/data-model.md#settings.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.db.models import Setting

DEFAULTS: dict[str, Any] = {
    "timezone": "Europe/Moscow",
    "run_time": "03:00",
    "backup_time": "12:00",
    "default_planner_agent": {"agent": "claude_code", "model": "opus", "effort": "high"},
    "default_executor_agent": {"agent": "claude_code", "model": "opus", "effort": "medium"},
    "system_agent": {"agent": "claude_code", "model": "sonnet", "effort": "medium"},
    "watchdog_idle_minutes": 45,
    "test_timeout_minutes": 30,
    "agent_chat_idle_hours": 6,
    "limit_auto_resume": False,
    "pending_action_ttl_hours": 24,
    "question_reminder_hours": 24,
}


class SettingsService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> Any:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting: {key}")
        async with self._session_factory() as session:
            row = await session.get(Setting, key)
        return row.value if row is not None else DEFAULTS[key]

    async def get_all(self) -> dict[str, Any]:
        async with self._session_factory() as session:
            rows = await session.scalars(select(Setting))
            stored = {row.key: row.value for row in rows}
        return DEFAULTS | {k: v for k, v in stored.items() if k in DEFAULTS}

    async def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting: {key}")
        async with self._session_factory() as session, session.begin():
            await session.merge(Setting(key=key, value=value))
