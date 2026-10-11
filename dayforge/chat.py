"""Чат с системным агентом (/agent и свободный текст в боте) — docs/agents.md#системный-агент."""

import asyncio
import time
from pathlib import Path

from dayforge import prompts
from dayforge.agents.launcher import AgentLauncher
from dayforge.core.enums import AgentRole, SessionOutcome
from dayforge.services import Services

FALLBACK_REPLIES = {
    SessionOutcome.LIMIT: "Упёрся в лимит подписки — попробуйте позже.",
    SessionOutcome.WATCHDOG: "Агент долго не отвечал, сессия прервана. Попробуйте ещё раз.",
    SessionOutcome.ERROR: "Агент завершился с ошибкой. Подробности — в логах системы.",
}


class ChatAgent:
    def __init__(self, services: Services, launcher: AgentLauncher, *, projects_dir: Path) -> None:
        self._s = services
        self._launcher = launcher
        self._projects_dir = projects_dir
        self._lock = asyncio.Lock()
        self._session_id: int | None = None
        self._last_message = 0.0

    def reset(self) -> None:
        self._session_id = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def ask(self, text: str) -> str:
        """Одно сообщение пользователя → ответ агента. Сообщения идут строго по очереди."""
        async with self._lock:
            idle_limit = 3600.0 * await self._s.settings.get("agent_chat_idle_hours")
            if self._session_id is not None and time.monotonic() - self._last_message > idle_limit:
                self._session_id = None
            self._last_message = time.monotonic()

            today = await self._s.calendar.today()
            result = await self._launcher.launch(
                role=AgentRole.SYSTEM,
                agent=await self._s.settings.get("system_agent"),
                cwd=self._projects_dir,
                prompt=text,
                system_prompt=prompts.render("system_agent_system.md", today=f"{today:%d.%m.%Y}"),
                resume_session_id=self._session_id,
                read_only=True,
            )
            if result.result.cli_session_id:
                self._session_id = result.session_id
            if result.result.final_text:
                return result.result.final_text
            return FALLBACK_REPLIES.get(result.outcome, "Агент не ответил.")  # type: ignore[arg-type]
