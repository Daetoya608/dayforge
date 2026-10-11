from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar import Forecast
from dayforge.core.calendar_service import CalendarService
from dayforge.core.enums import CalendarDateKind, RunMode, RunTrigger, TaskOrigin, TaskStatus
from dayforge.core.projects import NewProject, ProjectService
from dayforge.core.settings import SettingsService
from dayforge.db.models import Project, Run, Task

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
SUNDAY = date(2026, 11, 1)


@pytest.fixture
def calendar(session_factory: async_sessionmaker[AsyncSession]) -> CalendarService:
    return CalendarService(session_factory, SettingsService(session_factory))


@pytest.fixture
async def project(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> Project:
    service = ProjectService(session_factory, SettingsService(session_factory), tmp_path)
    created = await service.create(
        NewProject(name="Todo", slug="todo-bot", idea="Бот", planned_days=5)
    )
    # План утверждён в воскресенье 1 ноября в 12:00 по Москве.
    async with session_factory() as session, session.begin():
        stored = await session.get(Project, created.id)
        assert stored is not None
        stored.activated_at = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    return stored


async def test_docs_example_forecast(calendar: CalendarService, project: Project) -> None:
    await calendar.set_weekdays(project.id, {MON, WED, FRI})
    await calendar.set_dates(None, [date(2026, 11, 4)], CalendarDateKind.EXCLUDE)

    result = await calendar.forecast(project, today=SUNDAY)

    assert result == Forecast(date(2026, 11, 13))


async def test_set_weekdays_replaces_template(calendar: CalendarService, project: Project) -> None:
    await calendar.set_weekdays(project.id, {MON, TUE})
    await calendar.set_weekdays(project.id, {SAT})

    rules = await calendar.project_calendar(project, today=SUNDAY)

    assert rules.weekdays == {SAT}
    with pytest.raises(ValueError):
        await calendar.set_weekdays(project.id, {7})


async def test_dates_override_each_other_and_can_be_cleared(
    calendar: CalendarService, project: Project
) -> None:
    day = date(2026, 11, 7)
    await calendar.set_dates(project.id, [day], CalendarDateKind.INCLUDE)
    await calendar.set_dates(project.id, [day], CalendarDateKind.EXCLUDE)

    rules = await calendar.project_calendar(project, today=SUNDAY)
    assert rules.include == frozenset()
    assert rules.exclude == {day}

    await calendar.clear_dates(project.id, [day])
    rules = await calendar.project_calendar(project, today=SUNDAY)
    assert rules.exclude == frozenset()


async def test_day_off_for_all_projects_only_as_exclude(calendar: CalendarService) -> None:
    with pytest.raises(ValueError):
        await calendar.set_dates(None, [date(2026, 12, 31)], CalendarDateKind.INCLUDE)


async def test_used_slot_and_unfinished_tasks_shift_forecast(
    calendar: CalendarService,
    project: Project,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await calendar.set_weekdays(project.id, {MON, WED, FRI})
    monday = date(2026, 11, 2)
    async with session_factory() as session, session.begin():
        stored = await session.get(Project, project.id)
        assert stored is not None
        stored.current_day = 5  # все дни начаты, остались незавершённые задачи
        session.add(
            Task(
                project_id=project.id,
                key="T9",
                origin=TaskOrigin.PLAN,
                day_number=5,
                title="Хвост",
                description="Незавершённая задача",
                status=TaskStatus.PAUSED,
            )
        )
        session.add(
            Run(
                project_id=project.id,
                batch_id="b1",
                mode=RunMode.SCHEDULED,
                trigger=RunTrigger.SCHEDULER,
                slot_date=monday,
            )
        )
        project = stored

    # Сегодняшний слот уже использован — незавершённое доделается в среду.
    assert await calendar.forecast(project, today=monday) == Forecast(date(2026, 11, 4))


async def test_forecast_before_approval_uses_proposed_day_count(
    calendar: CalendarService, project: Project, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        stored = await session.get(Project, project.id)
        assert stored is not None
        stored.activated_at = None
    await calendar.set_weekdays(project.id, {MON, WED, FRI})

    result = await calendar.forecast(stored, today=SUNDAY, planned_days=2)

    assert result == Forecast(date(2026, 11, 4))


async def test_today_uses_configured_timezone(calendar: CalendarService) -> None:
    late_evening_utc = datetime(2026, 11, 1, 22, 30, tzinfo=UTC)  # 01:30 по Москве

    assert await calendar.today(late_evening_utc) == date(2026, 11, 2)
