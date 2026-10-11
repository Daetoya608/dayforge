"""Сборка процесса: бот, планировщик, MCP-сервер в одном event loop (docs/architecture.md)."""

import asyncio
import logging

from dayforge.actions import register_action_handlers
from dayforge.bot.app import create_bot, create_dispatcher
from dayforge.config import Config
from dayforge.core.events import LogNotifier
from dayforge.db.session import create_engine, create_session_factory
from dayforge.logging_setup import configure_logging
from dayforge.mcp_server.app import build_app, create_server
from dayforge.scheduler import create_scheduler
from dayforge.services import build_services
from dayforge.vcs.github import GitHubProvider

log = logging.getLogger(__name__)


def _prepare_dirs(config: Config) -> None:
    for path in (config.data_dir, config.sessions_log_dir, config.backup_dir, config.projects_dir):
        path.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    config = Config()  # type: ignore[call-arg]  # поля приходят из окружения
    configure_logging(config.log_level)
    _prepare_dirs(config)

    engine = create_engine(config.db_url)
    provider = (
        GitHubProvider(config.github_token.get_secret_value(), config.github_owner)
        if config.github_token and config.github_owner
        else None
    )
    services = build_services(
        create_session_factory(engine),
        LogNotifier(),
        projects_dir=config.projects_dir,
        sessions_log_dir=config.sessions_log_dir,
        vcs_provider=provider,
    )
    register_action_handlers(services)

    scheduler = create_scheduler(await services.settings.get("timezone"))
    mcp_server = create_server(
        build_app(services, host=config.mcp_host),
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
        if provider is not None:
            await provider.aclose()
        await engine.dispose()
        log.info("Dayforge stopped")
