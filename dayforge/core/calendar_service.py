"""Календарь в БД: шаблоны, даты, прогноз окончания по проекту (docs/calendar.md)."""

from collections.abc import Iterable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar import Forecast, ProjectCalendar, forecast
from dayforge.core.enums import CalendarDateKind, RunMode, RunStatus, TaskOrigin, TaskStatus
from dayforge.core.settings import SettingsService
from dayforge.db.base import utcnow
from dayforge.db.models import CalendarDate, CalendarWeekday, Project, Run, Task

# Задачи, которые ещё нужно доделать: дают «незавершённое» для прогноза.
UNFINISHED_STATUSES = (
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.VERIFYING,
    TaskStatus.PAUSED,
)


class CalendarService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: SettingsService
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def timezone(self) -> ZoneInfo:
        return ZoneInfo(await self._settings.get("timezone"))

    async def today(self, now: datetime | None = None) -> date:
        return (now or utcnow()).astimezone(await self.timezone()).date()

    async def set_weekdays(self, project_id: int, weekdays: Iterable[int]) -> None:
        days = set(weekdays)
        if not days <= set(range(7)):
            raise ValueError(f"дни недели — числа 0–6, получено {sorted(days)}")
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(CalendarWeekday).where(CalendarWeekday.project_id == project_id)
            )
            session.add_all(CalendarWeekday(project_id=project_id, weekday=d) for d in days)

    async def set_dates(
        self, project_id: int | None, dates: Iterable[date], kind: CalendarDateKind
    ) -> None:
        """include/exclude для проекта или, при project_id=None, общий выходной (только exclude)."""
        if project_id is None and kind is not CalendarDateKind.EXCLUDE:
            raise ValueError("для всех проектов можно задать только выходной (exclude)")
        dates = set(dates)
        async with self._session_factory() as session, session.begin():
            await _delete_dates(session, project_id, dates)
            session.add_all(
                CalendarDate(project_id=project_id, date=d, kind=kind) for d in sorted(dates)
            )

    async def clear_dates(self, project_id: int | None, dates: Iterable[date]) -> None:
        async with self._session_factory() as session, session.begin():
            await _delete_dates(session, project_id, set(dates))

    async def project_calendar(self, project: Project, *, today: date) -> ProjectCalendar:
        """Правила проекта; до утверждения плана слоты считаются с сегодняшнего дня."""
        tz = await self.timezone()
        starts_on = project.activated_at.astimezone(tz).date() if project.activated_at else today
        async with self._session_factory() as session:
            weekdays = await session.scalars(
                select(CalendarWeekday.weekday).where(CalendarWeekday.project_id == project.id)
            )
            rows = await session.scalars(
                select(CalendarDate).where(
                    or_(CalendarDate.project_id == project.id, CalendarDate.project_id.is_(None))
                )
            )
            dates = list(rows)
        return ProjectCalendar(
            starts_on=starts_on,
            weekdays=frozenset(weekdays),
            include=_dates(dates, CalendarDateKind.INCLUDE, project=True),
            exclude=_dates(dates, CalendarDateKind.EXCLUDE, project=True),
            days_off=_dates(dates, CalendarDateKind.EXCLUDE, project=False),
        )

    async def forecast(
        self, project: Project, *, today: date, planned_days: int | None = None
    ) -> Forecast:
        """Прогноз окончания. planned_days — для плана на согласовании, ещё не утверждённого."""
        calendar = await self.project_calendar(project, today=today)
        async with self._session_factory() as session:
            today_slot_used = await session.scalar(
                select(
                    exists().where(
                        Run.project_id == project.id,
                        Run.mode == RunMode.SCHEDULED,
                        Run.slot_date == today,
                        Run.status != RunStatus.CANCELLED,
                    )
                )
            )
            has_unfinished = await session.scalar(
                select(
                    exists().where(
                        Task.project_id == project.id,
                        Task.status.in_(UNFINISHED_STATUSES),
                        or_(
                            Task.origin == TaskOrigin.ADHOC,
                            Task.day_number <= project.current_day,
                        ),
                    )
                )
            )
        return forecast(
            calendar,
            today=today,
            today_slot_used=bool(today_slot_used),
            planned_days=planned_days if planned_days is not None else project.planned_days,
            current_day=project.current_day,
            has_unfinished=bool(has_unfinished),
        )


async def _delete_dates(session: AsyncSession, project_id: int | None, dates: set[date]) -> None:
    owner = (
        CalendarDate.project_id.is_(None)
        if project_id is None
        else CalendarDate.project_id == project_id
    )
    await session.execute(delete(CalendarDate).where(owner, CalendarDate.date.in_(dates)))


def _dates(rows: list[CalendarDate], kind: CalendarDateKind, *, project: bool) -> frozenset[date]:
    return frozenset(
        row.date for row in rows if row.kind is kind and (row.project_id is not None) == project
    )
