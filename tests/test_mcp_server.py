import asyncio
import socket

import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dayforge.core.enums import AgentRole
from dayforge.mcp_server.app import build_app, build_servers, create_server, endpoint_path

HOST = "127.0.0.1"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


@pytest.fixture
async def mcp_port() -> int:
    port = _free_port()
    server = create_server(build_app(build_servers(), host=HOST), host=HOST, port=port)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 — у uvicorn нет события старта
        await asyncio.sleep(0.01)
    yield port
    server.should_exit = True
    await task


@pytest.mark.parametrize("role", list(AgentRole))
async def test_each_role_endpoint_speaks_mcp(mcp_port: int, role: AgentRole) -> None:
    url = f"http://{HOST}:{mcp_port}{endpoint_path(role)}"
    async with streamable_http_client(url) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            result = await session.initialize()
            assert result.server_info.name == "dayforge"
            await session.list_tools()
