import copy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar_service import CalendarService
from dayforge.core.enums import (
    CommitKind,
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from dayforge.core.plan_format import PlanValidationError
from dayforge.core.plans import PlanError, PlanService
from dayforge.core.projects import NewProject, ProjectService
from dayforge.core.settings import SettingsService
from dayforge.db.models import Commit, Milestone, Plan, Project, Task
from dayforge.vcs.git import Git
from tests.test_plan_format import make_plan, task

MON, TUE, WED, THU, FRI = range(5)

SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture
def calendar(session_factory: SessionFactory) -> CalendarService:
    return CalendarService(session_factory, SettingsService(session_factory))


@pytest.fixture
def plans(session_factory: SessionFactory, calendar: CalendarService) -> PlanService:
    return PlanService(session_factory, calendar)


@pytest.fixture
async def project(
    session_factory: SessionFactory, calendar: CalendarService, tmp_path: Path
) -> Project:
    service = ProjectService(session_factory, SettingsService(session_factory), tmp_path)
    created = await service.create(
        NewProject(name="Todo", slug="todo-bot", idea="Бот", planned_days=3)
    )
    await calendar.set_weekdays(created.id, {MON, TUE, WED, THU, FRI})
    return created


async def load(session_factory: SessionFactory, model: Any, **where: Any) -> list[Any]:
    async with session_factory() as session:
        query = select(model).filter_by(**where)
        return list(await session.scalars(query))


async def reload(session_factory: SessionFactory, project: Project) -> Project:
    (stored,) = await load(session_factory, Project, id=project.id)
    return stored


async def test_first_plan_goes_to_review(
    plans: PlanService, project: Project, session_factory: SessionFactory
) -> None:
    result = await plans.submit(project.id, make_plan())

    assert result.version == 1
    assert result.diff is None
    assert result.warnings == []
    assert result.forecast.end_date is not None
    assert (await reload(session_factory, project)).status is ProjectStatus.PLAN_REVIEW


async def test_invalid_plan_is_not_stored(
    plans: PlanService, project: Project, session_factory: SessionFactory
) -> None:
    raw = make_plan()
    raw["days"][0]["day"] = 7

    with pytest.raises(PlanValidationError):
        await plans.submit(project.id, raw)

    assert await load(session_factory, Plan) == []
    assert (await reload(session_factory, project)).status is ProjectStatus.DRAFT


async def test_new_proposal_supersedes_previous_one(
    plans: PlanService, project: Project, session_factory: SessionFactory
) -> None:
    first = await plans.submit(project.id, make_plan())
    second = await plans.submit(project.id, make_plan())

    assert second.version == 2
    with pytest.raises(PlanError, match="уже superseded"):
        await plans.approve(first.plan_id)


async def test_approve_creates_tasks_and_initializes_repository(
    plans: PlanService, project: Project, session_factory: SessionFactory
) -> None:
    result = await plans.submit(project.id, make_plan())

    await plans.approve(result.plan_id)

    stored = await reload(session_factory, project)
    assert stored.status is ProjectStatus.ACTIVE
    assert stored.activated_at is not None
    assert stored.planned_days == 3
    assert stored.test_command == "uv run pytest -q"
    assert stored.day_goals == {"1": "Каркас", "2": "Функции", "3": "Отчёты"}

    tasks = sorted(await load(session_factory, Task), key=lambda t: t.key)
    assert [(t.key, t.day_number, t.position, t.status) for t in tasks] == [
        ("T1", 1, 0, TaskStatus.PENDING),
        ("T2", 1, 1, TaskStatus.PENDING),
        ("T3", 2, 0, TaskStatus.PENDING),
        ("T4", 3, 0, TaskStatus.PENDING),
    ]
    assert tasks[1].depends_on == ["T1"]
    assert [m.key for m in await load(session_factory, Milestone)] == ["M1", "M2"]

    git = Git(Path(stored.repo_path))
    assert await git.run("log", "--format=%s", "main") == "chore: project init (plan v1)"
    assert await git.rev_parse("dev") == await git.rev_parse("main")
    assert await git.current_branch() == "dev"
    assert await git.is_clean()
    files = await git.run("ls-files")
    assert "docs/PLAN.md" in files and "docs/JOURNAL.md" in files and "AGENTS.md" in files

    (commit,) = await load(session_factory, Commit)
    assert commit.kind is CommitKind.SYSTEM
    assert commit.sha == await git.rev_parse("main")

    plan_md = (Path(stored.repo_path) / "docs" / "PLAN.md").read_text()
    assert plan_md.startswith("# План: Todo\n")
    assert "## День 1 — Каркас" in plan_md


async def test_failed_repository_init_rolls_back_approval(
    plans: PlanService,
    project: Project,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await plans.submit(project.id, make_plan())

    async def broken_commit(self: Git, message: str) -> str:
        raise RuntimeError("disk full")

    monkeypatch.setattr(Git, "commit_all", broken_commit)
    with pytest.raises(RuntimeError):
        await plans.approve(result.plan_id)

    assert (await reload(session_factory, project)).status is ProjectStatus.PLAN_REVIEW
    assert await load(session_factory, Task) == []
    monkeypatch.undo()
    await plans.approve(result.plan_id)
    assert (await reload(session_factory, project)).status is ProjectStatus.ACTIVE


async def test_reject_returns_project_to_planning(
    plans: PlanService, project: Project, session_factory: SessionFactory
) -> None:
    result = await plans.submit(project.id, make_plan())

    await plans.reject(result.plan_id)

    (plan,) = await load(session_factory, Plan)
    assert plan.status is PlanStatus.REJECTED
    assert (await reload(session_factory, project)).status is ProjectStatus.PLANNING
    with pytest.raises(PlanError):
        await plans.approve(result.plan_id)


async def test_archived_project_does_not_accept_plans(
    plans: PlanService, project: Project, session_factory: SessionFactory
) -> None:
    async with session_factory() as session, session.begin():
        stored = await session.get(Project, project.id)
        assert stored is not None
        stored.status = ProjectStatus.ARCHIVED

    with pytest.raises(PlanError, match="archived"):
        await plans.submit(project.id, make_plan())


class TestReplan:
    async def start(self, plans: PlanService, project: Project, sf: SessionFactory) -> None:
        """Утверждённый план; день 1 выполнен, день 2 начат, T3 на паузе."""
        await plans.approve((await plans.submit(project.id, make_plan())).plan_id)
        async with sf() as session, session.begin():
            stored = await session.get(Project, project.id)
            assert stored is not None
            stored.current_day = 2
            for t in await session.scalars(select(Task).where(Task.project_id == project.id)):
                t.status = {"T1": TaskStatus.DONE, "T2": TaskStatus.DONE}.get(t.key, t.status)
                if t.key == "T3":
                    t.status = TaskStatus.PAUSED

    def replan(self) -> dict[str, Any]:
        raw = copy.deepcopy(make_plan())
        raw["change_summary"] = "Экспорт вместо отчётов"
        raw["days"] = [
            {"day": 2, "goal": "Функции", "tasks": [task("T3", depends_on=["T2"])]},
            {"day": 3, "goal": "Экспорт", "tasks": [task("T5", milestone="M2")]},
        ]
        raw["days"][0]["tasks"][0]["description"] = "Уточнённое описание"
        return raw

    async def test_replan_diff_and_sync(
        self, plans: PlanService, project: Project, session_factory: SessionFactory
    ) -> None:
        await self.start(plans, project, session_factory)

        result = await plans.submit(project.id, self.replan())
        assert result.version == 2
        assert result.diff == [
            "~ T3 Задача T3: описание",
            "+ T5 Задача T5 (день 3)",
            "− T4 Задача T4 (был в дне 3)",
        ]
        stored = await reload(session_factory, project)
        assert stored.status is ProjectStatus.ACTIVE  # перепланирование статус не меняет

        await plans.approve(result.plan_id)

        tasks = {t.key: t for t in await load(session_factory, Task)}
        assert tasks["T1"].status is TaskStatus.DONE
        assert tasks["T3"].status is TaskStatus.PAUSED
        assert tasks["T3"].description == "Уточнённое описание"
        assert tasks["T4"].status is TaskStatus.CANCELLED
        assert tasks["T5"].status is TaskStatus.PENDING
        stored = await reload(session_factory, project)
        assert stored.day_goals == {"1": "Каркас", "2": "Функции", "3": "Экспорт"}
        statuses = {p.version: p.status for p in await load(session_factory, Plan)}
        assert statuses == {1: PlanStatus.SUPERSEDED, 2: PlanStatus.APPROVED}

        # Перепланирование не коммитит: PLAN.md в dev обновляет раннер.
        git = Git(Path(stored.repo_path))
        assert await git.run("rev-list", "--count", "--all") == "1"

        plan_md = await plans.render(project.id)
        assert "## День 1 — Каркас ✅" in plan_md
        assert "**T4**" not in plan_md
        assert "## День 3 — Экспорт" in plan_md

    async def test_replan_requires_starting_from_open_day(
        self, plans: PlanService, project: Project, session_factory: SessionFactory
    ) -> None:
        await self.start(plans, project, session_factory)
        raw = self.replan()
        for number, day in enumerate(raw["days"], start=3):
            day["day"] = number

        with pytest.raises(PlanValidationError, match="ожидается 2"):
            await plans.submit(project.id, raw)


async def test_render_requires_approved_plan(plans: PlanService, project: Project) -> None:
    with pytest.raises(PlanError):
        await plans.render(project.id)
