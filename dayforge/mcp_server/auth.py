"""Авторизация вызовов MCP по токену сессии агента (docs/mcp-tools.md#транспорт-и-авторизация)."""

import json
import re
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from starlette.types import ASGIApp, Receive, Scope, Send

from dayforge.core.enums import AgentRole
from dayforge.core.sessions import SessionInfo, SessionService

_PATH_RE = re.compile(r"^/mcp/(executor|planner|system)/?$")
STATE_KEY = "dayforge_session_id"


class SessionAuthMiddleware:
    """Пускает к /mcp/<роль> только с действующим токеном сессии этой роли."""

    def __init__(self, app: ASGIApp, sessions: SessionService) -> None:
        self._app = app
        self._sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match = _PATH_RE.match(scope.get("path", "")) if scope["type"] == "http" else None
        if match is None:
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode(errors="replace")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        info = await self._sessions.authenticate(token) if token else None
        if info is None:
            await _reject(send, 401, "unauthorized", "нужен действующий токен сессии")
            return
        if info.role is not AgentRole(match.group(1)):
            await _reject(send, 403, "forbidden", "токен выдан для другой роли")
            return
        scope.setdefault("state", {})[STATE_KEY] = info.id
        await self._app(scope, receive, send)


async def _reject(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": code, "message": message}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def tool_error(code: str, message: str, **extra: Any) -> ToolError:
    return ToolError(json.dumps({"error": code, "message": message, **extra}, ensure_ascii=False))


async def current_session(
    ctx: Context, sessions: SessionService, *, finalizing_allowed: bool = False
) -> SessionInfo:
    """Сессия вызывающего агента. После завершающего вызова другие вызовы отклоняются."""
    request = ctx.request_context.request
    session_id = getattr(getattr(request, "state", None), STATE_KEY, None)
    if session_id is None:
        raise tool_error("unauthorized", "вызов без сессии")
    info = await sessions.info(session_id)
    if info.outcome is not None and not finalizing_allowed:
        raise tool_error(
            "session_finalizing",
            "сессия завершается: закоммить работу и выйди, другие вызовы недоступны",
        )
    return info
