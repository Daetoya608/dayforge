"""Очередь запусков проектов (docs/run-lifecycle.md#очередь)."""

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar_service import CalendarService
from dayforge.core.enums import ProjectStatus, RunMode, RunStatus, RunTrigger
from dayforge.db.base import utcnow
from dayforge.db.models import Project, Run

QUEUEABLE = (ProjectStatus.ACTIVE, ProjectStatus.BLOCKED)


class RunService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], calendar: CalendarService
    ) -> None:
        self._session_factory = session_factory
        self._calendar = calendar

    async def enqueue(
        self,
        project_ids: Sequence[int],
        *,
        mode: RunMode,
        trigger: RunTrigger,
        slot_date: date | None = None,
    ) -> list[Run]:
        """Ставит проекты в очередь в порядке приоритета, одним batch.

        Правила склейки: next поглощает стоящий в очереди continue; continue при стоящем
        запуске проекта не добавляется; повторный next ставится вторым элементом.
        """
        batch_id = str(uuid.uuid4())
        created: list[Run] = []
        async with self._session_factory() as session, session.begin():
            projects = list(
                await session.scalars(
                    select(Project)
                    .where(Project.id.in_(project_ids), Project.status.in_(QUEUEABLE))
                    .order_by(Project.priority, Project.id)
                )
            )
            for project in projects:
                queued = list(
                    await session.scalars(
                        select(Run).where(
                            Run.project_id == project.id, Run.status == RunStatus.QUEUED
                        )
                    )
                )
                if mode is RunMode.CONTINUE and queued:
                    continue
                if mode is RunMode.NEXT:
                    pending_continue = next((r for r in queued if r.mode is RunMode.CONTINUE), None)
                    if pending_continue is not None:
                        pending_continue.mode = RunMode.NEXT
                        created.append(pending_continue)
                        continue
                run = Run(
                    project_id=project.id,
                    batch_id=batch_id,
                    mode=mode,
                    trigger=trigger,
                    slot_date=slot_date,
                )
                session.add(run)
                created.append(run)
        return created

    async def enqueue_scheduled(self, today: date) -> list[Run]:
        """Ежедневный запуск: проекты, у которых сегодня неиспользованный слот."""
        async with self._session_factory() as session:
            projects = list(
                await session.scalars(select(Project).where(Project.status.in_(QUEUEABLE)))
            )
            used = set(
                await session.scalars(
                    select(Run.project_id).where(
                        Run.mode == RunMode.SCHEDULED,
                        Run.slot_date == today,
                        Run.status != RunStatus.CANCELLED,
                    )
                )
            )
        due = [
            p.id
            for p in projects
            if p.id not in used
            and (await self._calendar.project_calendar(p, today=today)).is_slot(today)
        ]
        if not due:
            return []
        return await self.enqueue(
            due, mode=RunMode.SCHEDULED, trigger=RunTrigger.SCHEDULER, slot_date=today
        )

    async def next_queued(self) -> Run | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(Run).where(Run.status == RunStatus.QUEUED).order_by(Run.id).limit(1)
            )

    async def queued(self) -> list[Run]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(Run).where(Run.status == RunStatus.QUEUED).order_by(Run.id)
                )
            )

    async def start(self, run_id: int) -> Run:
        async with self._session_factory() as session, session.begin():
            run = await _run(session, run_id)
            run.status = RunStatus.RUNNING
            run.started_at = utcnow()
            return run

    async def finish(
        self,
        run_id: int,
        status: RunStatus,
        *,
        day_number: int | None = None,
        error: str | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await _run(session, run_id)
            run.status = status
            run.finished_at = utcnow()
            if day_number is not None:
                run.day_number = day_number
            if error is not None:
                run.error = error

    async def cancel_queued(self) -> list[Run]:
        """Лимит подписки: всё, что стоит в очереди, отменяется."""
        async with self._session_factory() as session, session.begin():
            runs = list(await session.scalars(select(Run).where(Run.status == RunStatus.QUEUED)))
            for run in runs:
                run.status = RunStatus.CANCELLED
                run.finished_at = utcnow()
            return runs

    async def history(self, project_id: int | None = None, limit: int = 20) -> list[Run]:
        query = select(Run).order_by(Run.id.desc()).limit(limit)
        if project_id is not None:
            query = query.where(Run.project_id == project_id)
        async with self._session_factory() as session:
            return list(await session.scalars(query))

    async def get(self, run_id: int) -> Run:
        async with self._session_factory() as session:
            return await _run(session, run_id)


async def _run(session: AsyncSession, run_id: int) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError(f"запуска {run_id} нет")
    return run
