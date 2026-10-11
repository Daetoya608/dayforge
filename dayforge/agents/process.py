"""Общий запуск CLI агента: процесс в своей группе, построчный разбор вывода, лог JSONL."""

import asyncio
import contextlib
import json
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dayforge.agents.base import AgentEvent, ErrorKind, SessionResult
from dayforge.db.base import utcnow

TERMINATE_GRACE_SECONDS = 30
STDERR_TAIL_LINES = 200
MASK = "***"
# Строки вывода агента бывают огромными (содержимое файлов) — лимит буфера читателя.
STREAM_LIMIT = 16 * 1024 * 1024


@dataclass
class ParseState:
    """Что адаптер узнал из потока событий к концу сессии."""

    cli_session_id: str | None = None
    final_text: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error_kind: ErrorKind = "none"
    limit_resets_at: datetime | None = None
    error_text: str | None = None


# Разбор одной JSON-строки вывода CLI в события; состояние обновляется по ходу.
LineParser = Callable[[dict[str, Any], ParseState], list[AgentEvent]]
# Распознавание лимита подписки по тексту ошибки: (лимит?, время сброса).
LimitDetector = Callable[[str], tuple[bool, datetime | None]]


def launch_prefix(agent_unix_user: str | None) -> list[str]:
    """Запуск от отдельного пользователя через обёртку (docs/operations.md#пользователи)."""
    if not agent_unix_user:
        return []
    return ["sudo", "-n", "-H", "-u", agent_unix_user, "/usr/local/bin/dayforge-agent-exec"]


class CliProcess:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        parser: LineParser,
        detect_limit: LimitDetector,
        log_path: Path,
        mask_values: Iterable[str],
        cleanup: Callable[[], None] = lambda: None,
    ) -> None:
        self._process = process
        self._parser = parser
        self._detect_limit = detect_limit
        self._log_path = log_path
        self._mask_values = sorted({v for v in mask_values if len(v) >= 4}, key=len, reverse=True)
        self._cleanup = cleanup
        self._state = ParseState()
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._consumed = False

    @classmethod
    async def start(
        cls,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        **kwargs: Any,
    ) -> "CliProcess":
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # своя группа процессов — terminate() гасит и потомков
            limit=STREAM_LIMIT,
        )
        return cls(process, **kwargs)

    @property
    def pid(self) -> int:
        return self._process.pid

    async def events(self) -> AsyncIterator[AgentEvent]:
        assert self._process.stdout is not None
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a") as log:
            async for raw in self._process.stdout:
                line = self._mask(raw.decode(errors="replace").rstrip("\n"))
                if not line.strip():
                    continue
                log.write(line + "\n")
                log.flush()
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                for event in self._parser(data, self._state):
                    yield event
        self._consumed = True

    async def wait(self) -> SessionResult:
        if not self._consumed:
            async for _ in self.events():
                pass
        exit_code = await self._process.wait()
        with contextlib.suppress(Exception):
            await self._stderr_task
        self._cleanup()
        self._append_stderr_to_log()

        state = self._state
        error_kind = state.error_kind
        if error_kind == "none" and exit_code != 0:
            text = "\n".join([state.error_text or "", *self._stderr])
            is_limit, resets_at = self._detect_limit(text)
            if is_limit:
                error_kind, state.limit_resets_at = "limit", resets_at
            else:
                error_kind = "crash"
        return SessionResult(
            exit_code=exit_code,
            cli_session_id=state.cli_session_id,
            error_kind=error_kind,
            limit_resets_at=state.limit_resets_at,
            usage=state.usage,
            final_text=state.final_text,
        )

    async def terminate(self) -> None:
        if self._process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self._process.wait(), TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._process.pid, signal.SIGKILL)
            await self._process.wait()

    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    async def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        async for raw in self._process.stderr:
            self._stderr.append(self._mask(raw.decode(errors="replace").rstrip("\n")))

    def _append_stderr_to_log(self) -> None:
        if not self._stderr:
            return
        with self._log_path.open("a") as log:
            record = {
                "ts": utcnow().isoformat(),
                "type": "dayforge.stderr",
                "lines": list(self._stderr),
            }
            log.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _mask(self, text: str) -> str:
        for value in self._mask_values:
            text = text.replace(value, MASK)
        return text


def event(kind: str, **data: Any) -> AgentEvent:
    return AgentEvent(ts=utcnow(), kind=kind, data=data)  # type: ignore[arg-type]
