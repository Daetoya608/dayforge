"""Раннер: очередь запусков, запуск проекта, выполнение задачи, шлюз (docs/run-lifecycle.md)."""

import asyncio
import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from dayforge import prompts
from dayforge.agents.launcher import AgentLauncher, LaunchResult
from dayforge.core.calendar import Forecast
from dayforge.core.enums import (
    AgentRole,
    CommitKind,
    DayTrigger,
    PauseReason,
    ProjectStatus,
    QuestionKind,
    RunMode,
    RunStatus,
    RunTrigger,
    SessionOutcome,
    SessionStatus,
    TaskStatus,
)
from dayforge.core.events import (
    LimitReached,
    MilestoneReleased,
    ProjectCompleted,
    ProjectRef,
    ProjectSkipped,
    RunFailed,
    RunReport,
    SystemRestarted,
    TaskPaused,
)
from dayforge.core.plans import PlanError
from dayforge.core.reports import SHORT_SHA, DoneTask, Link, OpenTask, RunSummary
from dayforge.core.secrets import EnvFile
from dayforge.db.base import utcnow
from dayforge.db.models import Project, Run, Task
from dayforge.runner import gitops
from dayforge.services import Services
from dayforge.vcs.git import Git, MergeConflict

log = logging.getLogger(__name__)

PAUSE_REASON_TEXT = {
    PauseReason.QUESTION: "ждёт ответа на вопрос",
    PauseReason.SECRET: "ждёт секрета",
    PauseReason.LIMIT: "лимит подписки",
    PauseReason.WATCHDOG: "агент завис — сессия прервана",
    PauseReason.TESTS_FAILED: "тесты не прошли",
    PauseReason.NO_RESULT: "агент не сообщил результат",
    PauseReason.ERROR: "ошибка",
    PauseReason.INTERRUPTED: "прервана перезапуском",
}


class TaskResult(StrEnum):
    DONE = "done"
    BLOCKED = "blocked"
    LIMIT = "limit"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass
class _Collected:
    """Что произошло за запуск проекта — для отчёта."""

    done: list[DoneTask] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    releases: list[Link] = field(default_factory=list)
    tests_green: bool | None = None
    notes: list[str] = field(default_factory=list)
    limit_resets_at: datetime | None = None


class Runner:
    def __init__(
        self,
        services: Services,
        launcher: AgentLauncher,
        *,
        agent_unix_user: str | None = None,
    ) -> None:
        self._s = services
        self._launcher = launcher
        self._agent_unix_user = agent_unix_user
        self._wake = asyncio.Event()
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._worker: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()

    # --- очередь ---

    def lock(self, project_id: int) -> asyncio.Lock:
        """Блокировка рабочей директории проекта: исполнитель, планировщик, PLAN.md, релиз."""
        return self._locks[project_id]

    def kick(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self._worker = asyncio.create_task(self.run_forever(), name="runner")

    async def stop(self) -> None:
        for task in [self._worker, *self._background]:
            if task is not None:
                task.cancel()
        for task in [self._worker, *self._background]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @property
    def busy(self) -> bool:
        return any(lock.locked() for lock in self._locks.values())

    async def run_forever(self) -> None:
        while True:
            run = await self._s.runs.next_queued()
            if run is None:
                await self._wake.wait()
                self._wake.clear()
                continue
            await self.execute_run(run)

    async def drain(self) -> None:
        """Выполнить всё, что стоит в очереди (для тестов и ручного запуска)."""
        while (run := await self._s.runs.next_queued()) is not None:
            await self.execute_run(run)

    async def execute_run(self, run: Run) -> RunStatus:
        run = await self._s.runs.start(run.id)
        project = await self._s.projects.get_by_id(run.project_id)
        try:
            async with self.lock(project.id):
                status, day = await self._run_project(run, project)
        except asyncio.CancelledError:
            await self._s.runs.finish(run.id, RunStatus.CANCELLED)
            raise
        except Exception as exc:
            log.exception("run %s of %s failed", run.id, project.slug)
            await self._s.runs.finish(run.id, RunStatus.ERROR, error=str(exc)[:2000])
            await self._s.notifier.send(
                RunFailed(project=ProjectRef.of(project), run_id=run.id, error=str(exc)[:500])
            )
            return RunStatus.ERROR
        await self._s.runs.finish(run.id, status, day_number=day)
        return status

    # --- запуск проекта ---

    async def _run_project(self, run: Run, project: Project) -> tuple[RunStatus, int | None]:
        ref = ProjectRef.of(project)
        if project.status is ProjectStatus.BLOCKED:
            reason = "ждёт ответа на вопрос"
            if run.mode is not RunMode.SCHEDULED:
                await self._s.notifier.send(ProjectSkipped(project=ref, reason=reason))
                return RunStatus.SKIPPED, None
            # Пропуск слота — это и напоминание о вопросе, но не чаще question_reminder_hours.
            every = timedelta(hours=await self._s.settings.get("question_reminder_hours"))
            due = await self._s.questions.due_reminders(now=utcnow(), every=every)
            if any(q.project_id == project.id for q in due):
                reason += " — слот пропущен, прогноз сдвинется"
                await self._s.notifier.send(ProjectSkipped(project=ref, reason=reason))
            return RunStatus.SKIPPED, None
        if project.status is not ProjectStatus.ACTIVE:
            return RunStatus.CANCELLED, None

        today = await self._s.calendar.today()
        previous_forecast = await self._s.calendar.forecast(project, today=today)
        git = Git(Path(project.repo_path))
        collected = _Collected()
        await gitops.ensure_on_dev(git)
        await self._publish(project, collected)

        queue = await self._s.tasks.carry_over(project.id)
        day_number = None
        if run.mode in (RunMode.SCHEDULED, RunMode.NEXT):
            trigger = DayTrigger.SCHEDULED if run.mode is RunMode.SCHEDULED else DayTrigger.NEXT
            started = await self._s.tasks.start_next_day(project.id, trigger=trigger, today=today)
            if started is not None:
                day_number, day_tasks = started
                seen = {t.id for t in queue}
                queue += [t for t in day_tasks if t.id not in seen]

        status = RunStatus.FINISHED
        for queued in queue:
            task = await self._s.tasks.get(queued.id)
            if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED):
                continue
            if await self._s.tasks.depends_unfinished(task):
                continue
            result = await self._execute_task(run, project, task, collected)
            if result is TaskResult.BLOCKED:
                status = RunStatus.BLOCKED
                break
            if result is TaskResult.LIMIT:
                status = RunStatus.LIMIT
                break

        await gitops.ensure_on_dev(git)
        await self._s.tasks.close_finished_days(project.id)
        await self._sync_plan_file(project, git)
        await self._push(project, collected, "dev")
        if await self._s.tasks.is_project_finished(project.id):
            await self._complete_project(project, git, collected)

        await self._report(run, project, queue, collected, day_number, previous_forecast, status)
        if status is RunStatus.LIMIT:
            await self._handle_limit(project, collected.limit_resets_at)
        return status, day_number

    async def _complete_project(self, project: Project, git: Git, collected: _Collected) -> None:
        for milestone in await self._s.releases.completed_milestones(project.id):
            await self._release_milestone(project, git, milestone, collected)
        await self._s.projects.set_status(project.id, ProjectStatus.COMPLETED)
        async with self._s.session_factory() as session:
            done = await session.scalar(
                select(func.count()).where(
                    Task.project_id == project.id, Task.status == TaskStatus.DONE
                )
            )
        project = await self._s.projects.get_by_id(project.id)
        await self._s.notifier.send(
            ProjectCompleted(
                project=ProjectRef.of(project),
                url=self._web_url(project),
                days=project.current_day,
                tasks_done=int(done or 0),
                versions=[link.text for link in collected.releases],
            )
        )

    async def _handle_limit(self, project: Project, resets_at: datetime | None) -> None:
        cancelled = await self._s.runs.cancel_queued()
        auto = bool(await self._s.settings.get("limit_auto_resume")) and resets_at is not None
        refs = []
        for run in cancelled:
            other = await self._s.projects.get_by_id(run.project_id)
            refs.append(ProjectRef.of(other))
        await self._s.notifier.send(
            LimitReached(
                project=ProjectRef.of(project),
                cancelled=refs,
                resets_at=resets_at,
                auto_resume=auto,
            )
        )
        if auto and resets_at is not None:
            task = asyncio.create_task(self._resume_after_limit(resets_at, project, cancelled))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _resume_after_limit(
        self, resets_at: datetime, project: Project, cancelled: list[Run]
    ) -> None:
        await asyncio.sleep(max(0.0, (resets_at - utcnow()).total_seconds()) + 60)
        await self._s.runs.enqueue(
            [project.id], mode=RunMode.CONTINUE, trigger=RunTrigger.AUTO_RESUME
        )
        for run in cancelled:
            await self._s.runs.enqueue(
                [run.project_id],
                mode=run.mode,
                trigger=RunTrigger.AUTO_RESUME,
                slot_date=run.slot_date,
            )
        self.kick()

    # --- задача ---

    async def _execute_task(
        self, run: Run, project: Project, task: Task, collected: _Collected
    ) -> TaskResult:
        git = Git(Path(project.repo_path))
        branch = task.branch or gitops.branch_name(task.key, task.title)
        await gitops.checkout_task_branch(git, branch)
        prompt, resume_session_id = await self._task_prompt(project, task, branch)
        task = await self._s.tasks.mark_running(task.id, branch=branch)
        refs_before = {ref: await git.rev_parse(ref) for ref in ("dev", "main")}

        try:
            launch = await self._launch(project, task, run, prompt, resume_session_id)
            nudged = fixes = conflicts = 0
            while True:
                outcome = launch.outcome
                if outcome is SessionOutcome.COMPLETED:
                    gate = await self._gate(project, task, git, branch, refs_before, collected)
                    if gate.ok:
                        return TaskResult.DONE
                    if gate.kind == "tests" and fixes == 0:
                        fixes += 1
                        await self._s.tasks.back_to_running(task.id)
                        text = prompts.render(
                            "executor_fix_tests.md",
                            test_command=project.test_command,
                            output=gate.details,
                        )
                        launch = await self._launch(project, task, run, text, launch.session_id)
                        continue
                    if gate.kind == "conflict" and conflicts == 0:
                        conflicts += 1
                        await self._s.tasks.back_to_running(task.id)
                        text = prompts.render(
                            "executor_merge_conflict.md", files=gate.details.splitlines()
                        )
                        launch = await self._launch(project, task, run, text, launch.session_id)
                        continue
                    reason = PauseReason.TESTS_FAILED if gate.kind == "tests" else PauseReason.ERROR
                    return await self._pause(project, task, git, reason, gate.details)
                if outcome in (SessionOutcome.QUESTION, SessionOutcome.SECRET):
                    await gitops.save_wip(git, task.key, "ждёт ответа")
                    await self._push(project, collected, branch)
                    return TaskResult.BLOCKED
                if outcome is SessionOutcome.FAILED:
                    await gitops.save_wip(git, task.key, "не выполнена")
                    await self._push(project, collected, branch)
                    return TaskResult.FAILED
                if outcome is SessionOutcome.LIMIT:
                    collected.limit_resets_at = launch.result.limit_resets_at
                    await self._pause(project, task, git, PauseReason.LIMIT, None, notify=False)
                    return TaskResult.LIMIT
                if outcome is SessionOutcome.WATCHDOG:
                    return await self._pause(project, task, git, PauseReason.WATCHDOG, None)
                if outcome is None and not nudged:
                    nudged += 1
                    text = prompts.render("executor_need_result.md")
                    launch = await self._launch(project, task, run, text, launch.session_id)
                    continue
                reason = PauseReason.NO_RESULT if outcome is None else PauseReason.ERROR
                return await self._pause(project, task, git, reason, None)
        except asyncio.CancelledError:
            await self._s.tasks.pause(task.id, PauseReason.INTERRUPTED)
            with contextlib.suppress(Exception):
                await gitops.save_wip(git, task.key, "прервана перезапуском")
            raise

    async def _task_prompt(
        self, project: Project, task: Task, branch: str
    ) -> tuple[str, int | None]:
        previous = await self._s.sessions.latest_for_task(task.id)
        can_resume = previous is not None and previous.cli_session_id is not None
        if task.status is TaskStatus.PAUSED and task.pause_reason in (
            PauseReason.QUESTION,
            PauseReason.SECRET,
        ):
            question = await self._s.questions.latest_answered(task.id)
            if question is not None:
                text = _resume_text(question.kind, question.payload, question.answers or [])
                if can_resume:
                    return text, previous.id  # type: ignore[union-attr]
                return _task_text(project, task, branch, restarted=True) + "\n\n" + text, None
        if task.status is TaskStatus.PAUSED and can_resume:
            reason = PAUSE_REASON_TEXT.get(task.pause_reason, "пауза")  # type: ignore[arg-type]
            return prompts.render("executor_continue.md", reason=reason), previous.id  # type: ignore[union-attr]
        return _task_text(project, task, branch, restarted=task.attempts > 0), None

    async def _launch(
        self,
        project: Project,
        task: Task,
        run: Run,
        prompt: str,
        resume_session_id: int | None,
    ) -> LaunchResult:
        return await self._launcher.launch(
            role=AgentRole.EXECUTOR,
            agent=project.executor_agent,
            cwd=Path(project.repo_path),
            prompt=prompt,
            system_prompt=prompts.render("executor_system.md"),
            project_id=project.id,
            task_id=task.id,
            run_id=run.id,
            resume_session_id=resume_session_id,
            mask_values=tuple(EnvFile(Path(project.repo_path) / ".env").values()),
        )

    async def _pause(
        self,
        project: Project,
        task: Task,
        git: Git,
        reason: PauseReason,
        details: str | None,
        *,
        notify: bool = True,
    ) -> TaskResult:
        await self._s.tasks.pause(task.id, reason)
        await gitops.save_wip(git, task.key, PAUSE_REASON_TEXT[reason])
        with contextlib.suppress(Exception):
            await self._s.repos.push(git.cwd, await git.current_branch())
        if notify:
            await self._s.notifier.send(
                TaskPaused(
                    project=ProjectRef.of(project),
                    task_key=task.key,
                    reason=PAUSE_REASON_TEXT[reason],
                    details=(details or "")[-3000:] or None,
                )
            )
        return TaskResult.PAUSED

    # --- шлюз ---

    async def _gate(
        self,
        project: Project,
        task: Task,
        git: Git,
        branch: str,
        refs_before: dict[str, str],
        collected: _Collected,
    ) -> "_Gate":
        # 1. Агент не трогал dev и main и остался в своей ветке.
        changed = [ref for ref, sha in refs_before.items() if await git.rev_parse(ref) != sha]
        current = await git.current_branch()
        if changed or current != branch:
            for ref, sha in refs_before.items():
                await git.run("update-ref", f"refs/heads/{ref}", sha)
            await git.checkout(branch)
            return _Gate(False, "refs", f"агент изменил {', '.join(changed) or 'текущую ветку'}")

        # 2. Незакоммиченное — в WIP: это не ошибка, но в отчёте видно.
        if await gitops.save_wip(git, task.key, "незакоммиченные изменения"):
            collected.notes.append(f"{task.key}: агент не закоммитил часть изменений")

        # 3. dev ушёл вперёд — сливаем в ветку; конфликт разрешает агент.
        if not await gitops.is_ancestor(git, "dev", branch):
            try:
                await git.merge("dev", message=f"merge dev into {branch}", abort_on_conflict=False)
            except MergeConflict as exc:
                return _Gate(False, "conflict", "\n".join(exc.files))

        # 4. Тесты.
        timeout = 60.0 * await self._s.settings.get("test_timeout_minutes")
        tests = await gitops.run_tests(
            git.cwd,
            project.test_command or "true",
            agent_unix_user=self._agent_unix_user,
            limit_seconds=timeout,
        )
        collected.tests_green = tests.passed
        if not tests.passed:
            return _Gate(False, "tests", tests.output)

        # 5–7. Мерж в dev, учёт коммитов, PLAN.md, push, удаление ветки.
        commits = await git.commits(f"dev..{branch}")
        await git.checkout("dev")
        merge_sha = await git.merge(branch, message=f"merge({task.key}): {task.title}")
        await self._s.releases.record_commits(
            project.id, commits, branch=branch, kind=CommitKind.AGENT, task_id=task.id
        )
        await self._s.releases.record_commits(
            project.id,
            await git.commits(f"{merge_sha}~1..{merge_sha}"),
            branch="dev",
            kind=CommitKind.SYSTEM,
            task_id=task.id,
        )
        collected.commits += [c.sha for c in commits]
        await self._s.tasks.mark_done(task.id, merge_sha=merge_sha)
        task = await self._s.tasks.get(task.id)
        collected.done.append(
            DoneTask(
                key=task.key, title=task.title, summary=task.summary or "", decisions=task.decisions
            )
        )
        await self._sync_plan_file(project, git)
        await self._push(project, collected, "dev")
        await git.delete_branch(branch)
        with contextlib.suppress(Exception):
            await self._s.repos.delete_remote_branch(git.cwd, branch)

        # 8. Веха выполнена — релиз.
        for milestone in await self._s.releases.completed_milestones(project.id):
            await self._release_milestone(project, git, milestone, collected)
        return _Gate(True, "ok", "")

    # --- релизы ---

    async def _release_milestone(
        self, project: Project, git: Git, milestone: Any, collected: _Collected
    ) -> None:
        tasks = await self._s.releases.milestone_tasks(project.id, milestone.key)
        notes = "\n".join(f"- {t.key} {t.title}: {t.summary or ''}".rstrip(": ") for t in tasks)
        link = await self._release(
            project,
            git,
            tag=milestone.version,
            title=milestone.title,
            notes=notes,
            milestone_id=milestone.id,
        )
        collected.releases.append(link)

    async def _release(
        self,
        project: Project,
        git: Git,
        *,
        tag: str,
        title: str,
        notes: str,
        milestone_id: int | None,
    ) -> Link:
        await gitops.ensure_on_dev(git)
        await git.checkout("main")
        sha = await git.merge("dev", message=f"chore: release {tag}")
        await git.tag(tag, f"{title}\n\n{notes}".strip())
        await git.checkout("dev")
        url = None
        try:
            await self._s.repos.push(git.cwd, "main", tags=True)
            provider = self._s.repos.provider
            refreshed = await self._s.projects.get_by_id(project.id)
            if provider is not None and refreshed.remote_url:
                repo = provider.repo_from_remote(refreshed.remote_url)
                url = await provider.create_release(repo, tag, f"{title}\n\n{notes}".strip())
        except Exception as exc:
            log.warning("release %s of %s not published: %s", tag, project.slug, exc)
        await self._s.releases.record_release(
            project.id, tag=tag, sha=sha, milestone_id=milestone_id
        )
        await self._s.notifier.send(
            MilestoneReleased(project=ProjectRef.of(project), tag=tag, title=title, url=url)
        )
        return Link(text=tag, url=url)

    async def manual_release(self, project_id: int) -> str:
        async with self.lock(project_id):
            project = await self._s.projects.get_by_id(project_id)
            git = Git(Path(project.repo_path))
            await gitops.ensure_on_dev(git)
            if await gitops.is_ancestor(git, "dev", "main"):
                return "Нечего выпускать: в dev нет изменений после последнего релиза"
            tag = await self._s.releases.next_manual_version(project_id)
            link = await self._release(
                project, git, tag=tag, title=f"Релиз {tag}", notes="Ручной релиз", milestone_id=None
            )
            return f"Выпущено {link.text}" + (f": {link.url}" if link.url else "")

    # --- PLAN.md и публикация ---

    async def sync_plan_file(self, project_id: int) -> None:
        """PLAN.md после перепланирования — под блокировкой, когда не идёт задача."""
        async with self.lock(project_id):
            project = await self._s.projects.get_by_id(project_id)
            git = Git(Path(project.repo_path))
            await gitops.ensure_on_dev(git)
            await self._sync_plan_file(project, git)
            with contextlib.suppress(Exception):
                await self._s.repos.push(git.cwd, "dev")

    async def _sync_plan_file(self, project: Project, git: Git) -> None:
        try:
            text = await self._s.plans.render(project.id)
        except PlanError:
            return
        if await git.current_branch() != "dev":
            return
        await gitops.write_plan_file(git, text)

    async def _publish(self, project: Project, collected: _Collected) -> None:
        if self._s.repos.provider is None:
            return
        try:
            await self._s.repos.publish(project.id)
        except Exception as exc:
            log.warning("publish %s failed: %s", project.slug, exc)
            collected.notes.append(f"Репозиторий не опубликован: {exc}")

    async def _push(self, project: Project, collected: _Collected, *refs: str) -> None:
        try:
            await self._s.repos.push(Path(project.repo_path), *refs)
        except Exception as exc:
            log.warning("push %s %s failed: %s", project.slug, refs, exc)
            note = "push на GitHub не удался — повторится позже"
            if note not in collected.notes:
                collected.notes.append(note)

    def _web_url(self, project: Project) -> str | None:
        provider = self._s.repos.provider
        if provider is None or not project.remote_url:
            return None
        return provider.web_url(provider.repo_from_remote(project.remote_url))

    # --- отчёт ---

    async def _report(
        self,
        run: Run,
        project: Project,
        queue: list[Task],
        collected: _Collected,
        day_number: int | None,
        previous_forecast: Forecast,
        status: RunStatus,
    ) -> None:
        project = await self._s.projects.get_by_id(project.id)
        open_tasks = []
        for queued in queue:
            task = await self._s.tasks.get(queued.id)
            if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
                continue
            if task.status is TaskStatus.PAUSED:
                reason = PAUSE_REASON_TEXT.get(task.pause_reason, "пауза")  # type: ignore[arg-type]
            elif task.status is TaskStatus.FAILED:
                reason = "не может быть выполнена"
            elif await self._s.tasks.depends_unfinished(task):
                reason = "ждёт зависимостей"
            else:
                reason = "не дошла очередь"
            open_tasks.append(OpenTask(key=task.key, title=task.title, reason=reason))

        commit_links = [self._commit_link(project, sha) for sha in collected.commits]
        summary = RunSummary(
            run_id=run.id,
            project=ProjectRef.of(project),
            date=await self._s.calendar.today(),
            status=status,
            day_number=day_number,
            planned_days=project.planned_days,
            done=collected.done,
            open=open_tasks,
            commits=commit_links,
            releases=collected.releases,
            tests_green=collected.tests_green,
            forecast=await self._s.calendar.forecast(project, today=await self._s.calendar.today()),
            previous_forecast=previous_forecast,
            note="; ".join(collected.notes) or None,
        )
        report = await self._s.reports.store(summary)
        await self._s.notifier.send(
            RunReport(project=summary.project, run_id=run.id, text=report.text)
        )

    def _commit_link(self, project: Project, sha: str) -> Link:
        provider = self._s.repos.provider
        url = None
        if provider is not None and project.remote_url:
            url = provider.commit_url(provider.repo_from_remote(project.remote_url), sha)
        return Link(text=sha[:SHORT_SHA], url=url)

    # --- восстановление после рестарта ---

    async def recover(self) -> list[tuple[ProjectRef, str]]:
        """docs/architecture.md#восстановление-после-рестарта."""
        interrupted: list[tuple[ProjectRef, str]] = []
        for row in await self._s.sessions.running():
            await self._s.sessions.end(row.id, SessionStatus.CRASHED)
        async with self._s.session_factory() as session:
            stuck = list(
                await session.scalars(
                    select(Task).where(Task.status.in_((TaskStatus.RUNNING, TaskStatus.VERIFYING)))
                )
            )
            runs = list(
                await session.scalars(
                    select(Run).where(Run.status.in_((RunStatus.RUNNING, RunStatus.QUEUED)))
                )
            )
        for run in runs:
            await self._s.runs.finish(run.id, RunStatus.CANCELLED)
        for task in stuck:
            await self._s.tasks.pause(task.id, PauseReason.INTERRUPTED)
            project = await self._s.projects.get_by_id(task.project_id)
            with contextlib.suppress(Exception):
                git = Git(Path(project.repo_path))
                if await git.merge_in_progress():
                    await git.run("merge", "--abort")
                await gitops.save_wip(git, task.key, "прервана перезапуском")
            interrupted.append((ProjectRef.of(project), task.key))
        if interrupted:
            await self._s.notifier.send(SystemRestarted(interrupted=interrupted))
        return interrupted


@dataclass(frozen=True)
class _Gate:
    ok: bool
    kind: str  # ok, refs, conflict, tests
    details: str


def _task_text(project: Project, task: Task, branch: str, *, restarted: bool) -> str:
    return prompts.render(
        "executor_task.md",
        key=task.key,
        title=task.title,
        day=task.day_number,
        description=task.description,
        criteria=task.acceptance_criteria,
        depends_on=task.depends_on,
        branch=branch,
        test_command=project.test_command or "—",
        restarted=restarted,
    )


def _resume_text(kind: QuestionKind, payload: dict[str, Any], answers: list[str]) -> str:
    if kind is QuestionKind.SECRET:
        return prompts.render("executor_resume.md", secret_name=payload["name"], answers=[])
    items = [
        {"question": q["text"], "answer": a}
        for q, a in zip(payload.get("questions", []), answers, strict=False)
    ]
    return prompts.render("executor_resume.md", secret_name=None, answers=items)
