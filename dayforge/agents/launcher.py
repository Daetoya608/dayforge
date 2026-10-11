"""Запуск сессии агента: сессия и токен MCP, поток событий, watchdog, исход (docs/agents.md)."""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dayforge.agents.base import AgentAdapter, SessionRequest, SessionResult
from dayforge.core.enums import AgentRole, SessionOutcome, SessionStatus
from dayforge.core.sessions import SessionService
from dayforge.core.settings import SettingsService

log = logging.getLogger(__name__)

TOUCH_INTERVAL_SECONDS = 30
WATCHDOG_CHECK_SECONDS = 5.0
PAUSING_OUTCOMES = (SessionOutcome.QUESTION, SessionOutcome.SECRET, SessionOutcome.PLAN_SUBMITTED)


@dataclass(frozen=True)
class LaunchResult:
    session_id: int
    result: SessionResult
    # Исход по завершающему вызову MCP либо по поведению процесса (лимит, watchdog, ошибка).
    outcome: SessionOutcome | None
    watchdog_fired: bool


class AgentLauncher:
    def __init__(
        self,
        sessions: SessionService,
        settings: SettingsService,
        adapters: dict[str, AgentAdapter],
        *,
        mcp_base_url: str,
        watchdog_check_seconds: float = WATCHDOG_CHECK_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._adapters = adapters
        self._mcp_base_url = mcp_base_url.rstrip("/")
        self._check_seconds = watchdog_check_seconds

    async def launch(
        self,
        *,
        role: AgentRole,
        agent: dict[str, Any],
        cwd: Path,
        prompt: str,
        system_prompt: str,
        project_id: int | None = None,
        task_id: int | None = None,
        run_id: int | None = None,
        resume_session_id: int | None = None,
        read_only: bool = False,
        mask_values: tuple[str, ...] = (),
    ) -> LaunchResult:
        """Новая сессия или продолжение существующей (resume_session_id)."""
        adapter = self._adapters.get(agent["agent"])
        if adapter is None:
            raise ValueError(f"агент {agent['agent']} не поддерживается")

        if resume_session_id is not None:
            token = await self._sessions.reactivate(resume_session_id)
            row = await self._sessions.get(resume_session_id)
        else:
            row, token = await self._sessions.create(
                role, agent=agent, project_id=project_id, task_id=task_id, run_id=run_id
            )

        request = SessionRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            cwd=cwd,
            model=agent["model"],
            effort=agent.get("effort"),
            mcp_url=f"{self._mcp_base_url}/mcp/{role.value}",
            mcp_token=token,
            log_path=Path(row.log_path),
            resume_id=row.cli_session_id if resume_session_id is not None else None,
            read_only=read_only,
            mask_values=mask_values,
        )
        idle_limit = 60.0 * await self._settings.get("watchdog_idle_minutes")
        process = await adapter.start(request)
        result, fired = await self._supervise(row.id, process, idle_limit)

        info = await self._sessions.info(row.id)
        outcome = info.outcome
        if outcome is None:
            if fired:
                outcome = SessionOutcome.WATCHDOG
            elif result.error_kind == "limit":
                outcome = SessionOutcome.LIMIT
            elif result.error_kind != "none":
                outcome = SessionOutcome.ERROR
        if result.cli_session_id:
            await self._sessions.set_cli_session_id(row.id, result.cli_session_id)
        await self._sessions.end(
            row.id, _final_status(outcome, fired), outcome=outcome, usage=result.usage
        )
        return LaunchResult(row.id, result, outcome, fired)

    async def _supervise(
        self, session_id: int, process: Any, idle_limit: float
    ) -> tuple[SessionResult, bool]:
        last_event = time.monotonic()
        last_touch = 0.0

        async def consume() -> None:
            nonlocal last_event, last_touch
            async for event in process.events():
                last_event = time.monotonic()
                if event.kind == "init" and event.data.get("session_id"):
                    await self._sessions.set_cli_session_id(session_id, event.data["session_id"])
                if last_event - last_touch >= TOUCH_INTERVAL_SECONDS:
                    last_touch = last_event
                    await self._sessions.touch(session_id)

        consumer = asyncio.create_task(consume())
        fired = False
        try:
            while not consumer.done():
                await asyncio.wait({consumer}, timeout=self._check_seconds)
                if not consumer.done() and time.monotonic() - last_event > idle_limit:
                    log.warning("watchdog: session %s idle, terminating", session_id)
                    fired = True
                    await process.terminate()
                    break
        except asyncio.CancelledError:
            await process.terminate()
            raise
        finally:
            if not consumer.done():
                consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await consumer
        return await process.wait(), fired


def _final_status(outcome: SessionOutcome | None, fired: bool) -> SessionStatus:
    if fired:
        return SessionStatus.KILLED
    if outcome in PAUSING_OUTCOMES:
        return SessionStatus.PAUSED
    if outcome in (SessionOutcome.ERROR, SessionOutcome.LIMIT):
        return SessionStatus.CRASHED
    return SessionStatus.ENDED
