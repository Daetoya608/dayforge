from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.agents.launcher import AgentLauncher
from dayforge.chat import ChatAgent
from dayforge.core.enums import PlanStatus, ProjectStatus, SessionOutcome, TaskStatus
from dayforge.core.events import PlanningFailed, RecordingNotifier
from dayforge.core.projects import NewProject
from dayforge.db.models import Plan, Project, Task
from dayforge.orchestrator import Orchestrator
from dayforge.runner.runner import Runner
from dayforge.services import Services, build_services
from dayforge.vcs.git import Git
from tests.scripted_agent import Call, Script, ScriptedAdapter, ask
from tests.test_runner import two_day_plan

SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def services(
    session_factory: SessionFactory, notifier: RecordingNotifier, tmp_path: Path
) -> Services:
    (tmp_path / "ws").mkdir()
    return build_services(
        session_factory,
        notifier,
        projects_dir=tmp_path / "ws",
        sessions_log_dir=tmp_path / "logs",
        vcs_provider=None,
    )


def build(services: Services, script: Script) -> tuple[Orchestrator, ScriptedAdapter]:
    adapter = ScriptedAdapter(services, script)
    launcher = AgentLauncher(
        services.sessions,
        services.settings,
        {"claude_code": adapter},  # type: ignore[dict-item]
        mcp_base_url="http://127.0.0.1:1",
    )
    orchestrator = Orchestrator(services, launcher, Runner(services, launcher))
    services.orchestrator = orchestrator
    return orchestrator, adapter


async def new_project(services: Services) -> Project:
    return await services.projects.create(
        NewProject(name="Todo", slug="todo-bot", idea="Бот учёта расходов", planned_days=2)
    )


async def submit(call: Call, services: Services, raw: dict[str, Any] | None = None) -> None:
    assert call.session.project_id is not None
    await services.plans.submit(call.session.project_id, raw or two_day_plan())
    await services.sessions.record_outcome(call.session.id, SessionOutcome.PLAN_SUBMITTED)


async def latest_plan(sf: SessionFactory) -> Plan:
    async with sf() as session:
        plans = list(await session.scalars(select(Plan).order_by(Plan.id)))
    return plans[-1]


async def test_clarify_answer_plan_approve(
    services: Services, session_factory: SessionFactory
) -> None:
    question_ids: list[int] = []

    async def planner(call: Call, services: Services) -> None:
        if call.number == 1:
            question_ids.append(await ask(call, services, "Какая валюта?"))
        else:
            (call.request.cwd / "docs" / "ARCHITECTURE.md").write_text("# Архитектура\n")
            await submit(call, services)

    orchestrator, adapter = build(services, planner)
    project = await new_project(services)

    await orchestrator.start_planning(project.id)
    await orchestrator.wait_idle()
    assert (await services.projects.get_by_id(project.id)).status is ProjectStatus.CLARIFYING
    assert "Новый проект: **Todo**" in adapter.calls[0].request.prompt
    assert "архитектор" in adapter.calls[0].request.system_prompt

    outcome = await services.questions.answer(question_ids[0], ["Рубли"])
    await orchestrator.after_answer(outcome)
    await orchestrator.wait_idle()

    resumed = adapter.calls[1].request
    assert resumed.resume_id == "cli-1"
    assert "**Какая валюта?** — Рубли" in resumed.prompt
    assert (await services.projects.get_by_id(project.id)).status is ProjectStatus.PLAN_REVIEW

    plan = await latest_plan(session_factory)
    approved = await orchestrator.approve_plan(plan.id)

    assert approved.status is ProjectStatus.ACTIVE
    files = await Git(Path(project.repo_path)).run("ls-files")
    assert "docs/ARCHITECTURE.md" in files and "docs/PLAN.md" in files


async def test_feedback_resumes_planner_and_reject_archives(
    services: Services, session_factory: SessionFactory
) -> None:
    orchestrator, adapter = build(services, submit)
    project = await new_project(services)
    await orchestrator.start_planning(project.id)
    await orchestrator.wait_idle()
    first = await latest_plan(session_factory)

    await orchestrator.send_plan_feedback(first.id, "Объедини дни\nИ добавь экспорт")
    await orchestrator.wait_idle()

    assert "> Объедини дни\n> И добавь экспорт" in adapter.calls[1].request.prompt
    assert adapter.calls[1].request.resume_id == "cli-1"
    second = await latest_plan(session_factory)
    assert (second.version, second.status) == (2, PlanStatus.PROPOSED)
    async with session_factory() as session:
        stored_first = await session.get(Plan, first.id)
    assert stored_first is not None and stored_first.status is PlanStatus.REJECTED

    archived = await orchestrator.reject_plan(second.id)
    assert archived.status is ProjectStatus.ARCHIVED


async def test_planner_without_result_is_reported(
    services: Services, notifier: RecordingNotifier
) -> None:
    async def silent(call: Call, services: Services) -> None:
        return None

    orchestrator, _ = build(services, silent)
    project = await new_project(services)

    await orchestrator.start_planning(project.id)
    await orchestrator.wait_idle()

    (event,) = notifier.of_type(PlanningFailed)
    assert "не задал вопросы и не отправил план" in event.reason


async def test_replan_commits_docs_and_plan(
    services: Services, session_factory: SessionFactory
) -> None:
    async def planner(call: Call, services: Services) -> None:
        if call.number == 1:
            await submit(call, services)
            return
        raw = two_day_plan()
        raw["change_summary"] = "Новая задача"
        raw["days"] = [raw["days"][1]]
        raw["days"][0]["tasks"].append(
            {
                "key": "T4",
                "title": "Экспорт",
                "description": "CSV",
                "acceptance_criteria": ["есть"],
                "milestone": "M2",
            }
        )
        (call.request.cwd / "docs" / "ARCHITECTURE.md").write_text("# Новая архитектура\n")
        await submit(call, services, raw)

    orchestrator, adapter = build(services, planner)
    project = await new_project(services)
    await services.calendar.set_weekdays(project.id, range(7))
    await orchestrator.start_planning(project.id)
    await orchestrator.wait_idle()
    await orchestrator.approve_plan((await latest_plan(session_factory)).id)
    async with session_factory() as session, session.begin():
        stored = await session.get(Project, project.id)
        assert stored is not None
        stored.current_day = 1
        for task in await session.scalars(select(Task).where(Task.day_number == 1)):
            task.status = TaskStatus.DONE

    await orchestrator.start_replan(project.id, "Добавь экспорт")
    await orchestrator.wait_idle()

    prompt = adapter.calls[1].request.prompt
    assert "новый план начинается с дня **2**" in prompt
    assert "выполнены (в план не включать, ключи не переиспользовать): T1, T2" in prompt

    await orchestrator.approve_plan((await latest_plan(session_factory)).id)

    git = Git(Path(project.repo_path))
    log = await git.run("log", "--format=%s", "dev")
    assert "docs: перепланирование" in log
    assert "chore(plan): обновлён PLAN.md" in log
    assert await git.is_clean()
    assert "**T4** Экспорт" in (Path(project.repo_path) / "docs" / "PLAN.md").read_text()


async def test_delete_project(services: Services, session_factory: SessionFactory) -> None:
    orchestrator, _ = build(services, submit)
    project = await new_project(services)

    message = await orchestrator.delete_project(project.id, delete_remote=False)

    assert message == "Проект todo-bot удалён"
    assert not Path(project.repo_path).exists()
    assert await services.projects.list_projects(include_archived=True) == []


async def test_chat_keeps_session_and_resets(services: Services, tmp_path: Path) -> None:
    replies = iter(["Привет!", "Второй ответ", "Новый диалог"])

    async def agent(call: Call, services: Services) -> dict[str, Any]:
        assert call.request.read_only
        assert call.request.cwd == tmp_path / "ws"
        return {"final_text": next(replies)}

    adapter = ScriptedAdapter(services, agent)
    launcher = AgentLauncher(
        services.sessions,
        services.settings,
        {"claude_code": adapter},  # type: ignore[dict-item]
        mcp_base_url="http://127.0.0.1:1",
    )
    chat = ChatAgent(services, launcher, projects_dir=tmp_path / "ws")

    assert await chat.ask("привет") == "Привет!"
    assert await chat.ask("а что с проектами?") == "Второй ответ"
    assert adapter.calls[1].request.resume_id == "cli-1"
    assert "менеджер" in adapter.calls[0].request.system_prompt

    chat.reset()
    assert await chat.ask("снова") == "Новый диалог"
    assert adapter.calls[2].request.resume_id is None


async def test_chat_fallback_on_error(services: Services, tmp_path: Path) -> None:
    async def agent(call: Call, services: Services) -> dict[str, Any]:
        return {"exit_code": 1, "error_kind": "limit"}

    adapter = ScriptedAdapter(services, agent)
    launcher = AgentLauncher(
        services.sessions,
        services.settings,
        {"claude_code": adapter},  # type: ignore[dict-item]
        mcp_base_url="http://127.0.0.1:1",
    )
    chat = ChatAgent(services, launcher, projects_dir=tmp_path)

    assert "лимит" in await chat.ask("привет")
