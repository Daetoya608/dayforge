"""Интерфейс запуска агентов (docs/agents.md#agentadapter).

Флаги конкретных CLI упоминаются только в реализациях адаптеров.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

EventKind = Literal["init", "text", "tool_call", "tool_result", "usage", "error", "result"]
ErrorKind = Literal["none", "limit", "auth", "crash"]


@dataclass(frozen=True, kw_only=True)
class SessionRequest:
    prompt: str
    system_prompt: str
    cwd: Path
    model: str
    effort: str | None
    mcp_url: str
    mcp_token: str
    log_path: Path
    resume_id: str | None = None
    read_only: bool = False


@dataclass(frozen=True, kw_only=True)
class AgentEvent:
    ts: datetime
    kind: EventKind
    data: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class SessionResult:
    exit_code: int
    cli_session_id: str | None
    error_kind: ErrorKind
    limit_resets_at: datetime | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    final_text: str | None = None


class AgentProcess(Protocol):
    def events(self) -> AsyncIterator[AgentEvent]: ...

    async def wait(self) -> SessionResult: ...

    async def terminate(self) -> None:
        """SIGTERM группе процессов, через 30 секунд — SIGKILL."""
        ...


class AgentAdapter(Protocol):
    name: str

    async def start(self, request: SessionRequest) -> AgentProcess: ...
