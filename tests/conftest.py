from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from dayforge.db import models  # noqa: F401 — регистрирует таблицы
from dayforge.db.base import Base
from dayforge.db.models import Project
from dayforge.db.session import create_engine, create_session_factory


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def active_project(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> "Project":
    """Утверждённый проект с рабочей директорией и задачей T1 в работе."""
    from dayforge.core.enums import ProjectStatus, TaskOrigin, TaskStatus
    from dayforge.core.projects import NewProject, ProjectService
    from dayforge.core.settings import SettingsService
    from dayforge.db.base import utcnow
    from dayforge.db.models import Project, Task

    service = ProjectService(session_factory, SettingsService(session_factory), tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    created = await service.create(
        NewProject(name="Todo", slug="todo-bot", idea="Бот учёта расходов", planned_days=3)
    )
    async with session_factory() as session, session.begin():
        project = await session.get(Project, created.id)
        assert project is not None
        project.status = ProjectStatus.ACTIVE
        project.activated_at = utcnow()
        project.current_day = 1
        session.add(
            Task(
                project_id=project.id,
                key="T1",
                origin=TaskOrigin.PLAN,
                day_number=1,
                title="Платежи",
                description="Подключить приём платежей",
                status=TaskStatus.RUNNING,
            )
        )
    return project
