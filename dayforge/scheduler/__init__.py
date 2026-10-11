"""Задания по времени: ежедневный запуск, бэкап, истечение подтверждений, чистка логов."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from dayforge.core.events import PendingActionExpired
from dayforge.runner.runner import Runner
from dayforge.services import Services

log = logging.getLogger(__name__)

SESSION_LOG_RETENTION = timedelta(days=30)
# Пропущенный во время простоя запуск задним числом не выполняется (docs/architecture.md).
MISFIRE_GRACE_SECONDS = 300
BACKUP_WAIT_POLL_SECONDS = 60


def _cron(hhmm: str, timezone: str) -> CronTrigger:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return CronTrigger(hour=hour, minute=minute, timezone=timezone)


class DayforgeScheduler:
    def __init__(
        self,
        services: Services,
        runner: Runner,
        *,
        sessions_log_dir: Path,
        backup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._s = services
        self._runner = runner
        self._logs_dir = sessions_log_dir
        self._backup = backup
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self) -> None:
        settings = await self._s.settings.get_all()
        tz = settings["timezone"]
        self._scheduler = AsyncIOScheduler(timezone=tz)
        common = {"misfire_grace_time": MISFIRE_GRACE_SECONDS, "coalesce": True, "max_instances": 1}
        self._scheduler.add_job(
            self.daily_run, _cron(settings["run_time"], tz), id="daily_run", **common
        )
        if self._backup is not None:
            self._scheduler.add_job(
                self.backup, _cron(settings["backup_time"], tz), id="backup", **common
            )
        self._scheduler.add_job(
            self.expire_actions, "interval", minutes=15, id="expire_actions", **common
        )
        self._scheduler.add_job(
            self.cleanup_logs, "interval", hours=24, id="cleanup_logs", **common
        )
        self._scheduler.start()

    async def reschedule(self) -> None:
        """После изменения времени запуска, бэкапа или часового пояса в /settings."""
        if self._scheduler is None:
            return
        settings = await self._s.settings.get_all()
        tz = settings["timezone"]
        self._scheduler.reschedule_job("daily_run", trigger=_cron(settings["run_time"], tz))
        if self._backup is not None:
            self._scheduler.reschedule_job("backup", trigger=_cron(settings["backup_time"], tz))

    def next_run_times(self) -> dict[str, str]:
        if self._scheduler is None:
            return {}
        return {
            job.id: job.next_run_time.isoformat()
            for job in self._scheduler.get_jobs()
            if job.next_run_time is not None
        }

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # --- задания ---

    async def daily_run(self) -> int:
        today = await self._s.calendar.today()
        runs = await self._s.runs.enqueue_scheduled(today)
        log.info("daily run %s: %d project(s) queued", today, len(runs))
        if runs:
            self._runner.kick()
        return len(runs)

    async def backup(self) -> None:
        """Бэкап — после окна запусков: ждём, пока раннер освободится."""
        assert self._backup is not None
        while self._runner.busy or await self._s.runs.queued():  # noqa: ASYNC110 — очередь в БД
            await asyncio.sleep(BACKUP_WAIT_POLL_SECONDS)
        await self._backup()

    async def expire_actions(self) -> None:
        for action in await self._s.actions.expire_due():
            await self._s.notifier.send(
                PendingActionExpired(action_id=action.id, tg_message_id=action.tg_message_id)
            )

    async def cleanup_logs(self) -> int:
        cutoff = time.time() - SESSION_LOG_RETENTION.total_seconds()
        return await asyncio.to_thread(_remove_older_than, self._logs_dir, cutoff)


def _remove_older_than(directory: Path, cutoff: float) -> int:
    removed = 0
    if not directory.exists():
        return 0
    for path in directory.glob("*.jsonl"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
