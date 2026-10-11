"""Конфигурация из окружения (`.env`). Всё, что меняется из бота, — в таблице settings."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageConfig(BaseSettings):
    """Пути к данным. Отдельно от Config, чтобы Alembic работал без токенов."""

    # env_ignore_empty: пустые строки из .env.example (`GITHUB_TOKEN=`) считаются незаданными.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    base_dir: Path = Path("/srv/dayforge")

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dayforge.sqlite3"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def sessions_log_dir(self) -> Path:
        return self.data_dir / "logs" / "sessions"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    @property
    def projects_dir(self) -> Path:
        return self.base_dir / "projects"


class Config(StorageConfig):
    telegram_bot_token: SecretStr
    owner_telegram_id: int

    github_token: SecretStr | None = None
    github_owner: str | None = None

    # None — агенты запускаются от текущего пользователя (локальная разработка).
    agent_unix_user: str | None = None

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765

    restic_repository: str | None = None
    restic_password_file: Path | None = None

    log_level: str = "INFO"
