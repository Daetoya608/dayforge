import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.backup import BackupService
from dayforge.core.events import BackupFailed, RecordingNotifier
from dayforge.core.secrets import EnvFile
from dayforge.services import Services, build_services
from tests.test_runner import approved_project

FAKE_RESTIC = str(Path(__file__).parent / "fake_cli" / "fake_restic.py")
MONDAY, SUNDAY = date(2026, 11, 2), date(2026, 11, 8)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def services(
    session_factory: async_sessionmaker[AsyncSession], notifier: RecordingNotifier, tmp_path: Path
) -> Services:
    (tmp_path / "ws").mkdir()
    return build_services(
        session_factory,
        notifier,
        projects_dir=tmp_path / "ws",
        sessions_log_dir=tmp_path / "logs",
        vcs_provider=None,
    )


def backup_service(services: Services, tmp_path: Path) -> BackupService:
    system_env = tmp_path / "system.env"
    system_env.write_text("TELEGRAM_BOT_TOKEN=x\n")
    return BackupService(
        services,
        db_path=tmp_path / "test.sqlite3",
        backup_dir=tmp_path / "backup",
        system_env=system_env,
        restic_repository="s3:https://example/bucket",
        restic_password_file=tmp_path / "pw",
        restic_binary=FAKE_RESTIC,
    )


def calls(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


async def test_backup_snapshot_and_retention(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "restic.log"
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(log))
    project = await approved_project(services)
    EnvFile(Path(project.repo_path) / ".env").set("API_KEY", "secret")

    await backup_service(services, tmp_path).run(today=MONDAY)

    backup, forget = calls(log)
    assert backup["argv"][-2:] == ["--tag", "daily"]  # type: ignore[index]
    assert str(tmp_path / "system.env") in backup["argv"]  # type: ignore[operator]
    assert backup["files"] == ["dayforge.sqlite3", "env/todo-bot.env"]
    assert backup["repository"] == "s3:https://example/bucket"
    assert forget["argv"] == [
        "forget",
        "--keep-daily",
        "7",
        "--keep-weekly",
        "4",
        "--keep-monthly",
        "6",
        "--prune",
    ]
    assert not (tmp_path / "backup").exists()  # временные файлы убраны


async def test_snapshot_is_a_valid_database(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(tmp_path / "restic.log"))
    await approved_project(services)
    service = backup_service(services, tmp_path)

    await service._snapshot()

    with sqlite3.connect(tmp_path / "backup" / "dayforge.sqlite3") as db:
        assert db.execute("select slug from projects").fetchall() == [("todo-bot",)]


async def test_sunday_runs_check(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "restic.log"
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(log))

    await backup_service(services, tmp_path).run(today=SUNDAY)

    assert [c["argv"][0] for c in calls(log)] == ["backup", "forget", "check"]  # type: ignore[index]


async def test_failure_is_reported(
    services: Services,
    notifier: RecordingNotifier,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(tmp_path / "restic.log"))
    monkeypatch.setenv("FAKE_RESTIC_EXIT", "3")

    await backup_service(services, tmp_path).run(today=MONDAY)

    (event,) = notifier.of_type(BackupFailed)
    assert "restic backup завершился с кодом 3" in event.error
