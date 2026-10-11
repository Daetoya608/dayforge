"""Оркестратор: планировщик, утверждение плана, перепланирование, релизы, удаление.

Сессии планировщика идут в «интерактивной линии» — сразу, параллельно ночным запускам
(docs/architecture.md#конкурентность), но под блокировкой рабочей директории проекта.
"""

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from dayforge import prompts
from dayforge.agents.launcher import AgentLauncher, LaunchResult
from dayforge.core.enums import (
    AgentRole,
    PlanStatus,
    ProjectStatus,
    QuestionKind,
    SessionOutcome,
    TaskOrigin,
    TaskStatus,
)
from dayforge.core.events import DaysShortage, PlanningFailed, ProjectRef
from dayforge.core.questions import AnswerOutcome
from dayforge.db.models import AgentSession, Plan, Project, Task
from dayforge.runner.runner import Runner
from dayforge.services import Services
from dayforge.vcs.git import Git

log = logging.getLogger(__name__)

PLANNING_STATUSES = (ProjectStatus.DRAFT, ProjectStatus.CLARIFYING, ProjectStatus.PLANNING)
ACTIVE_TASK_STATUSES = (TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.PAUSED)


class Orchestrator:
    def __init__(self, services: Services, launcher: AgentLauncher, runner: Runner) -> None:
        self._s = services
        self._launcher = launcher
        self._runner = runner
        self._background: set[asyncio.Task[Any]] = set()

    # --- интерфейс dayforge.services.Orchestrator ---

    async def start_planning(self, project_id: int) -> None:
        project = await self._s.projects.set_status(project_id, ProjectStatus.CLARIFYING)
        text = prompts.render("planner_clarify.md", name=project.name, days=project.planned_days)
        self._spawn(self._planner_session(project_id, text, resume_session_id=None))

    async def start_replan(self, project_id: int, instructions: str) -> None:
        self._spawn(self._replan(project_id, instructions))

    async def send_plan_feedback(self, plan_id: int, feedback: str) -> None:
        """«Правки» к предложенному плану: план отклонён, правки — в ту же сессию планировщика."""
        project_id = await self._plan_project(plan_id)
        await self._s.plans.reject(plan_id)
        session = await self._latest_planner_session(project_id)
        text = prompts.render("planner_feedback.md", feedback=feedback)
        self._spawn(
            self._planner_session(
                project_id, text, resume_session_id=session.id if session else None
            )
        )

    def kick_runner(self) -> None:
        self._runner.kick()

    async def release(self, project_id: int) -> str:
        return await self._runner.manual_release(project_id)

    async def archive_project(self, project_id: int) -> str:
        project = await self._s.projects.set_status(project_id, ProjectStatus.ARCHIVED)
        return f"Проект {project.slug} архивирован"

    async def delete_project(self, project_id: int, *, delete_remote: bool) -> str:
        async with self._runner.lock(project_id):
            project = await self._s.projects.get_by_id(project_id)
            provider = self._s.repos.provider
            if delete_remote and provider is not None and project.remote_url:
                await provider.delete_repo(provider.repo_from_remote(project.remote_url))
            await asyncio.to_thread(shutil.rmtree, project.repo_path, True)
            async with self._s.session_factory() as session, session.begin():
                row = await session.get(Project, project_id)
                if row is not None:
                    await session.delete(row)
        where = " и репозиторий на GitHub" if delete_remote and project.remote_url else ""
        return f"Проект {project.slug} удалён{where}"

    # --- решения пользователя ---

    async def approve_plan(self, plan_id: int) -> Project:
        project_id = await self._plan_project(plan_id)
        before = await self._s.projects.get_by_id(project_id)
        first_approval = before.status is ProjectStatus.PLAN_REVIEW
        if first_approval:
            project = await self._s.plans.approve(plan_id)
            try:
                await self._s.repos.publish(project.id)
            except Exception as exc:
                log.warning("publish %s failed: %s", project.slug, exc)
        else:
            async with self._runner.lock(project_id):
                project = await self._s.plans.approve(plan_id)
                await self._commit_docs(project, "docs: перепланирование")
            await self._runner.sync_plan_file(project_id)

        forecast = await self._s.calendar.forecast(project, today=await self._s.calendar.today())
        if forecast.missing_days:
            await self._s.notifier.send(
                DaysShortage(project=ProjectRef.of(project), missing_days=forecast.missing_days)
            )
        return project

    async def reject_plan(self, plan_id: int) -> Project:
        """«Отклонить»: новый проект архивируется, перепланирование просто отменяется."""
        project_id = await self._plan_project(plan_id)
        project = await self._s.projects.get_by_id(project_id)
        await self._s.plans.reject(plan_id)
        if project.status in (ProjectStatus.PLAN_REVIEW, *PLANNING_STATUSES):
            return await self._s.projects.set_status(project_id, ProjectStatus.ARCHIVED)
        async with self._runner.lock(project_id):
            await self._discard_docs(project)
        return project

    async def after_answer(self, outcome: AnswerOutcome) -> None:
        """Ответ на вопрос планировщика сразу продолжает его сессию."""
        if outcome.task_id is not None or outcome.session_id is None:
            return
        project = await self._s.projects.get_by_id(outcome.project_id)
        question = await self._s.questions.get(outcome.question_id)
        if question.kind is QuestionKind.SECRET:
            text = prompts.render(
                "executor_resume.md", secret_name=question.payload["name"], answers=[]
            )
        else:
            items = [
                {"question": q["text"], "answer": a}
                for q, a in zip(question.payload["questions"], question.answers or [], strict=False)
            ]
            text = prompts.render("planner_answers.md", answers=items)
        if project.status in PLANNING_STATUSES:
            await self._s.projects.set_status(project.id, ProjectStatus.PLANNING)
        self._spawn(self._planner_session(project.id, text, resume_session_id=outcome.session_id))

    async def recover(self) -> None:
        """После рестарта: планирование, прерванное на полуслове, — сообщить пользователю."""
        async with self._s.session_factory() as session:
            projects = list(
                await session.scalars(select(Project).where(Project.status.in_(PLANNING_STATUSES)))
            )
        for project in projects:
            if await self._s.questions.has_open_question(project_id=project.id):
                continue
            await self._s.notifier.send(
                PlanningFailed(project=ProjectRef.of(project), reason="прервано перезапуском")
            )

    async def stop(self) -> None:
        for task in list(self._background):
            task.cancel()
        for task in list(self._background):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def wait_idle(self) -> None:
        """Дождаться фоновых сессий планировщика (для тестов)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # --- внутреннее ---

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _replan(self, project_id: int, instructions: str) -> None:
        project = await self._s.projects.get_by_id(project_id)
        async with self._s.session_factory() as session:
            tasks = list(await session.scalars(select(Task).where(Task.project_id == project_id)))
        done = sorted(t.key for t in tasks if t.status is TaskStatus.DONE)
        active = sorted(
            t.key for t in tasks if t.origin is TaskOrigin.PLAN and t.status in ACTIVE_TASK_STATUSES
        )
        current_open = any(
            t.day_number == project.current_day
            and t.status not in (TaskStatus.DONE, TaskStatus.CANCELLED)
            for t in tasks
        )
        first_day = (
            1
            if project.current_day == 0
            else project.current_day
            if current_open
            else project.current_day + 1
        )
        text = prompts.render(
            "planner_replan.md",
            name=project.name,
            instructions=instructions,
            current_day=project.current_day,
            planned_days=project.planned_days,
            first_day=first_day,
            done_keys=done,
            active_keys=active,
        )
        await self._planner_session(project_id, text, resume_session_id=None)

    async def _planner_session(
        self, project_id: int, prompt: str, *, resume_session_id: int | None
    ) -> None:
        try:
            async with self._runner.lock(project_id):
                project = await self._s.projects.get_by_id(project_id)
                result = await self._launcher.launch(
                    role=AgentRole.PLANNER,
                    agent=project.planner_agent,
                    cwd=Path(project.repo_path),
                    prompt=prompt,
                    system_prompt=prompts.render("planner_system.md"),
                    project_id=project_id,
                    resume_session_id=resume_session_id,
                )
            await self._after_planner(project, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("planner session for project %s failed", project_id)
            project = await self._s.projects.get_by_id(project_id)
            await self._s.notifier.send(
                PlanningFailed(project=ProjectRef.of(project), reason=str(exc)[:500])
            )

    async def _after_planner(self, project: Project, result: LaunchResult) -> None:
        if result.outcome in (SessionOutcome.QUESTION, SessionOutcome.PLAN_SUBMITTED):
            return
        reasons = {
            SessionOutcome.LIMIT: "лимит подписки — повторите позже",
            SessionOutcome.WATCHDOG: "агент завис — сессия прервана",
            SessionOutcome.ERROR: "ошибка агента",
        }
        reason = reasons.get(result.outcome, "агент не задал вопросы и не отправил план")  # type: ignore[arg-type]
        await self._s.notifier.send(PlanningFailed(project=ProjectRef.of(project), reason=reason))

    async def _latest_planner_session(self, project_id: int) -> AgentSession | None:
        async with self._s.session_factory() as session:
            return await session.scalar(
                select(AgentSession)
                .where(
                    AgentSession.project_id == project_id, AgentSession.role == AgentRole.PLANNER
                )
                .order_by(AgentSession.id.desc())
                .limit(1)
            )

    async def _plan_project(self, plan_id: int) -> int:
        async with self._s.session_factory() as session:
            plan = await session.get(Plan, plan_id)
        if plan is None or plan.status is not PlanStatus.PROPOSED:
            raise ValueError("план уже не ждёт решения")
        return plan.project_id

    async def _commit_docs(self, project: Project, message: str) -> None:
        """Изменения документов планировщиком при перепланировании — коммитом в dev."""
        git = Git(Path(project.repo_path))
        if await git.current_branch() == "dev" and not await git.is_clean():
            await git.run("add", "docs")
            if await git.run("diff", "--cached", "--name-only"):
                await git.run("commit", "-q", "-m", message)

    async def _discard_docs(self, project: Project) -> None:
        git = Git(Path(project.repo_path))
        if await git.current_branch() == "dev":
            await git.run("checkout", "--", "docs")
