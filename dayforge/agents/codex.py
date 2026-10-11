"""Адаптер Codex: `codex exec --json` (docs/agents.md#codex).

Флаги и формат событий — уточнять при обновлении CLI: это единственное место, где они описаны.
"""

import os
import re
from datetime import datetime
from typing import Any

from dayforge.agents.base import AgentEvent, SessionRequest
from dayforge.agents.process import CliProcess, ParseState, event, launch_prefix

MCP_TOKEN_ENV = "DAYFORGE_MCP_TOKEN"
_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached", re.IGNORECASE)


class CodexAdapter:
    name = "codex"

    def __init__(self, *, binary: str = "codex", agent_unix_user: str | None = None) -> None:
        self._binary = binary
        self._agent_unix_user = agent_unix_user

    async def start(self, request: SessionRequest) -> CliProcess:
        # У codex exec нет отдельного системного промпта — роль идёт в начале сообщения.
        prompt = f"{request.system_prompt}\n\n---\n\n{request.prompt}"
        options = [
            "--json",
            "--model",
            request.model,
            "-c",
            f'mcp_servers.dayforge.url="{request.mcp_url}"',
            "-c",
            f'mcp_servers.dayforge.bearer_token_env_var="{MCP_TOKEN_ENV}"',
            "--cd",
            str(request.cwd),
            "--skip-git-repo-check",
        ]
        if request.effort:
            options += ["-c", f'model_reasoning_effort="{request.effort}"']
        if request.read_only:
            options += ["--sandbox", "read-only"]
        else:
            options += ["--dangerously-bypass-approvals-and-sandbox"]

        command = [*launch_prefix(self._agent_unix_user), self._binary, "exec"]
        if request.resume_id:
            command += ["resume", request.resume_id]
        command += [*options, prompt]

        return await CliProcess.start(
            command,
            cwd=request.cwd,
            env={**os.environ, MCP_TOKEN_ENV: request.mcp_token},
            parser=parse_line,
            detect_limit=detect_limit,
            log_path=request.log_path,
            mask_values=[request.mcp_token, *request.mask_values],
        )


def parse_line(data: dict[str, Any], state: ParseState) -> list[AgentEvent]:
    kind = data.get("type")
    if kind == "thread.started":
        state.cli_session_id = data.get("thread_id") or state.cli_session_id
        return [event("init", session_id=state.cli_session_id)]

    if kind in ("item.started", "item.completed"):
        item = data.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message" and kind == "item.completed":
            state.final_text = item.get("text", "")
            return [event("text", text=state.final_text)]
        if item_type in ("command_execution", "mcp_tool_call", "file_change", "web_search"):
            name = "tool_call" if kind == "item.started" else "tool_result"
            return [event(name, item_type=item_type, id=item.get("id"))]
        return []

    if kind == "turn.completed":
        state.usage = {"usage": data.get("usage") or {}}
        return [event("usage", **state.usage)]

    if kind in ("turn.failed", "error"):
        message = data.get("message") or (data.get("error") or {}).get("message") or ""
        state.error_text = str(message)
        is_limit, resets_at = detect_limit(state.error_text)
        state.error_kind = "limit" if is_limit else "crash"
        state.limit_resets_at = resets_at
        return [event("error", message=state.error_text)]

    return []


def detect_limit(text: str) -> tuple[bool, datetime | None]:
    return bool(_LIMIT_RE.search(text)), None
