"""Ежедневный бэкап БД и .env в restic (docs/operations.md#бэкапы)."""

import asyncio
import datetime
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from dayforge.core.enums import ProjectStatus
from dayforge.core.events import BackupFailed
from dayforge.services import Services

log = logging.getLogger(__name__)

RETENTION = ["--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6"]
SUNDAY = 6


class BackupError(Exception):
    pass


class BackupService:
    def __init__(
        self,
        services: Services,
        *,
        db_path: Path,
        backup_dir: Path,
        system_env: Path | None,
        restic_repository: str,
        restic_password_file: Path | None,
        restic_binary: str = "restic",
    ) -> None:
        self._s = services
        self._db_path = db_path
        self._backup_dir = backup_dir
        self._system_env = system_env
        self._restic_env = {
            "RESTIC_REPOSITORY": restic_repository,
            **({"RESTIC_PASSWORD_FILE": str(restic_password_file)} if restic_password_file else {}),
        }
        self._restic = restic_binary

    async def run(self, today: datetime.date | None = None) -> None:
        """Полный цикл; ошибка любого шага — уведомление в Telegram."""
        try:
            await self._push_projects()
            await self._snapshot()
            await self._restic_cmd(
                "backup", str(self._backup_dir), *self._extra_paths(), "--tag", "daily"
            )
            await self._restic_cmd("forget", *RETENTION, "--prune")
            if (today or datetime.date.today()).weekday() == SUNDAY:
                await self._restic_cmd("check")
        except Exception as exc:
            log.exception("backup failed")
            await self._s.notifier.send(BackupFailed(error=str(exc)[:1000]))
        finally:
            await asyncio.to_thread(shutil.rmtree, self._backup_dir, True)

    async def _push_projects(self) -> None:
        """Повторный push всех проектов — на случай упавших push в течение дня."""
        if self._s.repos.provider is None:
            return
        for project in await self._s.projects.list_projects(include_archived=True):
            if project.remote_url is None or project.status is ProjectStatus.DRAFT:
                continue
            try:
                await self._s.repos.publish(project.id)
            except Exception as exc:
                log.warning("push before backup failed for %s: %s", project.slug, exc)

    async def _snapshot(self) -> None:
        env_dir = self._backup_dir / "env"
        await asyncio.to_thread(env_dir.mkdir, parents=True, exist_ok=True)
        # Консистентный снапшот работающей БД — тот же механизм, что `sqlite3 .backup`.
        await asyncio.to_thread(
            _sqlite_backup, self._db_path, self._backup_dir / "dayforge.sqlite3"
        )
        for project in await self._s.projects.list_projects(include_archived=True):
            source = Path(project.repo_path) / ".env"
            if source.exists():
                target = env_dir / f"{project.slug}.env"
                await asyncio.to_thread(shutil.copy2, source, target)
                os.chmod(target, 0o600)

    def _extra_paths(self) -> list[str]:
        return [str(self._system_env)] if self._system_env and self._system_env.exists() else []

    async def _restic_cmd(self, *args: str) -> None:
        process = await asyncio.create_subprocess_exec(
            self._restic,
            *args,
            env={**os.environ, **self._restic_env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            tail = output.decode(errors="replace").strip()[-800:]
            raise BackupError(f"restic {args[0]} завершился с кодом {process.returncode}: {tail}")


def _sqlite_backup(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    os.chmod(target, 0o600)
