"""Поддельный `claude` для сквозного теста: ведёт себя как агент, вызывающий MCP по HTTP.

Читает --mcp-config, подставляет ${ПЕРЕМЕННЫЕ} окружения в заголовки (как Claude Code),
выполняет действия из FAKE_ACTIONS (JSON) в рабочей директории и печатает stream-json.
Действия: {"write": путь, "text": …}, {"commit": сообщение}, {"tool": имя, "args": {…}}.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


def expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


async def call_tools(url: str, headers: dict[str, str], calls: list[dict]) -> list[str]:
    results = []
    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(url, http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        for call in calls:
            result = await session.call_tool(call["tool"], call.get("args", {}))
            results.append(result.content[0].text if result.content else "")
    return results


def main() -> int:
    argv = sys.argv[1:]
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
    server = config["mcpServers"]["dayforge"]
    headers = {k: expand(v) for k, v in server["headers"].items()}
    print(
        json.dumps({"type": "system", "subtype": "init", "session_id": "e2e-session"}), flush=True
    )

    tool_calls = []
    for action in json.loads(os.environ["FAKE_ACTIONS"]):
        if "write" in action:
            Path(action["write"]).write_text(action.get("text", "x\n"))
        elif "commit" in action:
            subprocess.run(["git", "add", "-A"], check=True)
            subprocess.run(["git", "commit", "-q", "-m", action["commit"]], check=True)
        elif "tool" in action:
            tool_calls.append(action)
    results = asyncio.run(call_tools(server["url"], headers, tool_calls)) if tool_calls else []
    for text in results:
        print(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            ),
            flush=True,
        )
    print(
        json.dumps(
            {"type": "result", "is_error": False, "result": "готово", "session_id": "e2e-session"}
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
