"""Адаптер Claude Code: `claude -p` с потоковым JSON (docs/agents.md#claude-code).

Флаги и формат событий — уточнять при обновлении CLI: это единственное место, где они описаны.
"""

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dayforge.agents.base import AgentEvent, SessionRequest
from dayforge.agents.process import CliProcess, ParseState, event, launch_prefix

MCP_TOKEN_ENV = "DAYFORGE_MCP_TOKEN"
# Уровни reasoning, которые принимает `claude --effort` (проверено на Claude Code 2.1).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
READ_ONLY_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "mcp__dayforge__*",
]
WRITE_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
_LIMIT_RE = re.compile(r"usage limit|limit reached|rate limit", re.IGNORECASE)
_RESET_EPOCH_RE = re.compile(r"\|(\d{10})\b")


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(self, *, binary: str = "claude", agent_unix_user: str | None = None) -> None:
        self._binary = binary
        self._agent_unix_user = agent_unix_user

    async def start(self, request: SessionRequest) -> CliProcess:
        config_path = _write_mcp_config(request.mcp_url)
        command = [
            *launch_prefix(self._agent_unix_user),
            self._binary,
            "-p",
            request.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            request.model,
            "--append-system-prompt",
            request.system_prompt,
            "--mcp-config",
            str(config_path),
            "--strict-mcp-config",
        ]
        if request.read_only:
            command += ["--allowedTools", ",".join(READ_ONLY_TOOLS)]
            command += ["--disallowedTools", ",".join(WRITE_TOOLS)]
        else:
            command += ["--permission-mode", "bypassPermissions"]
        if request.effort in EFFORT_LEVELS:
            command += ["--effort", request.effort]  # type: ignore[list-item]
        if request.resume_id:
            command += ["--resume", request.resume_id]

        env = {**os.environ, MCP_TOKEN_ENV: request.mcp_token}
        return await CliProcess.start(
            command,
            cwd=request.cwd,
            env=env,
            parser=parse_line,
            detect_limit=detect_limit,
            log_path=request.log_path,
            mask_values=[request.mcp_token, *request.mask_values],
            cleanup=lambda: config_path.unlink(missing_ok=True),
        )


def _write_mcp_config(url: str) -> Path:
    """Конфиг без секретов: токен подставляется из переменной окружения процесса."""
    config = {
        "mcpServers": {
            "dayforge": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer ${{{MCP_TOKEN_ENV}}}"},
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix="dayforge-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as file:
        json.dump(config, file)
    os.chmod(path, 0o644)  # читает пользователь агента; секретов внутри нет
    return Path(path)


def parse_line(data: dict[str, Any], state: ParseState) -> list[AgentEvent]:
    kind = data.get("type")
    if kind == "system" and data.get("subtype") == "init":
        state.cli_session_id = data.get("session_id") or state.cli_session_id
        return [event("init", session_id=state.cli_session_id, model=data.get("model"))]

    if kind == "assistant":
        events = []
        for block in (data.get("message") or {}).get("content") or []:
            if block.get("type") == "text":
                events.append(event("text", text=block.get("text", "")))
            elif block.get("type") == "tool_use":
                events.append(event("tool_call", name=block.get("name"), input=block.get("input")))
        return events

    if kind == "user":
        return [
            event("tool_result", tool_use_id=block.get("tool_use_id"))
            for block in (data.get("message") or {}).get("content") or []
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

    if kind == "rate_limit_event":
        # Состояние лимита подписки приходит отдельным событием (Claude Code 2.1).
        info = data.get("rate_limit_info") or {}
        if info.get("resetsAt"):
            state.limit_resets_at = datetime.fromtimestamp(int(info["resetsAt"]), UTC)
        if info.get("status") not in (None, "allowed", "allowed_warning"):
            state.error_kind = "limit"
        return [event("usage", rate_limit=info)]

    if kind == "result":
        state.cli_session_id = data.get("session_id") or state.cli_session_id
        state.usage = {
            "usage": data.get("usage") or {},
            "cost_usd": data.get("total_cost_usd"),
            "turns": data.get("num_turns"),
        }
        text = str(data.get("result") or "")
        if data.get("is_error"):
            state.error_text = text
            is_limit, resets_at = detect_limit(text)
            if state.error_kind != "limit":
                state.error_kind = "limit" if is_limit else "crash"
            state.limit_resets_at = resets_at or state.limit_resets_at
        else:
            state.final_text = text
        return [event("result", is_error=bool(data.get("is_error")), subtype=data.get("subtype"))]

    return []


def detect_limit(text: str) -> tuple[bool, datetime | None]:
    if not _LIMIT_RE.search(text):
        return False, None
    match = _RESET_EPOCH_RE.search(text)
    resets_at = datetime.fromtimestamp(int(match.group(1)), UTC) if match else None
    return True, resets_at
