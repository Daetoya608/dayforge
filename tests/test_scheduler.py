import os
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.agents.launcher import AgentLauncher
from dayforge.core.enums import RunMode
from dayforge.core.events import PendingActionExpired, RecordingNotifier
from dayforge.runner.runner import Runner
from dayforge.scheduler import DayforgeScheduler
from dayforge.services import Services, build_services
from tests.scripted_agent import ScriptedAdapter
from tests.test_runner import approved_project

SessionFactory = async_sessionmaker[AsyncSession]


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


@pytest.fixture
def runner(services: Services) -> Runner:
    async def idle(call, services):  # type: ignore[no-untyped-def]
        return None

    launcher = AgentLauncher(
        services.sessions,
        services.settings,
        {"claude_code": ScriptedAdapter(services, idle)},  # type: ignore[dict-item]
        mcp_base_url="http://127.0.0.1:1",
    )
    return Runner(services, launcher)


async def test_daily_run_queues_projects_with_slot(
    services: Services, runner: Runner, tmp_path: Path
) -> None:
    project = await approved_project(services)
    scheduler = DayforgeScheduler(services, runner, sessions_log_dir=tmp_path / "logs")

    assert await scheduler.daily_run() == 1
    (queued,) = await services.runs.queued()
    assert (queued.project_id, queued.mode, queued.slot_date) == (
        project.id,
        RunMode.SCHEDULED,
        await services.calendar.today(),
    )
    assert await scheduler.daily_run() == 0  # слот уже занят


async def test_jobs_follow_settings(services: Services, runner: Runner, tmp_path: Path) -> None:
    backups: list[bool] = []

    async def backup() -> None:
        backups.append(True)

    scheduler = DayforgeScheduler(
        services, runner, sessions_log_dir=tmp_path / "logs", backup=backup
    )
    await scheduler.start()
    try:
        before = scheduler.next_run_times()
        assert set(before) == {"daily_run", "backup", "expire_actions", "cleanup_logs"}
        assert "T03:00:00+03:00" in before["daily_run"]

        await services.settings.set("run_time", "04:30")
        await scheduler.reschedule()

        assert "T04:30:00+03:00" in scheduler.next_run_times()["daily_run"]
        await scheduler.backup()
        assert backups == [True]
    finally:
        scheduler.shutdown()


async def test_expired_actions_are_reported(
    services: Services, runner: Runner, notifier: RecordingNotifier, tmp_path: Path
) -> None:
    async def handler(payload: dict[str, object]) -> str:
        return "ok"

    services.actions.register("release", handler)
    services.actions._ttl = timedelta(seconds=-1)  # сразу просрочено
    action_id = await services.actions.create("release", {}, description="релиз")
    scheduler = DayforgeScheduler(services, runner, sessions_log_dir=tmp_path / "logs")

    await scheduler.expire_actions()

    (event,) = notifier.of_type(PendingActionExpired)
    assert event.action_id == action_id


async def test_cleanup_removes_only_old_logs(
    services: Services, runner: Runner, tmp_path: Path
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    old, fresh = logs / "1.jsonl", logs / "2.jsonl"
    old.write_text("{}")
    fresh.write_text("{}")
    month_ago = time.time() - 31 * 86400
    os.utime(old, (month_ago, month_ago))
    scheduler = DayforgeScheduler(services, runner, sessions_log_dir=logs)

    assert await scheduler.cleanup_logs() == 1
    assert not old.exists() and fresh.exists()
