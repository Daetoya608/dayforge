from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.actions import register_action_handlers
from dayforge.core.enums import (
    AgentRole,
    PlanStatus,
    ProjectStatus,
    QuestionKind,
    SessionOutcome,
    TaskStatus,
)
from dayforge.core.events import PlanChangeProposed, PlanProposed, RecordingNotifier, TaskFailed
from dayforge.core.secrets import EnvFile
from dayforge.db.models import CalendarDate, PendingAction, Plan, Project, Question, Task
from dayforge.services import Services, build_services
from tests.mcp_helpers import FakeOrchestrator, call, client, running_server
from tests.test_plan_format import make_plan

SessionFactory = async_sessionmaker[AsyncSession]
AGENT = {"agent": "claude_code", "model": "opus", "effort": "medium"}


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def orchestrator() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
def services(
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    orchestrator: FakeOrchestrator,
    tmp_path: Path,
) -> Services:
    services = build_services(
        session_factory,
        notifier,
        projects_dir=tmp_path / "ws",
        sessions_log_dir=tmp_path / "logs",
        vcs_provider=None,
    )
    services.orchestrator = orchestrator
    register_action_handlers(services)
    return services


@pytest.fixture
async def port(services: Services) -> Any:
    async with running_server(services) as port:
        yield port


async def task_of(session_factory: SessionFactory, project: Project) -> Task:
    async with session_factory() as session:
        return (await session.scalars(select(Task).where(Task.project_id == project.id))).one()


async def executor_token(services: Services, project: Project, task: Task) -> tuple[int, str]:
    row, token = await services.sessions.create(
        AgentRole.EXECUTOR, agent=AGENT, project_id=project.id, task_id=task.id
    )
    return row.id, token


class TestAuth:
    async def test_missing_token_is_rejected(self, port: int) -> None:
        with pytest.raises(Exception):  # noqa: B017 — клиент оборачивает 401 по-разному
            async with client(port, AgentRole.EXECUTOR, None):
                pass

    async def test_token_of_other_role_is_rejected(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
    ) -> None:
        _, token = await executor_token(
            services, active_project, await task_of(session_factory, active_project)
        )
        with pytest.raises(Exception):  # noqa: B017
            async with client(port, AgentRole.SYSTEM, token):
                pass

    async def test_ended_session_token_stops_working(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
    ) -> None:
        from dayforge.core.enums import SessionStatus

        session_id, token = await executor_token(
            services, active_project, await task_of(session_factory, active_project)
        )
        async with client(port, AgentRole.EXECUTOR, token) as mcp:
            tools = {t.name for t in (await mcp.list_tools()).tools}
        assert tools == {
            "ask_user",
            "request_secret",
            "complete_task",
            "fail_task",
            "propose_plan_change",
        }

        await services.sessions.end(session_id, SessionStatus.ENDED)
        with pytest.raises(Exception):  # noqa: B017
            async with client(port, AgentRole.EXECUTOR, token):
                pass


class TestExecutor:
    async def test_ask_user_pauses_and_finalizes_session(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
    ) -> None:
        task = await task_of(session_factory, active_project)
        session_id, token = await executor_token(services, active_project, task)

        async with client(port, AgentRole.EXECUTOR, token) as mcp:
            error, result = await call(
                mcp,
                "ask_user",
                questions=[{"text": "Какой провайдер?", "options": ["Stripe", "ЮKassa"]}],
                context="платежи",
            )
            assert not error
            assert result["status"] == "paused"
            assert "wip(T1)" in result["instruction"]

            error, result = await call(mcp, "complete_task", summary="готово")
            assert error
            assert result["error"] == "session_finalizing"

        assert (await services.sessions.info(session_id)).outcome is SessionOutcome.QUESTION
        assert (await task_of(session_factory, active_project)).status is TaskStatus.PAUSED

    async def test_complete_task(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
    ) -> None:
        task = await task_of(session_factory, active_project)
        session_id, token = await executor_token(services, active_project, task)

        async with client(port, AgentRole.EXECUTOR, token) as mcp:
            error, result = await call(
                mcp, "complete_task", summary="Сделал платежи", decisions=["Stripe"]
            )

        assert not error and result["status"] == "accepted"
        stored = await task_of(session_factory, active_project)
        assert (stored.status, stored.summary, stored.decisions) == (
            TaskStatus.VERIFYING,
            "Сделал платежи",
            ["Stripe"],
        )
        assert (await services.sessions.info(session_id)).outcome is SessionOutcome.COMPLETED

    async def test_request_secret_available_is_not_terminal(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
    ) -> None:
        EnvFile(Path(active_project.repo_path) / ".env").set("STRIPE_KEY", "x")
        task = await task_of(session_factory, active_project)
        session_id, token = await executor_token(services, active_project, task)

        async with client(port, AgentRole.EXECUTOR, token) as mcp:
            error, result = await call(mcp, "request_secret", name="STRIPE_KEY", description="ключ")
            assert not error and result == {
                "status": "available",
                "env_var": "STRIPE_KEY",
                "env_file": ".env",
            }
            error, result = await call(mcp, "request_secret", name="OTHER_KEY", description="x")
            assert result["status"] == "paused"

        assert (await services.sessions.info(session_id)).outcome is SessionOutcome.SECRET

    async def test_fail_and_propose(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
        notifier: RecordingNotifier,
    ) -> None:
        task = await task_of(session_factory, active_project)
        _, token = await executor_token(services, active_project, task)

        async with client(port, AgentRole.EXECUTOR, token) as mcp:
            error, result = await call(
                mcp, "propose_plan_change", reason="связаны", proposal="объединить T1 и T2"
            )
            assert not error and result == {"status": "sent"}
            error, _ = await call(mcp, "fail_task", reason="нет API", suggestion="CSV")
            assert not error

        assert (await task_of(session_factory, active_project)).status is TaskStatus.FAILED
        (failed,) = notifier.of_type(TaskFailed)
        assert (failed.task_key, failed.suggestion) == ("T1", "CSV")
        assert notifier.of_type(PlanChangeProposed)[0].proposal == "объединить T1 и T2"


class TestPlanner:
    async def planner_token(self, services: Services, project: Project) -> str:
        _, token = await services.sessions.create(
            AgentRole.PLANNER, agent=AGENT, project_id=project.id
        )
        return token

    async def new_project(self, services: Services) -> Project:
        from dayforge.core.projects import NewProject

        (services.projects._projects_dir).mkdir(exist_ok=True)
        return await services.projects.create(
            NewProject(name="Todo", slug="todo-bot", idea="Бот", planned_days=3)
        )

    async def test_invalid_plan_returns_details(self, port: int, services: Services) -> None:
        project = await self.new_project(services)
        plan = make_plan()
        plan["days"][2]["day"] = 9
        token = await self.planner_token(services, project)

        async with client(port, AgentRole.PLANNER, token) as mcp:
            error, result = await call(mcp, "submit_plan", **plan)

        assert error
        assert result["error"] == "invalid_plan"
        assert result["details"] == ["days[2].day: ожидается 3, получено 9"]

    async def test_valid_plan_goes_to_review(
        self,
        port: int,
        services: Services,
        session_factory: SessionFactory,
        notifier: RecordingNotifier,
    ) -> None:
        project = await self.new_project(services)
        token = await self.planner_token(services, project)

        async with client(port, AgentRole.PLANNER, token) as mcp:
            error, result = await call(mcp, "submit_plan", **make_plan())

        assert not error and result["status"] == "proposed"
        async with session_factory() as session:
            plan = (await session.scalars(select(Plan))).one()
        assert plan.status is PlanStatus.PROPOSED
        (event,) = notifier.of_type(PlanProposed)
        assert (event.plan_id, event.days, event.diff) == (plan.id, 3, None)
        assert (await services.projects.get_by_id(project.id)).status is ProjectStatus.PLAN_REVIEW

    async def test_clarifying_questions(
        self, port: int, services: Services, session_factory: SessionFactory
    ) -> None:
        project = await self.new_project(services)
        token = await self.planner_token(services, project)

        async with client(port, AgentRole.PLANNER, token) as mcp:
            error, _ = await call(mcp, "ask_user", questions=[{"text": "Валюта?"}])

        assert not error
        async with session_factory() as session:
            question = (await session.scalars(select(Question))).one()
        assert question.task_id is None


class TestSystem:
    async def system_client_token(self, services: Services) -> str:
        _, token = await services.sessions.create(AgentRole.SYSTEM, agent=AGENT)
        return token

    async def test_read_tools(self, port: int, services: Services, active_project: Project) -> None:
        token = await self.system_client_token(services)
        async with client(port, AgentRole.SYSTEM, token) as mcp:
            _, projects = await call(mcp, "list_projects")
            _, details = await call(mcp, "get_project", project="todo-bot")
            error, missing = await call(mcp, "get_project", project="nope")
            _, calendar = await call(mcp, "get_calendar")

        assert [p["slug"] for p in projects["projects"]] == ["todo-bot"]
        assert details["tasks"][0]["key"] == "T1"
        assert error and missing["error"] == "not_found"
        assert calendar == {"days": [], "weekdays": {}}

    async def test_changes(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
        orchestrator: FakeOrchestrator,
    ) -> None:
        token = await self.system_client_token(services)
        async with client(port, AgentRole.SYSTEM, token) as mcp:
            _, added = await call(
                mcp,
                "add_task",
                project="todo-bot",
                title="Опечатка",
                description="README",
                when="now",
            )
            _, small = await call(
                mcp,
                "update_calendar",
                changes=[{"op": "include", "project": "todo-bot", "dates": ["2026-11-07"]}],
            )
            _, bulk = await call(
                mcp,
                "update_calendar",
                changes=[{"op": "set_weekdays", "project": "todo-bot", "weekdays": [0, 2]}],
            )
            _, deleted = await call(mcp, "delete_project", project="todo-bot")
            _, created = await call(mcp, "create_project", idea="Сайт-визитка", days=2)

        assert added == {"status": "done", "task": "A1"}
        assert small["status"] == "done"
        assert bulk["status"] == "pending_confirmation"
        assert deleted["status"] == "pending_confirmation"
        assert created["status"] == "started"
        async with session_factory() as session:
            assert len(list(await session.scalars(select(CalendarDate)))) == 1
            kinds = [a.kind for a in await session.scalars(select(PendingAction))]
        assert kinds == ["bulk_calendar", "delete_project"]
        assert [name for name, _ in orchestrator.calls] == ["kick_runner", "start_planning"]

        decision = await services.actions.decide(1, confirm=True)
        assert decision.result is not None and "Календарь обновлён" in decision.result

    async def test_secret_answers_are_refused(
        self,
        port: int,
        services: Services,
        active_project: Project,
        session_factory: SessionFactory,
    ) -> None:
        question_id = await services.questions.request_secret(
            project_id=active_project.id,
            task_id=None,
            session_id=None,
            name="API_KEY",
            description="x",
        )
        token = await self.system_client_token(services)
        async with client(port, AgentRole.SYSTEM, token) as mcp:
            error, result = await call(
                mcp, "answer_question", question_id=question_id, answers=["value"]
            )

        assert error and result["error"] == "secret"
        async with session_factory() as session:
            question = await session.get(Question, question_id)
        assert question is not None and question.kind is QuestionKind.SECRET
