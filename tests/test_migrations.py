from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

ROOT = Path(__file__).resolve().parent.parent


def test_migrations_match_models(tmp_path: Path) -> None:
    alembic_config = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic_config.set_main_option(
        "sqlalchemy.url", f"sqlite+aiosqlite:///{tmp_path / 'migrated.sqlite3'}"
    )

    command.upgrade(alembic_config, "head")
    # Падает, если модели изменены, а миграция не создана.
    command.check(alembic_config)
    command.downgrade(alembic_config, "base")
