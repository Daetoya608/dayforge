"""Сервисы ядра одним объектом: собираются в app.py, передаются боту, MCP и раннеру."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar_service import CalendarService
from dayforge.core.events import Notifier
from dayforge.core.pending_actions import PendingActionService
from dayforge.core.plans import PlanService
from dayforge.core.projects import ProjectService
from dayforge.core.questions import QuestionService
from dayforge.core.releases import ReleaseService
from dayforge.core.reports import ReportService
from dayforge.core.repos import RepoSync
from dayforge.core.runs import RunService
from dayforge.core.sessions import SessionService
from dayforge.core.settings import DEFAULTS, SettingsService
from dayforge.core.tasks import TaskService
from dayforge.vcs.base import VcsProvider


class Orchestrator(Protocol):
    """Долгие действия с агентами и git: планирование, раннер, релизы, удаление.

    Реализация — dayforge.orchestrator; здесь интерфейс, чтобы MCP и бот от неё не зависели.
    """

    async def start_planning(self, project_id: int) -> None: ...

    async def start_replan(self, project_id: int, instructions: str) -> None: ...

    async def send_plan_feedback(self, plan_id: int, feedback: str) -> None: ...

    def kick_runner(self) -> None: ...

    async def release(self, project_id: int) -> str: ...

    async def archive_project(self, project_id: int) -> str: ...

    async def delete_project(self, project_id: int, *, delete_remote: bool) -> str: ...

    async def approve_plan(self, plan_id: int) -> Any: ...

    async def reject_plan(self, plan_id: int) -> Any: ...

    async def after_answer(self, outcome: Any) -> None: ...

    async def push_project(self, project_id: int) -> str: ...


@dataclass
class Services:
    session_factory: async_sessionmaker[AsyncSession]
    notifier: Notifier
    settings: SettingsService
    projects: ProjectService
    calendar: CalendarService
    plans: PlanService
    tasks: TaskService
    runs: RunService
    reports: ReportService
    questions: QuestionService
    actions: PendingActionService
    sessions: SessionService
    repos: RepoSync
    releases: ReleaseService
    # Подключается после сборки: оркестратору самому нужны сервисы.
    orchestrator: Orchestrator | None = None

    def require_orchestrator(self) -> Orchestrator:
        if self.orchestrator is None:
            raise RuntimeError("оркестратор не подключён")
        return self.orchestrator


def build_services(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    *,
    projects_dir: Path,
    sessions_log_dir: Path,
    vcs_provider: VcsProvider | None,
    pending_action_ttl: timedelta = timedelta(hours=DEFAULTS["pending_action_ttl_hours"]),
) -> Services:
    settings = SettingsService(session_factory)
    calendar = CalendarService(session_factory, settings)
    return Services(
        session_factory=session_factory,
        notifier=notifier,
        settings=settings,
        projects=ProjectService(session_factory, settings, projects_dir),
        calendar=calendar,
        plans=PlanService(session_factory, calendar),
        tasks=TaskService(session_factory),
        runs=RunService(session_factory, calendar),
        reports=ReportService(session_factory),
        questions=QuestionService(session_factory, notifier),
        actions=PendingActionService(session_factory, notifier, ttl=pending_action_ttl),
        sessions=SessionService(session_factory, sessions_log_dir),
        repos=RepoSync(session_factory, vcs_provider),
        releases=ReleaseService(session_factory),
    )
