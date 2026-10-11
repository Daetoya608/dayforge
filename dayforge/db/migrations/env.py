import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from dayforge.config import StorageConfig
from dayforge.db import models  # noqa: F401 — регистрирует таблицы в metadata
from dayforge.db.base import Base
from dayforge.db.session import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    # Тесты передают URL явно; в остальных случаях — из BASE_DIR.
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    storage = StorageConfig()
    storage.data_dir.mkdir(parents=True, exist_ok=True)
    return storage.db_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite не умеет ALTER большинства ограничений
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_engine(_db_url())
    async with engine.connect() as connection:
        await connection.run_sync(_configure)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_db_url(), target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
