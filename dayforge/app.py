"""Сборка процесса: бот, раннер, планировщик и MCP-сервер в одном event loop.

См. docs/architecture.md.
"""

import asyncio
import contextlib
import logging
from datetime import timedelta

from dayforge.actions import register_action_handlers
from dayforge.agents import build_adapters
from dayforge.agents.launcher import AgentLauncher
from dayforge.backup import BackupService
from dayforge.bot.app import create_bot, create_dispatcher, set_commands
from dayforge.bot.context import AppContext
from dayforge.bot.notifier import TelegramNotifier
from dayforge.bot.storage import BotKV, SqliteStorage
from dayforge.chat import ChatAgent
from dayforge.config import Config
from dayforge.db.session import create_engine, create_session_factory
from dayforge.logging_setup import configure_logging
from dayforge.mcp_server.app import build_app, create_server
from dayforge.orchestrator import Orchestrator
from dayforge.runner.runner import Runner
from dayforge.scheduler import DayforgeScheduler
from dayforge.services import build_services
from dayforge.vcs.github import GitHubProvider

log = logging.getLogger(__name__)


def _prepare_dirs(config: Config) -> None:
    for path in (config.data_dir, config.sessions_log_dir, config.projects_dir):
        path.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    config = Config()  # type: ignore[call-arg]  # поля приходят из окружения
    configure_logging(config.log_level)
    _prepare_dirs(config)

    engine = create_engine(config.db_url)
    session_factory = create_session_factory(engine)
    provider = (
        GitHubProvider(config.github_token.get_secret_value(), config.github_owner)
        if config.github_token and config.github_owner
        else None
    )
    bot = create_bot(config.telegram_bot_token.get_secret_value())
    notifier = TelegramNotifier(bot, config.owner_telegram_id)
    services = build_services(
        session_factory,
        notifier,
        projects_dir=config.projects_dir,
        sessions_log_dir=config.sessions_log_dir,
        vcs_provider=provider,
    )
    kv = BotKV(session_factory)
    notifier.attach(services, kv)
    register_action_handlers(services)

    launcher = AgentLauncher(
        services.sessions,
        services.settings,
        build_adapters(config.agent_unix_user),
        mcp_base_url=f"http://{config.mcp_host}:{config.mcp_port}",
    )
    runner = Runner(services, launcher, agent_unix_user=config.agent_unix_user)
    orchestrator = Orchestrator(services, launcher, runner)
    services.orchestrator = orchestrator
    chat = ChatAgent(services, launcher, projects_dir=config.projects_dir)

    backup = None
    if config.restic_repository:
        backup = BackupService(
            services,
            db_path=config.db_path,
            backup_dir=config.backup_dir,
            system_env=config.base_dir / ".env",
            restic_repository=config.restic_repository,
            restic_password_file=config.restic_password_file,
        ).run
    scheduler = DayforgeScheduler(
        services, runner, sessions_log_dir=config.sessions_log_dir, backup=backup
    )

    app = AppContext(
        services=services,
        orchestrator=orchestrator,
        runner=runner,
        chat=chat,
        kv=kv,
        scheduler=scheduler,
    )
    dispatcher = create_dispatcher(config.owner_telegram_id, app, SqliteStorage(kv))
    mcp_server = create_server(
        build_app(services, host=config.mcp_host), host=config.mcp_host, port=config.mcp_port
    )

    mcp_task = asyncio.create_task(mcp_server.serve(), name="mcp-server")
    try:
        # Агенты подключаются к MCP — он должен подняться раньше раннера.
        while not mcp_server.started and not mcp_task.done():  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        ttl_hours = await services.settings.get("pending_action_ttl_hours")
        services.actions.set_ttl(timedelta(hours=ttl_hours))
        await runner.recover()
        await orchestrator.recover()
        runner.start()
        await scheduler.start()
        with contextlib.suppress(Exception):
            await set_commands(bot)
        log.info("Dayforge started: MCP on %s:%s", config.mcp_host, config.mcp_port)
        # start_polling сам обрабатывает SIGINT/SIGTERM и возвращает управление.
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown()
        await orchestrator.stop()
        await runner.stop()
        mcp_server.should_exit = True
        await mcp_task
        await bot.session.close()
        if provider is not None:
            await provider.aclose()
        await engine.dispose()
        log.info("Dayforge stopped")
