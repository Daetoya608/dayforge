"""MCP-сервер для агентов: по эндпоинту на роль (docs/mcp-tools.md).

Все три сервера живут в одном Starlette-приложении внутри основного процесса.
"""

import contextlib
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import BaseRoute

from dayforge.core.enums import AgentRole
from dayforge.mcp_server import executor, planner, system
from dayforge.mcp_server.auth import SessionAuthMiddleware
from dayforge.services import Services

# Имя сервера в конфиге агента: инструменты видны как mcp__dayforge__<имя>.
SERVER_NAME = "dayforge"

INSTRUCTIONS = {
    AgentRole.EXECUTOR: (
        "Инструменты Dayforge для исполнителя задачи. Закончи сессию одним из вызовов: "
        "complete_task, ask_user, request_secret или fail_task."
    ),
    AgentRole.PLANNER: (
        "Инструменты Dayforge для планировщика: уточняющие вопросы (ask_user) и отправка плана "
        "(submit_plan)."
    ),
    AgentRole.SYSTEM: (
        "Инструменты Dayforge для системного агента: чтение состояния системы и изменения "
        "(проекты, планы, календарь, запуски)."
    ),
}
REGISTRARS = {
    AgentRole.EXECUTOR: executor.register,
    AgentRole.PLANNER: planner.register,
    AgentRole.SYSTEM: system.register,
}


def endpoint_path(role: AgentRole) -> str:
    return f"/mcp/{role.value}"


def build_servers(services: Services) -> dict[AgentRole, MCPServer]:
    servers = {}
    for role in AgentRole:
        server = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS[role])
        REGISTRARS[role](server, services)
        servers[role] = server
    return servers


def build_app(services: Services, *, host: str) -> Starlette:
    servers = build_servers(services)
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

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(SessionAuthMiddleware, sessions=services.sessions)],
    )


class EmbeddedServer(uvicorn.Server):
    """uvicorn внутри общего event loop: сигналы обрабатывает aiogram, а не uvicorn."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[no-untyped-def]
        yield


def create_server(app: Starlette, *, host: str, port: int) -> EmbeddedServer:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
    return EmbeddedServer(config)
