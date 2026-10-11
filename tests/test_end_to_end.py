"""Сквозной прогон: раннер → настоящий адаптер Claude Code → подпроцесс «агента» → MCP по HTTP."""

import json
import stat
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.agents.claude_code import ClaudeCodeAdapter
from dayforge.agents.launcher import AgentLauncher
from dayforge.core.enums import (
    PauseReason,
    ProjectStatus,
    RunMode,
    RunStatus,
    RunTrigger,
    SessionStatus,
    TaskStatus,
)
from dayforge.core.events import QuestionAsked, RecordingNotifier
from dayforge.db.models import AgentSession, Task
from dayforge.runner.runner import Runner
from dayforge.services import Services, build_services
from tests.mcp_helpers import HOST, running_server
from tests.test_runner import approved_project

FAKE = Path(__file__).parent / "fake_cli" / "fake_claude_mcp.py"
SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    """Обёртка, запускающая поддельный CLI интерпретатором с установленным SDK MCP."""
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def services(
    session_factory: SessionFactory, notifier: RecordingNotifier, tmp_path: Path
) -> Services:
    (tmp_path / "ws").mkdir()
    return build_services(
        session_factory,
        notifier,
        projects_dir=tmp_path / "ws",
        sessions_log_dir=tmp_path / "logs",
        vcs_provider=None,
    )


async def run_once(services: Services, fake_claude: str, port: int, project_id: int) -> RunStatus:
    launcher = AgentLauncher(
        services.sessions,
        services.settings,
        {"claude_code": ClaudeCodeAdapter(binary=fake_claude)},  # type: ignore[dict-item]
        mcp_base_url=f"http://{HOST}:{port}",
    )
    runner = Runner(services, launcher)
    (run,) = await services.runs.enqueue([project_id], mode=RunMode.NEXT, trigger=RunTrigger.BOT)
    await runner.execute_run(run)
    return (await services.runs.get(run.id)).status


async def test_task_completed_through_real_mcp(
    services: Services,
    session_factory: SessionFactory,
    fake_claude: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await approved_project(services)
    monkeypatch.setenv(
        "FAKE_ACTIONS",
        json.dumps(
            [
                {"write": "ok.txt"},
                {"commit": "feat(T1): ok"},
                {"tool": "complete_task", "args": {"summary": "сделано через MCP"}},
            ]
        ),
    )

    async with running_server(services) as port:
        status = await run_once(services, fake_claude, port, project.id)

    assert status is RunStatus.FINISHED
    async with session_factory() as session:
        tasks = {t.key: t for t in await session.scalars(select(Task))}
        sessions = list(await session.scalars(select(AgentSession)))
    assert tasks["T1"].status is TaskStatus.DONE
    assert tasks["T1"].summary == "сделано через MCP"
    assert sessions[0].cli_session_id == "e2e-session"
    assert sessions[0].status is SessionStatus.ENDED
    # Ответ MCP дошёл до «агента»: он напечатал его как свой текст.
    lines = [json.loads(line) for line in Path(sessions[0].log_path).read_text().splitlines()]
    replies = [
        json.loads(block["text"])
        for line in lines
        if line.get("type") == "assistant"
        for block in line["message"]["content"]
    ]
    assert replies[0]["status"] == "accepted"


async def test_question_through_real_mcp_blocks_project(
    services: Services,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    fake_claude: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await approved_project(services)
    monkeypatch.setenv(
        "FAKE_ACTIONS",
        json.dumps([{"tool": "ask_user", "args": {"questions": [{"text": "Какая БД?"}]}}]),
    )

    async with running_server(services) as port:
        status = await run_once(services, fake_claude, port, project.id)

    assert status is RunStatus.BLOCKED
    assert (await services.projects.get_by_id(project.id)).status is ProjectStatus.BLOCKED
    async with session_factory() as session:
        t1 = (await session.scalars(select(Task).where(Task.key == "T1"))).one()
    assert (t1.status, t1.pause_reason) == (TaskStatus.PAUSED, PauseReason.QUESTION)
    assert notifier.of_type(QuestionAsked)[0].questions[0]["text"] == "Какая БД?"
