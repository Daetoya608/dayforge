"""Поддельный адаптер агента для тестов раннера и оркестратора.

Сценарий — async-функция (call, services) → SessionResult-поля; она делает то, что сделал бы
агент: меняет файлы, коммитит и вызывает те же сервисы, что и MCP-инструменты.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from dayforge.agents.base import AgentEvent, SessionRequest, SessionResult
from dayforge.core.enums import SessionOutcome
from dayforge.core.sessions import SessionInfo
from dayforge.db.base import utcnow
from dayforge.services import Services
from dayforge.vcs.git import Git


@dataclass
class Call:
    request: SessionRequest
    session: SessionInfo
    number: int  # порядковый номер вызова агента в тесте

    @property
    def git(self) -> Git:
        return Git(self.request.cwd)


Script = Callable[[Call, Services], Awaitable[dict[str, Any] | None]]


class ScriptedProcess:
    def __init__(self, session_id: str, result: dict[str, Any], hang: bool) -> None:
        self._session_id = session_id
        self._result = result
        self._hang = hang
        self._stopped = asyncio.Event()

    async def events(self) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(ts=utcnow(), kind="init", data={"session_id": self._session_id})
        if self._hang:
            await self._stopped.wait()

    async def wait(self) -> SessionResult:
        if self._stopped.is_set():
            return SessionResult(exit_code=-15, cli_session_id=self._session_id, error_kind="crash")
        return SessionResult(
            exit_code=self._result.get("exit_code", 0),
            cli_session_id=self._session_id,
            error_kind=self._result.get("error_kind", "none"),
            limit_resets_at=self._result.get("limit_resets_at"),
            final_text=self._result.get("final_text"),
        )

    async def terminate(self) -> None:
        self._stopped.set()


@dataclass
class ScriptedAdapter:
    services: Services
    script: Script
    name: str = "claude_code"
    calls: list[Call] = field(default_factory=list)

    async def start(self, request: SessionRequest) -> ScriptedProcess:
        info = await self.services.sessions.authenticate(request.mcp_token)
        assert info is not None, "токен сессии должен быть действующим"
        call = Call(request, info, len(self.calls) + 1)
        self.calls.append(call)
        result = await self.script(call, self.services) or {}
        cli_id = request.resume_id or f"cli-{call.number}"
        return ScriptedProcess(cli_id, result, hang=result.get("hang", False))


# --- действия «агента» — то же, что делают MCP-инструменты ---


async def commit_file(call: Call, name: str, text: str | None = None) -> None:
    (call.request.cwd / name).write_text(text or f"call {call.number}\n")
    await call.git.commit_all(f"feat: {name}")


async def complete(call: Call, services: Services, summary: str = "готово") -> None:
    assert call.session.task_id is not None
    await services.tasks.mark_verifying(call.session.task_id, summary=summary, decisions=[])
    await services.sessions.record_outcome(call.session.id, SessionOutcome.COMPLETED)


async def ask(call: Call, services: Services, text: str = "Какой провайдер?") -> int:
    assert call.session.project_id is not None
    question_id = await services.questions.ask(
        project_id=call.session.project_id,
        task_id=call.session.task_id,
        session_id=call.session.id,
        questions=[{"text": text}],
    )
    await services.sessions.record_outcome(call.session.id, SessionOutcome.QUESTION)
    return question_id
