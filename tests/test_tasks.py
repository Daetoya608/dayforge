from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import (
    AdhocWhen,
    DayStatus,
    DayTrigger,
    PauseReason,
    QuestionKind,
    TaskOrigin,
    TaskStatus,
)
from dayforge.core.tasks import TaskError, TaskService
from dayforge.db.models import DayExecution, Project, Question, Task

SessionFactory = async_sessionmaker[AsyncSession]
TODAY = date(2026, 11, 2)


@pytest.fixture
def tasks(session_factory: SessionFactory) -> TaskService:
    return TaskService(session_factory)


@pytest.fixture
async def project(session_factory: SessionFactory) -> Project:
    """Проект на 2 дня: T1, T2 в дне 1, T3 в дне 2; ни один день не начат."""
    from tests.test_models import make_project

    async with session_factory() as session, session.begin():
        project = make_project(planned_days=2)
        session.add(project)
        await session.flush()
        for key, day, position in [("T1", 1, 0), ("T2", 1, 1), ("T3", 2, 0)]:
            session.add(
                Task(
                    project_id=project.id,
                    key=key,
                    origin=TaskOrigin.PLAN,
                    day_number=day,
                    position=position,
                    title=key,
                    description=key,
                    depends_on=["T1"] if key == "T2" else [],
                )
            )
    return project


async def by_key(session_factory: SessionFactory, project: Project) -> dict[str, Task]:
    async with session_factory() as session:
        rows = await session.scalars(select(Task).where(Task.project_id == project.id))
        return {t.key: t for t in rows}


async def test_start_next_day_moves_pointer(
    tasks: TaskService, project: Project, session_factory: SessionFactory
) -> None:
    day, day_tasks = await tasks.start_next_day(
        project.id, trigger=DayTrigger.SCHEDULED, today=TODAY
    ) or (0, [])
    assert day == 1
    assert [t.key for t in day_tasks] == ["T1", "T2"]

    assert (await tasks.start_next_day(project.id, trigger=DayTrigger.NEXT, today=TODAY))[0] == 2  # type: ignore[index]
    assert await tasks.start_next_day(project.id, trigger=DayTrigger.NEXT, today=TODAY) is None

    async with session_factory() as session:
        days = list(await session.scalars(select(DayExecution).order_by(DayExecution.day_number)))
    assert [(d.day_number, d.trigger) for d in days] == [
        (1, DayTrigger.SCHEDULED),
        (2, DayTrigger.NEXT),
    ]


async def test_task_lifecycle(
    tasks: TaskService, project: Project, session_factory: SessionFactory
) -> None:
    t1 = (await by_key(session_factory, project))["T1"]

    running = await tasks.mark_running(t1.id, branch="feature/T1-x")
    assert (running.status, running.attempts, running.branch) == (
        TaskStatus.RUNNING,
        1,
        "feature/T1-x",
    )
    await tasks.mark_verifying(t1.id, summary="сделано", decisions=["выбрал X"])
    await tasks.back_to_running(t1.id)
    await tasks.mark_verifying(t1.id, summary="сделано 2", decisions=[])
    await tasks.mark_done(t1.id, merge_sha="abc")

    done = (await by_key(session_factory, project))["T1"]
    assert (done.status, done.summary, done.merge_sha) == (TaskStatus.DONE, "сделано 2", "abc")
    assert done.completed_at is not None

    with pytest.raises(TaskError):
        await tasks.mark_verifying(t1.id, summary="", decisions=[])


async def test_carry_over_order(
    tasks: TaskService, project: Project, session_factory: SessionFactory
) -> None:
    await tasks.start_next_day(project.id, trigger=DayTrigger.SCHEDULED, today=TODAY)
    keys = await by_key(session_factory, project)
    await tasks.pause(keys["T1"].id, PauseReason.LIMIT)
    adhoc = await tasks.add_adhoc(project.id, title="Опечатка", description="README")

    carry = await tasks.carry_over(project.id)

    # Разовая, затем пауза, затем невыполненное из начатого дня; T3 (день 2) не начат.
    assert [t.key for t in carry] == [adhoc.key, "T1", "T2"]
    assert adhoc.key == "A1"
    assert (await tasks.add_adhoc(project.id, title="b", description="b")).key == "A2"


async def test_paused_on_open_question_is_not_carried(
    tasks: TaskService, project: Project, session_factory: SessionFactory
) -> None:
    keys = await by_key(session_factory, project)
    await tasks.pause(keys["T3"].id, PauseReason.QUESTION)
    async with session_factory() as session, session.begin():
        question = Question(
            project_id=project.id, task_id=keys["T3"].id, kind=QuestionKind.QUESTION, payload={}
        )
        session.add(question)

    assert [t.key for t in await tasks.carry_over(project.id)] == []

    from dayforge.core.enums import QuestionStatus

    async with session_factory() as session, session.begin():
        stored = await session.get(Question, question.id)
        assert stored is not None
        stored.status = QuestionStatus.ANSWERED

    assert [t.key for t in await tasks.carry_over(project.id)] == ["T3"]


async def test_days_close_and_project_finishes(
    tasks: TaskService, project: Project, session_factory: SessionFactory
) -> None:
    keys = await by_key(session_factory, project)
    await tasks.start_next_day(project.id, trigger=DayTrigger.SCHEDULED, today=TODAY)
    for key in ("T1", "T2"):
        await tasks.mark_done(keys[key].id, merge_sha=key)
    await tasks.close_finished_days(project.id)
    assert not await tasks.is_project_finished(project.id)

    await tasks.start_next_day(project.id, trigger=DayTrigger.SCHEDULED, today=TODAY)
    await tasks.mark_done(keys["T3"].id, merge_sha="t3")
    await tasks.close_finished_days(project.id)

    async with session_factory() as session:
        statuses = list(await session.scalars(select(DayExecution.status)))
    assert statuses == [DayStatus.DONE, DayStatus.DONE]
    assert await tasks.is_project_finished(project.id)

    await tasks.add_adhoc(project.id, title="ещё", description="ещё", when=AdhocWhen.NOW)
    assert not await tasks.is_project_finished(project.id)


async def test_dependencies_and_failed_tasks(
    tasks: TaskService, project: Project, session_factory: SessionFactory
) -> None:
    keys = await by_key(session_factory, project)
    assert await tasks.depends_unfinished(keys["T2"]) == ["T1"]
    await tasks.mark_done(keys["T1"].id, merge_sha="x")
    assert await tasks.depends_unfinished(keys["T2"]) == []

    await tasks.fail(keys["T3"].id, reason="нет API")
    with pytest.raises(TaskError):
        await tasks.set_status(keys["T3"].id, TaskStatus.DONE)
    restored = await tasks.set_status(keys["T3"].id, TaskStatus.PENDING)
    assert restored.status is TaskStatus.PENDING
