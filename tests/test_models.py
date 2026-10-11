from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import ProjectStatus, TaskOrigin, TaskStatus
from dayforge.db.models import Project, Task


def make_project(**overrides: object) -> Project:
    fields: dict[str, object] = {
        "slug": "todo-bot",
        "name": "Todo bot",
        "badge": "T",
        "idea": "Бот со списком дел",
        "priority": 1,
        "planned_days": 10,
        "planner_agent": {"agent": "claude_code", "model": "opus", "effort": "high"},
        "executor_agent": {"agent": "claude_code", "model": "opus", "effort": "medium"},
        "repo_path": "/srv/dayforge/projects/todo-bot",
    }
    return Project(**(fields | overrides))


async def test_project_and_task_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        project = make_project()
        session.add(project)
        await session.flush()
        session.add(
            Task(
                project_id=project.id,
                key="T1",
                origin=TaskOrigin.PLAN,
                day_number=1,
                title="Каркас",
                description="uv-проект и pytest",
                acceptance_criteria=["pytest проходит"],
            )
        )

    async with session_factory() as session:
        stored = await session.get(Project, project.id)
        assert stored is not None
        assert stored.status is ProjectStatus.DRAFT
        assert stored.current_day == 0
        assert stored.created_at.tzinfo is UTC

        task = await session.scalar(select(Task).where(Task.project_id == project.id))
        assert task is not None
        assert task.key == "T1"
        assert task.status is TaskStatus.PENDING
        assert task.acceptance_criteria == ["pytest проходит"]


async def test_datetimes_are_stored_as_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    moscow = timezone(timedelta(hours=3))
    activated = datetime(2026, 10, 11, 12, 0, tzinfo=moscow)

    async with session_factory() as session, session.begin():
        project = make_project(activated_at=activated)
        session.add(project)

    async with session_factory() as session:
        stored = await session.get(Project, project.id)
        assert stored is not None
        assert stored.activated_at == activated
        assert stored.activated_at.tzinfo is UTC
        assert stored.activated_at.hour == 9


async def test_naive_datetime_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(StatementError, match="naive datetime"):
        async with session_factory() as session, session.begin():
            session.add(make_project(activated_at=datetime(2026, 10, 11, 12, 0)))


async def test_task_defaults(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        project = make_project()
        session.add(project)
        await session.flush()
        task = Task(
            project_id=project.id,
            key="A1",
            origin=TaskOrigin.ADHOC,
            title="Опечатка",
            description="Исправить опечатку в README",
        )
        session.add(task)

    assert task.status is TaskStatus.PENDING
    assert task.attempts == 0
    assert task.decisions == []
