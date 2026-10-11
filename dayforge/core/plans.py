"""Приём, утверждение и отклонение плана, генерация PLAN.md (docs/plan-format.md)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar import Forecast
from dayforge.core.calendar_service import CalendarService
from dayforge.core.enums import (
    CommitKind,
    MilestoneStatus,
    PlanAuthor,
    PlanStatus,
    ProjectStatus,
    TaskOrigin,
    TaskStatus,
)
from dayforge.core.plan_diff import diff_plans
from dayforge.core.plan_format import PlanContent, PlanContext, check_plan
from dayforge.core.plan_render import MilestoneView, PlanView, TaskView, render_plan
from dayforge.db.base import utcnow
from dayforge.db.models import Commit, Milestone, Plan, Project, Task
from dayforge.vcs.git import Git

INITIAL_STATUSES = (
    ProjectStatus.DRAFT,
    ProjectStatus.CLARIFYING,
    ProjectStatus.PLANNING,
    ProjectStatus.PLAN_REVIEW,
)
REPLAN_STATUSES = (ProjectStatus.ACTIVE, ProjectStatus.BLOCKED, ProjectStatus.PAUSED)
ACTIVE_TASK_STATUSES = (TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.PAUSED)
# Задачи, которые перепланирование может отменить, если их нет в новом плане.
CANCELLABLE_STATUSES = (TaskStatus.PENDING, TaskStatus.FAILED)


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class SubmitResult:
    plan_id: int
    version: int
    warnings: list[str]
    # None — первый план проекта, сравнивать не с чем.
    diff: list[str] | None
    forecast: Forecast


class PlanService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], calendar: CalendarService
    ) -> None:
        self._session_factory = session_factory
        self._calendar = calendar

    async def submit(
        self, project_id: int, raw: dict[str, Any], *, author: PlanAuthor = PlanAuthor.PLANNER
    ) -> SubmitResult:
        """Проверяет план и сохраняет его на согласование. Ошибки — PlanValidationError."""
        async with self._session_factory() as session, session.begin():
            project = await _project(session, project_id)
            if project.status not in INITIAL_STATUSES + REPLAN_STATUSES:
                raise PlanError(f"проект в статусе {project.status}: план не принимается")

            tasks = await _tasks(session, project_id)
            done_keys = frozenset(t.key for t in tasks if t.status is TaskStatus.DONE)
            checked = check_plan(
                raw,
                PlanContext(
                    planned_days=project.planned_days,
                    first_day=_first_replan_day(project, tasks),
                    done_keys=done_keys,
                    active_keys=frozenset(
                        t.key
                        for t in tasks
                        if t.origin is TaskOrigin.PLAN and t.status in ACTIVE_TASK_STATUSES
                    ),
                ),
            )

            approved = await _approved_plan(session, project_id)
            diff = (
                diff_plans(
                    PlanContent.model_validate(approved.content),
                    checked.plan,
                    done_keys=done_keys,
                )
                if approved
                else None
            )

            await session.execute(
                update(Plan)
                .where(Plan.project_id == project_id, Plan.status == PlanStatus.PROPOSED)
                .values(status=PlanStatus.SUPERSEDED, decided_at=utcnow())
            )
            version = (
                await session.scalar(
                    select(func.max(Plan.version)).where(Plan.project_id == project_id)
                )
                or 0
            ) + 1
            plan = Plan(
                project_id=project_id,
                version=version,
                content=checked.plan.model_dump(mode="json"),
                summary=checked.plan.summary,
                change_summary=checked.plan.change_summary,
                created_by=author,
            )
            session.add(plan)
            if project.status in INITIAL_STATUSES:
                project.status = ProjectStatus.PLAN_REVIEW
            await session.flush()

        forecast = await self._calendar.forecast(
            project,
            today=await self._calendar.today(),
            planned_days=checked.plan.days[-1].day,
        )
        return SubmitResult(plan.id, version, checked.warnings, diff, forecast)

    async def approve(self, plan_id: int) -> Project:
        """Утверждение: задачи и вехи синхронизируются с планом в одной транзакции.

        Первое утверждение ещё и инициализирует репозиторий: PLAN.md, JOURNAL.md, первый коммит
        в main, ветка dev. PLAN.md после перепланирования коммитит раннер — под блокировкой
        проекта, когда в рабочей директории не идёт задача.
        """
        async with self._session_factory() as session, session.begin():
            plan = await _proposed_plan(session, plan_id)
            project = await _project(session, plan.project_id)
            content = PlanContent.model_validate(plan.content)
            now = utcnow()

            await session.execute(
                update(Plan)
                .where(Plan.project_id == project.id, Plan.status == PlanStatus.APPROVED)
                .values(status=PlanStatus.SUPERSEDED, decided_at=now)
            )
            plan.status = PlanStatus.APPROVED
            plan.decided_at = now

            await _sync_tasks(session, project.id, content)
            await _sync_milestones(session, project.id, content)
            project.day_goals = project.day_goals | {str(d.day): d.goal for d in content.days}
            project.planned_days = content.days[-1].day
            project.test_command = content.test_command

            if project.status is ProjectStatus.PLAN_REVIEW:
                project.status = ProjectStatus.ACTIVE
                project.activated_at = now
                await session.flush()
                # Внутри транзакции: если git упадёт, утверждение откатится и его можно повторить.
                await self._init_repository(session, project, plan)
        return project

    async def reject(self, plan_id: int) -> None:
        """План отклонён; новый проект возвращается к планированию."""
        async with self._session_factory() as session, session.begin():
            plan = await _proposed_plan(session, plan_id)
            plan.status = PlanStatus.REJECTED
            plan.decided_at = utcnow()
            project = await _project(session, plan.project_id)
            if project.status is ProjectStatus.PLAN_REVIEW:
                project.status = ProjectStatus.PLANNING

    async def render(self, project_id: int) -> str:
        async with self._session_factory() as session:
            project = await _project(session, project_id)
            plan = await _approved_plan(session, project_id)
            if plan is None:
                raise PlanError("у проекта нет утверждённого плана")
            return await self._render(session, project, plan)

    async def _render(self, session: AsyncSession, project: Project, plan: Plan) -> str:
        milestones = await session.scalars(
            select(Milestone).where(Milestone.project_id == project.id).order_by(Milestone.id)
        )
        tasks = await _tasks(session, project.id)
        forecast = await self._calendar.forecast(project, today=await self._calendar.today())
        return render_plan(
            PlanView(
                name=project.name,
                version=plan.version,
                summary=plan.summary,
                test_command=project.test_command or "",
                current_day=project.current_day,
                planned_days=project.planned_days,
                forecast=forecast,
                day_goals={int(day): goal for day, goal in project.day_goals.items()},
                milestones=[
                    MilestoneView(
                        key=m.key,
                        title=m.title,
                        version=m.version,
                        released=m.status is MilestoneStatus.RELEASED,
                    )
                    for m in milestones
                ],
                tasks=[
                    TaskView(
                        key=t.key,
                        title=t.title,
                        description=t.description,
                        acceptance_criteria=t.acceptance_criteria,
                        day_number=t.day_number,
                        position=t.position,
                        status=t.status,
                        pause_reason=t.pause_reason,
                    )
                    for t in tasks
                ],
            )
        )

    async def _init_repository(self, session: AsyncSession, project: Project, plan: Plan) -> None:
        path = Path(project.repo_path)
        (path / "docs" / "PLAN.md").write_text(await self._render(session, project, plan))
        journal = path / "docs" / "JOURNAL.md"
        if not journal.exists():
            journal.write_text(f"# Журнал: {project.name}\n")

        git = Git(path)
        message = f"chore: project init (plan v{plan.version})"
        sha = await git.commit_all(message)
        await git.create_branch("dev")
        session.add(
            Commit(
                sha=sha,
                project_id=project.id,
                branch="main",
                message=message,
                kind=CommitKind.SYSTEM,
                committed_at=utcnow(),
            )
        )


def _first_replan_day(project: Project, tasks: list[Task]) -> int:
    """Начатый день сохраняет номер, если в нём остались задачи; иначе план — со следующего."""
    if project.current_day == 0:
        return 1
    current_day_open = any(
        t.day_number == project.current_day
        and t.status not in (TaskStatus.DONE, TaskStatus.CANCELLED)
        for t in tasks
    )
    return project.current_day if current_day_open else project.current_day + 1


async def _sync_tasks(session: AsyncSession, project_id: int, content: PlanContent) -> None:
    existing = {t.key: t for t in await _tasks(session, project_id)}
    in_plan: set[str] = set()
    for day in content.days:
        for position, planned in enumerate(day.tasks):
            in_plan.add(planned.key)
            fields = {
                "day_number": day.day,
                "position": position,
                "title": planned.title,
                "description": planned.description,
                "acceptance_criteria": list(planned.acceptance_criteria),
                "depends_on": list(planned.depends_on),
                "milestone_key": planned.milestone,
            }
            task = existing.get(planned.key)
            if task is None:
                session.add(
                    Task(project_id=project_id, key=planned.key, origin=TaskOrigin.PLAN, **fields)
                )
            elif task.status is not TaskStatus.DONE:
                for name, value in fields.items():
                    setattr(task, name, value)

    for key, task in existing.items():
        if (
            task.origin is TaskOrigin.PLAN
            and key not in in_plan
            and task.status in CANCELLABLE_STATUSES
        ):
            task.status = TaskStatus.CANCELLED


async def _sync_milestones(session: AsyncSession, project_id: int, content: PlanContent) -> None:
    existing = {
        m.key: m
        for m in await session.scalars(select(Milestone).where(Milestone.project_id == project_id))
    }
    for planned in content.milestones:
        milestone = existing.pop(planned.key, None)
        if milestone is None:
            session.add(
                Milestone(
                    project_id=project_id,
                    key=planned.key,
                    title=planned.title,
                    version=planned.version,
                )
            )
        elif milestone.status is MilestoneStatus.PENDING:
            milestone.title = planned.title
            milestone.version = planned.version
    for milestone in existing.values():
        if milestone.status is MilestoneStatus.PENDING:
            await session.delete(milestone)


async def _project(session: AsyncSession, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise PlanError(f"проекта {project_id} нет")
    return project


async def _tasks(session: AsyncSession, project_id: int) -> list[Task]:
    return list(await session.scalars(select(Task).where(Task.project_id == project_id)))


async def _approved_plan(session: AsyncSession, project_id: int) -> Plan | None:
    return await session.scalar(
        select(Plan).where(Plan.project_id == project_id, Plan.status == PlanStatus.APPROVED)
    )


async def _proposed_plan(session: AsyncSession, plan_id: int) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise PlanError(f"плана {plan_id} нет")
    if plan.status is not PlanStatus.PROPOSED:
        raise PlanError(f"план v{plan.version} уже {plan.status}, решение по нему не принимается")
    return plan
