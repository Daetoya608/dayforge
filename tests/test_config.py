from pathlib import Path

import pytest

from dayforge.config import Config


def test_reads_environment_and_derives_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "42")
    monkeypatch.setenv("BASE_DIR", "/tmp/df")

    config = Config(_env_file=None)  # type: ignore[call-arg]

    assert config.telegram_bot_token.get_secret_value() == "123:abc"
    assert config.owner_telegram_id == 42
    assert config.db_path == Path("/tmp/df/data/dayforge.sqlite3")
    assert config.projects_dir == Path("/tmp/df/projects")
    assert config.db_url == "sqlite+aiosqlite:////tmp/df/data/dayforge.sqlite3"


def test_empty_values_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "42")
    monkeypatch.setenv("AGENT_UNIX_USER", "")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "")

    config = Config(_env_file=None)  # type: ignore[call-arg]

    assert config.agent_unix_user is None
    assert config.restic_password_file is None
