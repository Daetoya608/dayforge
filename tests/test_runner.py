from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.agents.launcher import AgentLauncher
from dayforge.core.enums import (
    MilestoneStatus,
    PauseReason,
    ProjectStatus,
    RunMode,
    RunStatus,
    RunTrigger,
    TaskStatus,
)
from dayforge.core.events import (
    LimitReached,
    MilestoneReleased,
    ProjectCompleted,
    ProjectSkipped,
    RecordingNotifier,
    RunReport,
    SystemRestarted,
    TaskPaused,
)
from dayforge.core.projects import NewProject
from dayforge.db.models import Commit, Milestone, Project, Run, Task
from dayforge.runner.runner import Runner
from dayforge.services import Services, build_services
from dayforge.vcs.git import Git
from tests.scripted_agent import Call, Script, ScriptedAdapter, ask, commit_file, complete
from tests.test_plan_format import task

SessionFactory = async_sessionmaker[AsyncSession]
TEST_COMMAND = "test -f ok.txt"


def two_day_plan() -> dict[str, Any]:
    return {
        "summary": "Проект для тестов",
        "estimate_comment": "2 дня",
        "test_command": TEST_COMMAND,
        "milestones": [
            {"key": "M1", "title": "База", "version": "v0.1.0"},
            {"key": "M2", "title": "Финал", "version": "v0.2.0"},
        ],
        "days": [
            {"day": 1, "goal": "База", "tasks": [task("T1"), task("T2", depends_on=["T1"])]},
            {"day": 2, "goal": "Финал", "tasks": [task("T3", milestone="M2")]},
        ],
    }


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


async def approved_project(services: Services, slug: str = "todo-bot") -> Project:
    project = await services.projects.create(
        NewProject(name=slug, slug=slug, idea="Тестовый проект", planned_days=2)
    )
    await services.calendar.set_weekdays(project.id, range(7))
    result = await services.plans.submit(project.id, two_day_plan())
    return await services.plans.approve(result.plan_id)


def make_runner(services: Services, script: Script) -> tuple[Runner, ScriptedAdapter]:
    adapter = ScriptedAdapter(services, script)
    launcher = AgentLauncher(
        services.sessions,
        services.settings,
        {"claude_code": adapter},  # type: ignore[dict-item]
        mcp_base_url="http://127.0.0.1:1",
        watchdog_check_seconds=0.02,
    )
    return Runner(services, launcher), adapter


async def run_next(services: Services, runner: Runner, project: Project) -> Run:
    (queued,) = await services.runs.enqueue([project.id], mode=RunMode.NEXT, trigger=RunTrigger.BOT)
    await runner.drain()
    return await services.runs.get(queued.id)


async def tasks_by_key(sf: SessionFactory, project: Project) -> dict[str, Task]:
    async with sf() as session:
        rows = await session.scalars(select(Task).where(Task.project_id == project.id))
        return {t.key: t for t in rows}


async def good_agent(call: Call, services: Services) -> None:
    await commit_file(call, "ok.txt")
    await commit_file(call, f"{call.session.task_id}.txt")
    await complete(call, services)


async def test_full_project_two_days(
    services: Services, session_factory: SessionFactory, notifier: RecordingNotifier
) -> None:
    project = await approved_project(services)
    runner, adapter = make_runner(services, good_agent)
    git = Git(Path(project.repo_path))

    run = await run_next(services, runner, project)

    assert (run.status, run.day_number) == (RunStatus.FINISHED, 1)
    tasks = await tasks_by_key(session_factory, project)
    assert {k: t.status for k, t in tasks.items()} == {
        "T1": TaskStatus.DONE,
        "T2": TaskStatus.DONE,
        "T3": TaskStatus.PENDING,
    }
    assert await git.current_branch() == "dev"
    assert await git.is_clean()
    assert "feature/" not in await git.run("branch")
    log = await git.run("log", "--first-parent", "--format=%s", "dev")
    assert "merge(T1): Задача T1" in log and "merge(T2): Задача T2" in log
    assert "chore(plan): обновлён PLAN.md" in log
    # Веха M1 (T1, T2) выпущена в main с тегом.
    assert await git.ref_exists("refs/tags/v0.1.0")
    assert "chore: release v0.1.0" in await git.run("log", "--format=%s", "main")
    assert await git.rev_parse("v0.1.0^{commit}") == await git.rev_parse("main")
    assert [e.tag for e in notifier.of_type(MilestoneReleased)] == ["v0.1.0"]
    (report,) = notifier.of_type(RunReport)
    assert "✅ <b>T1</b>" in report.text and "день 1 из 2" in report.text
    async with session_factory() as session:
        assert len(list(await session.scalars(select(Commit)))) >= 6
    # Задача исполнителя: новая сессия, промпт с описанием задачи и веткой.
    assert "Задача **T1: Задача T1**" in adapter.calls[0].request.prompt
    assert adapter.calls[0].request.resume_id is None

    run = await run_next(services, runner, project)

    assert (run.status, run.day_number) == (RunStatus.FINISHED, 2)
    stored = await services.projects.get_by_id(project.id)
    assert stored.status is ProjectStatus.COMPLETED
    (completed,) = notifier.of_type(ProjectCompleted)
    assert (completed.days, completed.tasks_done, completed.versions) == (2, 3, ["v0.2.0"])
    async with session_factory() as session:
        statuses = {m.key: m.status for m in await session.scalars(select(Milestone))}
    assert statuses == {"M1": MilestoneStatus.RELEASED, "M2": MilestoneStatus.RELEASED}
    plan_md = (Path(project.repo_path) / "docs" / "PLAN.md").read_text()
    assert "Проект завершён" in plan_md


async def test_failed_tests_get_one_fix_attempt(
    services: Services, session_factory: SessionFactory
) -> None:
    project = await approved_project(services)

    async def agent(call: Call, services: Services) -> None:
        if call.number == 1:
            await commit_file(call, "code.txt")  # ok.txt забыт — тесты упадут
        else:
            await commit_file(call, "ok.txt")
        await complete(call, services)

    runner, adapter = make_runner(services, agent)
    await run_next(services, runner, project)

    tasks = await tasks_by_key(session_factory, project)
    assert tasks["T1"].status is TaskStatus.DONE
    assert "тесты **не прошли**" in adapter.calls[1].request.prompt
    assert adapter.calls[1].request.resume_id == "cli-1"  # та же сессия


async def test_tests_failing_twice_pause_the_task(
    services: Services, session_factory: SessionFactory, notifier: RecordingNotifier
) -> None:
    project = await approved_project(services)

    async def agent(call: Call, services: Services) -> None:
        await commit_file(call, f"code{call.number}.txt")
        await complete(call, services)

    runner, _ = make_runner(services, agent)
    await run_next(services, runner, project)

    tasks = await tasks_by_key(session_factory, project)
    assert (tasks["T1"].status, tasks["T1"].pause_reason) == (
        TaskStatus.PAUSED,
        PauseReason.TESTS_FAILED,
    )
    assert tasks["T2"].status is TaskStatus.PENDING  # ждёт T1
    assert notifier.of_type(TaskPaused)[0].reason == "тесты не прошли"
    (report,) = notifier.of_type(RunReport)
    assert "T2</b> Задача T2 — ждёт зависимостей" in report.text


async def test_question_blocks_then_resumes_same_session(
    services: Services, session_factory: SessionFactory
) -> None:
    project = await approved_project(services)
    questions: list[int] = []

    async def agent(call: Call, services: Services) -> None:
        if call.number == 1:
            (call.request.cwd / "half.txt").write_text("half")  # не закоммичено — сделает WIP
            questions.append(await ask(call, services))
            return
        await commit_file(call, "ok.txt")
        await complete(call, services)

    runner, adapter = make_runner(services, agent)
    run = await run_next(services, runner, project)

    assert run.status is RunStatus.BLOCKED
    assert (await services.projects.get_by_id(project.id)).status is ProjectStatus.BLOCKED
    t1 = (await tasks_by_key(session_factory, project))["T1"]
    git = Git(Path(project.repo_path))
    assert "wip(T1): ждёт ответа" in await git.run("log", "--format=%s", t1.branch or "")

    await services.questions.answer(questions[0], ["Stripe"])
    (queued,) = await services.runs.enqueue(
        [project.id], mode=RunMode.CONTINUE, trigger=RunTrigger.BOT
    )
    await runner.drain()

    resumed = adapter.calls[1].request
    assert resumed.resume_id == "cli-1"
    assert "**Какой провайдер?** — Stripe" in resumed.prompt
    assert (await tasks_by_key(session_factory, project))["T1"].status is TaskStatus.DONE
    assert (await services.runs.get(queued.id)).day_number is None  # continue день не начинает


async def test_blocked_project_skips_scheduled_slot(
    services: Services, notifier: RecordingNotifier
) -> None:
    project = await approved_project(services)
    await services.projects.set_status(project.id, ProjectStatus.BLOCKED)
    runner, adapter = make_runner(services, good_agent)

    (queued,) = await services.runs.enqueue(
        [project.id], mode=RunMode.SCHEDULED, trigger=RunTrigger.SCHEDULER
    )
    await runner.drain()

    assert (await services.runs.get(queued.id)).status is RunStatus.SKIPPED
    assert "слот пропущен" in notifier.of_type(ProjectSkipped)[0].reason
    assert adapter.calls == []


async def test_limit_stops_whole_queue(
    services: Services, session_factory: SessionFactory, notifier: RecordingNotifier
) -> None:
    first = await approved_project(services, "first")
    second = await approved_project(services, "second")

    async def agent(call: Call, services: Services) -> dict[str, Any]:
        return {"exit_code": 1, "error_kind": "limit"}

    runner, _ = make_runner(services, agent)
    runs = await services.runs.enqueue(
        [first.id, second.id], mode=RunMode.NEXT, trigger=RunTrigger.BOT
    )
    await runner.drain()

    statuses = [(await services.runs.get(r.id)).status for r in runs]
    assert statuses == [RunStatus.LIMIT, RunStatus.CANCELLED]
    t1 = (await tasks_by_key(session_factory, first))["T1"]
    assert (t1.status, t1.pause_reason) == (TaskStatus.PAUSED, PauseReason.LIMIT)
    (event,) = notifier.of_type(LimitReached)
    assert [p.slug for p in event.cancelled] == ["second"]
    # Второй проект день не начинал — указатель не сдвинут.
    assert (await services.projects.get_by_id(second.id)).current_day == 0


async def test_session_without_result_gets_one_nudge(
    services: Services, session_factory: SessionFactory
) -> None:
    project = await approved_project(services)

    async def agent(call: Call, services: Services) -> None:
        if call.number == 1:
            await commit_file(call, "ok.txt")  # забыл complete_task
            return
        await complete(call, services)

    runner, adapter = make_runner(services, agent)
    await run_next(services, runner, project)

    assert "Сессия закончилась без завершающего вызова" in adapter.calls[1].request.prompt
    assert (await tasks_by_key(session_factory, project))["T1"].status is TaskStatus.DONE


async def test_agent_touching_dev_is_rolled_back(
    services: Services, session_factory: SessionFactory, notifier: RecordingNotifier
) -> None:
    project = await approved_project(services)
    git = Git(Path(project.repo_path))

    async def agent(call: Call, services: Services) -> None:
        await call.git.checkout("dev")
        await commit_file(call, "ok.txt")
        await complete(call, services)

    runner, _ = make_runner(services, agent)
    await run_next(services, runner, project)

    t1 = (await tasks_by_key(session_factory, project))["T1"]
    assert (t1.status, t1.pause_reason) == (TaskStatus.PAUSED, PauseReason.ERROR)
    assert "агент изменил dev" in (notifier.of_type(TaskPaused)[0].details or "")
    assert "feat: ok.txt" not in await git.run("log", "--format=%s", "dev")


async def test_watchdog_kills_hanging_session(
    services: Services, session_factory: SessionFactory
) -> None:
    project = await approved_project(services)
    await services.settings.set("watchdog_idle_minutes", 0.001)

    async def agent(call: Call, services: Services) -> dict[str, Any]:
        return {"hang": True}

    runner, _ = make_runner(services, agent)
    await run_next(services, runner, project)

    t1 = (await tasks_by_key(session_factory, project))["T1"]
    assert (t1.status, t1.pause_reason) == (TaskStatus.PAUSED, PauseReason.WATCHDOG)


async def test_recover_after_restart(
    services: Services, session_factory: SessionFactory, notifier: RecordingNotifier
) -> None:
    project = await approved_project(services)
    t1 = (await tasks_by_key(session_factory, project))["T1"]
    await services.tasks.mark_running(t1.id, branch="feature/T1-x")
    from dayforge.core.enums import AgentRole

    await services.sessions.create(
        AgentRole.EXECUTOR, agent=project.executor_agent, project_id=project.id, task_id=t1.id
    )
    (Path(project.repo_path) / "dirty.txt").write_text("x")
    runner, _ = make_runner(services, good_agent)

    interrupted = await runner.recover()

    assert [(ref.slug, key) for ref, key in interrupted] == [("todo-bot", "T1")]
    t1 = (await tasks_by_key(session_factory, project))["T1"]
    assert (t1.status, t1.pause_reason) == (TaskStatus.PAUSED, PauseReason.INTERRUPTED)
    assert await services.sessions.running() == []
    assert await Git(Path(project.repo_path)).is_clean()
    assert notifier.of_type(SystemRestarted)


async def test_manual_release(services: Services) -> None:
    project = await approved_project(services)
    runner, _ = make_runner(services, good_agent)

    assert "Нечего выпускать" in await runner.manual_release(project.id)

    await run_next(services, runner, project)  # M1 выпущена → v0.1.0
    git = Git(Path(project.repo_path))
    await git.checkout("dev")
    (Path(project.repo_path) / "extra.txt").write_text("x")
    await git.commit_all("feat: extra")

    message = await runner.manual_release(project.id)

    assert message == "Выпущено v0.2.0"
    assert await git.ref_exists("refs/tags/v0.2.0")
