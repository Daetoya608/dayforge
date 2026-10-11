from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar_service import CalendarService
from dayforge.core.enums import ProjectStatus, RunMode, RunStatus, RunTrigger
from dayforge.core.projects import NewProject, ProjectService
from dayforge.core.runs import RunService
from dayforge.core.settings import SettingsService
from dayforge.db.base import utcnow
from dayforge.db.models import Project

SessionFactory = async_sessionmaker[AsyncSession]
MONDAY = date(2026, 11, 2)


@pytest.fixture
def calendar(session_factory: SessionFactory) -> CalendarService:
    return CalendarService(session_factory, SettingsService(session_factory))


@pytest.fixture
def runs(session_factory: SessionFactory, calendar: CalendarService) -> RunService:
    return RunService(session_factory, calendar)


@pytest.fixture
async def projects(session_factory: SessionFactory, tmp_path: Path) -> list[Project]:
    service = ProjectService(session_factory, SettingsService(session_factory), tmp_path)
    created = [
        await service.create(NewProject(name=s, slug=s, idea="x", planned_days=3))
        for s in ("aa", "bb", "cc")
    ]
    async with session_factory() as session, session.begin():
        for project in created:
            stored = await session.get(Project, project.id)
            assert stored is not None
            stored.status = ProjectStatus.ACTIVE
            stored.activated_at = utcnow().replace(year=2026, month=11, day=1)
    return created


async def test_enqueue_orders_by_priority_in_one_batch(
    runs: RunService, projects: list[Project], session_factory: SessionFactory
) -> None:
    aa, _bb, cc = projects
    await ProjectService(session_factory, SettingsService(session_factory), Path()).set_priority(
        ["cc", "aa", "bb"]
    )

    created = await runs.enqueue([aa.id, cc.id], mode=RunMode.NEXT, trigger=RunTrigger.BOT)

    assert [r.project_id for r in created] == [cc.id, aa.id]
    assert len({r.batch_id for r in created}) == 1
    assert [r.project_id for r in await runs.queued()] == [cc.id, aa.id]


async def test_merge_rules(runs: RunService, projects: list[Project]) -> None:
    aa = projects[0]

    await runs.enqueue([aa.id], mode=RunMode.CONTINUE, trigger=RunTrigger.BOT)
    await runs.enqueue([aa.id], mode=RunMode.CONTINUE, trigger=RunTrigger.BOT)
    assert [r.mode for r in await runs.queued()] == [RunMode.CONTINUE]

    await runs.enqueue([aa.id], mode=RunMode.NEXT, trigger=RunTrigger.BOT)
    assert [r.mode for r in await runs.queued()] == [RunMode.NEXT]

    await runs.enqueue([aa.id], mode=RunMode.NEXT, trigger=RunTrigger.BOT)
    await runs.enqueue([aa.id], mode=RunMode.CONTINUE, trigger=RunTrigger.BOT)
    assert [r.mode for r in await runs.queued()] == [RunMode.NEXT, RunMode.NEXT]


async def test_paused_projects_are_not_queued(
    runs: RunService, projects: list[Project], session_factory: SessionFactory
) -> None:
    async with session_factory() as session, session.begin():
        stored = await session.get(Project, projects[0].id)
        assert stored is not None
        stored.status = ProjectStatus.PAUSED

    assert await runs.enqueue([projects[0].id], mode=RunMode.NEXT, trigger=RunTrigger.BOT) == []


async def test_scheduled_run_uses_calendar_once_per_slot(
    runs: RunService, calendar: CalendarService, projects: list[Project]
) -> None:
    aa, bb, _cc = projects
    await calendar.set_weekdays(aa.id, {0})  # понедельник
    await calendar.set_weekdays(bb.id, {1})

    first = await runs.enqueue_scheduled(MONDAY)
    assert [(r.project_id, r.slot_date, r.mode) for r in first] == [
        (aa.id, MONDAY, RunMode.SCHEDULED)
    ]

    await runs.start(first[0].id)
    await runs.finish(first[0].id, RunStatus.FINISHED, day_number=1)
    assert await runs.enqueue_scheduled(MONDAY) == []  # слот уже использован


async def test_cancel_queued_and_history(runs: RunService, projects: list[Project]) -> None:
    await runs.enqueue([p.id for p in projects], mode=RunMode.NEXT, trigger=RunTrigger.BOT)
    first = await runs.next_queued()
    assert first is not None
    await runs.start(first.id)

    cancelled = await runs.cancel_queued()

    assert len(cancelled) == 2
    assert await runs.next_queued() is None
    statuses = [r.status for r in await runs.history()]
    assert statuses == [RunStatus.CANCELLED, RunStatus.CANCELLED, RunStatus.RUNNING]
