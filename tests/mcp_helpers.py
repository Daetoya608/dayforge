"""Поднятие MCP-сервера Dayforge на свободном порту и вызов инструментов из тестов."""

import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from dayforge.core.enums import AgentRole
from dayforge.mcp_server.app import build_app, create_server, endpoint_path
from dayforge.services import Services

HOST = "127.0.0.1"


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def start_planning(self, project_id: int) -> None:
        self.calls.append(("start_planning", project_id))

    async def start_replan(self, project_id: int, instructions: str) -> None:
        self.calls.append(("start_replan", (project_id, instructions)))

    async def send_plan_feedback(self, plan_id: int, feedback: str) -> None:
        self.calls.append(("send_plan_feedback", (plan_id, feedback)))

    def kick_runner(self) -> None:
        self.calls.append(("kick_runner", None))

    async def release(self, project_id: int) -> str:
        self.calls.append(("release", project_id))
        return "released"

    async def archive_project(self, project_id: int) -> str:
        self.calls.append(("archive_project", project_id))
        return "archived"

    async def delete_project(self, project_id: int, *, delete_remote: bool) -> str:
        self.calls.append(("delete_project", (project_id, delete_remote)))
        return "deleted"

    async def approve_plan(self, plan_id: int) -> Any:
        self.calls.append(("approve_plan", plan_id))

    async def reject_plan(self, plan_id: int) -> Any:
        self.calls.append(("reject_plan", plan_id))

    async def after_answer(self, outcome: Any) -> None:
        self.calls.append(("after_answer", outcome))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def running_server(services: Services) -> AsyncIterator[int]:
    import asyncio

    port = free_port()
    server = create_server(build_app(services, host=HOST), host=HOST, port=port)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 — у uvicorn нет события старта
        await asyncio.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def client(port: int, role: AgentRole, token: str | None) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"http://{HOST}:{port}{endpoint_path(role)}"
    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(url, http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, **arguments: Any) -> tuple[bool, Any]:
    """(is_error, payload) — payload разобран из JSON."""
    result = await session.call_tool(tool, arguments)
    text = result.content[0].text if result.content else "{}"  # type: ignore[union-attr]
    if result.is_error and "{" in text:
        # SDK добавляет префикс «Error executing tool …:» перед нашим JSON.
        text = text[text.index("{") :]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    return bool(result.is_error), payload
