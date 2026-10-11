"""Сборка процесса: бот, планировщик, MCP-сервер в одном event loop (docs/architecture.md)."""

import asyncio
import logging

from dayforge.bot.app import create_bot, create_dispatcher
from dayforge.config import Config
from dayforge.core.settings import SettingsService
from dayforge.db.session import create_engine, create_session_factory
from dayforge.logging_setup import configure_logging
from dayforge.mcp_server.app import build_app, build_servers, create_server
from dayforge.scheduler import create_scheduler

log = logging.getLogger(__name__)


def _prepare_dirs(config: Config) -> None:
    for path in (config.data_dir, config.sessions_log_dir, config.backup_dir, config.projects_dir):
        path.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    config = Config()  # type: ignore[call-arg]  # поля приходят из окружения
    configure_logging(config.log_level)
    _prepare_dirs(config)

    engine = create_engine(config.db_url)
    settings = SettingsService(create_session_factory(engine))

    scheduler = create_scheduler(await settings.get("timezone"))
    mcp_server = create_server(
        build_app(build_servers(), host=config.mcp_host),
        host=config.mcp_host,
        port=config.mcp_port,
    )
    bot = create_bot(config.telegram_bot_token.get_secret_value())
    dispatcher = create_dispatcher(config.owner_telegram_id)

    scheduler.start()
    mcp_task = asyncio.create_task(mcp_server.serve(), name="mcp-server")
    log.info("Dayforge started: MCP on %s:%s", config.mcp_host, config.mcp_port)
    try:
        # start_polling сам обрабатывает SIGINT/SIGTERM и возвращает управление.
        await dispatcher.start_polling(bot)
    finally:
        mcp_server.should_exit = True
        await mcp_task
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()
        log.info("Dayforge stopped")
