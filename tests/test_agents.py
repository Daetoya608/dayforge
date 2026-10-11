import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dayforge.agents.base import SessionRequest
from dayforge.agents.claude_code import ClaudeCodeAdapter
from dayforge.agents.codex import CodexAdapter

FAKE = str(Path(__file__).parent / "fake_cli" / "fake_agent.py")

CLAUDE_OUTPUT = [
    {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "opus"},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Смотрю код, ключ sk_live_SECRET"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            ]
        },
    },
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    "not json at all",
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Готово",
        "session_id": "sess-1",
        "usage": {"input_tokens": 10},
        "total_cost_usd": 0.5,
        "num_turns": 3,
    },
]


def request(tmp_path: Path, **overrides: Any) -> SessionRequest:
    fields: dict[str, Any] = {
        "prompt": "Сделай задачу T1",
        "system_prompt": "Ты разработчик",
        "cwd": tmp_path,
        "model": "opus",
        "effort": "high",
        "mcp_url": "http://127.0.0.1:8765/mcp/executor",
        "mcp_token": "token-123456",
        "log_path": tmp_path / "logs" / "1.jsonl",
        "mask_values": ("sk_live_SECRET",),
    }
    return SessionRequest(**(fields | overrides))


def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: list[Any], **env: str) -> Path:
    output = tmp_path / "output.txt"
    output.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines))
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_OUTPUT", str(output))
    monkeypatch.setenv("FAKE_RECORD", str(record))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return record


class TestClaudeCode:
    async def test_command_events_and_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = fake_env(monkeypatch, tmp_path, CLAUDE_OUTPUT)
        process = await ClaudeCodeAdapter(binary=FAKE).start(request(tmp_path, resume_id="sess-0"))

        kinds = [e.kind async for e in process.events()]
        result = await process.wait()

        assert kinds == ["init", "text", "tool_call", "tool_result", "result"]
        assert (result.exit_code, result.error_kind, result.cli_session_id) == (0, "none", "sess-1")
        assert result.final_text == "Готово"
        assert result.usage["cost_usd"] == 0.5

        called = json.loads(record.read_text())
        argv = called["argv"]
        assert argv[:2] == ["-p", "Сделай задачу T1"]
        assert argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--resume") + 1] == "sess-0"
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--strict-mcp-config" in argv
        assert called["env"] == {
            "DAYFORGE_MCP_TOKEN": "token-123456",
            "MAX_THINKING_TOKENS": "32000",
        }

        # Токен — только в окружении; конфиг ссылается на переменную и удаляется после сессии.
        config_path = Path(argv[argv.index("--mcp-config") + 1])
        assert "token-123456" not in " ".join(argv)
        assert not config_path.exists()

        log = (tmp_path / "logs" / "1.jsonl").read_text()
        assert "sk_live_SECRET" not in log and "***" in log
        assert "not json at all" in log

    async def test_read_only_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        record = fake_env(monkeypatch, tmp_path, CLAUDE_OUTPUT)
        process = await ClaudeCodeAdapter(binary=FAKE).start(request(tmp_path, read_only=True))
        await process.wait()

        argv = json.loads(record.read_text())["argv"]
        assert "--permission-mode" not in argv
        assert "mcp__dayforge__*" in argv[argv.index("--allowedTools") + 1]
        assert "Edit" in argv[argv.index("--disallowedTools") + 1]

    async def test_usage_limit_is_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset = int(datetime(2026, 11, 2, 15, 0, tzinfo=UTC).timestamp())
        fake_env(
            monkeypatch,
            tmp_path,
            [
                {
                    "type": "result",
                    "is_error": True,
                    "result": f"Claude AI usage limit reached|{reset}",
                    "session_id": "sess-9",
                }
            ],
            FAKE_EXIT="1",
        )
        process = await ClaudeCodeAdapter(binary=FAKE).start(request(tmp_path))

        result = await process.wait()

        assert result.error_kind == "limit"
        assert result.limit_resets_at == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)

    async def test_crash_with_stderr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_env(monkeypatch, tmp_path, [], FAKE_EXIT="2", FAKE_STDERR="boom token-123456")
        process = await ClaudeCodeAdapter(binary=FAKE).start(request(tmp_path))

        result = await process.wait()

        assert (result.exit_code, result.error_kind) == (2, "crash")
        log = (tmp_path / "logs" / "1.jsonl").read_text()
        assert "boom ***" in log and "token-123456" not in log

    async def test_terminate_kills_process_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_env(monkeypatch, tmp_path, [CLAUDE_OUTPUT[0]], FAKE_SLEEP="60")
        process = await ClaudeCodeAdapter(binary=FAKE).start(request(tmp_path))
        started = time.monotonic()

        await asyncio.sleep(0.3)
        await process.terminate()
        result = await process.wait()

        assert time.monotonic() - started < 10
        assert result.exit_code != 0


class TestCodex:
    async def test_command_and_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = fake_env(
            monkeypatch,
            tmp_path,
            [
                {"type": "thread.started", "thread_id": "th-1"},
                {"type": "item.started", "item": {"type": "command_execution", "id": "c1"}},
                {"type": "item.completed", "item": {"type": "command_execution", "id": "c1"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Готово"}},
                {"type": "turn.completed", "usage": {"input_tokens": 5}},
            ],
        )
        process = await CodexAdapter(binary=FAKE).start(
            request(tmp_path, effort="medium", resume_id="th-0")
        )

        kinds = [e.kind async for e in process.events()]
        result = await process.wait()

        assert kinds == ["init", "tool_call", "tool_result", "text", "usage"]
        assert (result.cli_session_id, result.final_text) == ("th-1", "Готово")
        argv = json.loads(record.read_text())["argv"]
        assert argv[:3] == ["exec", "resume", "th-0"]
        assert 'model_reasoning_effort="medium"' in argv
        assert 'mcp_servers.dayforge.bearer_token_env_var="DAYFORGE_MCP_TOKEN"' in argv
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert argv[-1].startswith("Ты разработчик\n\n---\n\nСделай задачу T1")

    async def test_read_only_and_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = fake_env(
            monkeypatch,
            tmp_path,
            [{"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}],
            FAKE_EXIT="1",
        )
        process = await CodexAdapter(binary=FAKE).start(request(tmp_path, read_only=True))

        result = await process.wait()

        argv = json.loads(record.read_text())["argv"]
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert result.error_kind == "limit"


def test_launch_prefix_for_agent_user() -> None:
    from dayforge.agents.process import launch_prefix

    assert launch_prefix(None) == []
    assert launch_prefix("dayforge-agent") == [
        "sudo",
        "-n",
        "-H",
        "-u",
        "dayforge-agent",
        "/usr/local/bin/dayforge-agent-exec",
    ]
