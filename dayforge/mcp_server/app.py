"""MCP-сервер для агентов: по эндпоинту на роль (docs/mcp-tools.md).

Все три сервера живут в одном Starlette-приложении внутри основного процесса.
"""

import contextlib
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.routing import BaseRoute

from dayforge.core.enums import AgentRole

# Имя сервера в конфиге агента: инструменты видны как mcp__dayforge__<имя>.
SERVER_NAME = "dayforge"


def endpoint_path(role: AgentRole) -> str:
    return f"/mcp/{role.value}"


def build_servers() -> dict[AgentRole, MCPServer]:
    return {role: MCPServer(name=SERVER_NAME) for role in AgentRole}


def build_app(servers: dict[AgentRole, MCPServer], *, host: str) -> Starlette:
    routes: list[BaseRoute] = []
    for role, server in servers.items():
        sub_app = server.streamable_http_app(streamable_http_path=endpoint_path(role), host=host)
        routes.extend(sub_app.routes)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for server in servers.values():
                await stack.enter_async_context(server.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)


class EmbeddedServer(uvicorn.Server):
    """uvicorn внутри общего event loop: сигналы обрабатывает aiogram, а не uvicorn."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[no-untyped-def]
        yield


def create_server(app: Starlette, *, host: str, port: int) -> EmbeddedServer:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
    return EmbeddedServer(config)
