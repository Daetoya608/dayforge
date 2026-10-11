"""Инструменты системного агента (docs/mcp-tools.md#системный-агент--mcpsystem)."""

import asyncio
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from dayforge.actions import (
    CalendarChangeError,
    apply_calendar_changes,
    calendar_needs_confirmation,
)
from dayforge.core.enums import (
    AdhocWhen,
    QuestionKind,
    RunMode,
    RunStatus,
    RunTrigger,
)
from dayforge.core.projects import NewProject, ProjectError, suggest_slug
from dayforge.core.questions import QuestionError
from dayforge.db.models import CalendarWeekday, Commit, Milestone, Project, Run, Task
from dayforge.mcp_server.auth import current_session, tool_error
from dayforge.services import Services

MAX_CALENDAR_RANGE = 93
LOG_TEXT_LIMIT = 20_000


class AgentChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["claude_code", "codex"]
    model: str
    effort: str | None = None


class CalendarChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["set_weekdays", "include", "exclude", "day_off", "clear"]
    project: str | None = None
    dates: list[date] = []
    weekdays: Annotated[list[Annotated[int, Field(ge=0, le=6)]], Field(description="0 — пн")] = []


def register(server: MCPServer, services: Services) -> None:
    async def check(ctx: Context) -> int:
        return (await current_session(ctx, services.sessions)).id

    async def project_by_slug(slug: str) -> Project:
        try:
            return await services.projects.get(slug)
        except ProjectError as exc:
            raise tool_error("not_found", str(exc)) from None

    async def forecast_dict(project: Project, today: date) -> dict[str, Any]:
        forecast = await services.calendar.forecast(project, today=today)
        return {
            "end_date": forecast.end_date.isoformat() if forecast.end_date else None,
            "missing_days": forecast.missing_days,
        }

    # --- чтение ---

    @server.tool()
    async def list_projects(ctx: Context, include_archived: bool = False) -> dict[str, Any]:
        """Все проекты: статус, приоритет, день, прогноз окончания, открытые вопросы."""
        await check(ctx)
        today = await services.calendar.today()
        open_questions = await services.questions.open_questions()
        result = []
        for project in await services.projects.list_projects(include_archived=include_archived):
            result.append(
                {
                    "slug": project.slug,
                    "name": project.name,
                    "status": project.status.value,
                    "priority": project.priority,
                    "day": f"{project.current_day}/{project.planned_days}",
                    "forecast": await forecast_dict(project, today),
                    "open_questions": sum(1 for q in open_questions if q.project_id == project.id),
                }
            )
        return {"projects": result}

    @server.tool()
    async def get_project(project: str, ctx: Context) -> dict[str, Any]:
        """Подробно о проекте: настройки, задачи, вехи, последние коммиты, рабочая директория."""
        await check(ctx)
        row = await project_by_slug(project)
        async with services.session_factory() as session:
            tasks = list(
                await session.scalars(
                    select(Task)
                    .where(Task.project_id == row.id)
                    .order_by(Task.day_number, Task.position, Task.id)
                )
            )
            milestones = list(
                await session.scalars(select(Milestone).where(Milestone.project_id == row.id))
            )
            commits = list(
                await session.scalars(
                    select(Commit)
                    .where(Commit.project_id == row.id)
                    .order_by(Commit.committed_at.desc())
                    .limit(10)
                )
            )
        return {
            "slug": row.slug,
            "name": row.name,
            "status": row.status.value,
            "idea": row.idea,
            "priority": row.priority,
            "day": f"{row.current_day}/{row.planned_days}",
            "planner_agent": row.planner_agent,
            "executor_agent": row.executor_agent,
            "test_command": row.test_command,
            "repo_path": row.repo_path,
            "remote_url": row.remote_url,
            "forecast": await forecast_dict(row, await services.calendar.today()),
            "milestones": [
                {"key": m.key, "title": m.title, "version": m.version, "status": m.status.value}
                for m in milestones
            ],
            "tasks": [
                {
                    "key": t.key,
                    "title": t.title,
                    "day": t.day_number,
                    "status": t.status.value,
                    "pause_reason": t.pause_reason.value if t.pause_reason else None,
                }
                for t in tasks
            ],
            "recent_commits": [
                {"sha": c.sha[:7], "message": c.message.splitlines()[0], "branch": c.branch}
                for c in commits
            ],
        }

    @server.tool()
    async def get_calendar(
        ctx: Context, date_from: date | None = None, date_to: date | None = None
    ) -> dict[str, Any]:
        """Календарь за период (до 93 дней): какие проекты в какие даты, общие выходные."""
        await check(ctx)
        today = await services.calendar.today()
        start = date_from or today
        end = date_to or start + timedelta(days=27)
        if end < start or (end - start).days >= MAX_CALENDAR_RANGE:
            raise tool_error("bad_range", f"период — от 1 до {MAX_CALENDAR_RANGE} дней")
        projects = await services.projects.list_projects()
        calendars = {
            p.slug: await services.calendar.project_calendar(p, today=today) for p in projects
        }
        async with services.session_factory() as session:
            used = {
                (run.project_id, run.slot_date)
                for run in await session.scalars(
                    select(Run).where(
                        Run.mode == RunMode.SCHEDULED,
                        Run.slot_date >= start,
                        Run.slot_date <= end,
                        Run.status != RunStatus.CANCELLED,
                    )
                )
            }
            weekdays = list(await session.scalars(select(CalendarWeekday)))
        ids = {p.id: p.slug for p in projects}
        days = []
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            slots = [slug for slug, cal in calendars.items() if cal.is_slot(day)]
            day_off = any(day in cal.days_off for cal in calendars.values())
            if slots or day_off:
                days.append(
                    {
                        "date": day.isoformat(),
                        "projects": slots,
                        "used": [ids[pid] for pid, d in used if d == day and pid in ids],
                        "day_off": day_off,
                    }
                )
        template: dict[str, list[int]] = {}
        for row in weekdays:
            if row.project_id in ids:
                template.setdefault(ids[row.project_id], []).append(row.weekday)
        return {"days": days, "weekdays": {k: sorted(v) for k, v in template.items()}}

    @server.tool()
    async def get_runs(
        ctx: Context, project: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 20
    ) -> dict[str, Any]:
        """История запусков: режим, статус, день, время, ошибки."""
        await check(ctx)
        project_id = (await project_by_slug(project)).id if project else None
        runs = await services.runs.history(project_id, limit)
        return {
            "runs": [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "mode": r.mode.value,
                    "status": r.status.value,
                    "day": r.day_number,
                    "queued_at": r.queued_at.isoformat(),
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "error": r.error,
                }
                for r in runs
            ]
        }

    @server.tool()
    async def get_run_log(
        run_id: int, ctx: Context, tail: Annotated[int, Field(ge=1, le=1000)] = 200
    ) -> dict[str, Any]:
        """Хвост логов сессий агентов запуска — понять, почему что-то упало."""
        await check(ctx)
        sessions = await services.sessions.for_run(run_id)
        logs = []
        for row in sessions:
            lines = await asyncio.to_thread(_tail, Path(row.log_path), tail)
            logs.append(
                {
                    "session_id": row.id,
                    "role": row.role.value,
                    "status": row.status.value,
                    "outcome": row.outcome.value if row.outcome else None,
                    "log": "\n".join(lines)[-LOG_TEXT_LIMIT:],
                }
            )
        return {"sessions": logs}

    @server.tool()
    async def get_pending_questions(ctx: Context) -> dict[str, Any]:
        """Вопросы агентов, на которые пользователь ещё не ответил."""
        await check(ctx)
        result = []
        for question in await services.questions.open_questions():
            project = await services.projects.get_by_id(question.project_id)
            task = await services.tasks.get(question.task_id) if question.task_id else None
            result.append(
                {
                    "question_id": question.id,
                    "project": project.slug,
                    "task": task.key if task else None,
                    "kind": question.kind.value,
                    "payload": question.payload,
                }
            )
        return {"questions": result}

    # --- изменения ---

    @server.tool()
    async def create_project(
        idea: Annotated[str, Field(min_length=1)],
        days: Annotated[int, Field(ge=1, le=365)],
        ctx: Context,
        name: str | None = None,
        slug: str | None = None,
        priority: Annotated[int | None, Field(ge=1, description="Место в очереди дня")] = None,
        stack: str | None = None,
        planner_agent: AgentChoice | None = None,
        executor_agent: AgentChoice | None = None,
        weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = None,
    ) -> dict[str, Any]:
        """Создать проект — тот же процесс, что /new: дальше планировщик задаст вопросы."""
        await check(ctx)
        title = name or idea.strip().splitlines()[0][:60]
        try:
            project = await services.projects.create(
                NewProject(
                    name=title,
                    slug=slug or suggest_slug(title),
                    idea=idea,
                    planned_days=days,
                    stack_hint=stack,
                    priority_position=priority,
                    planner_agent=planner_agent.model_dump() if planner_agent else None,
                    executor_agent=executor_agent.model_dump() if executor_agent else None,
                )
            )
        except ProjectError as exc:
            raise tool_error("invalid_project", str(exc)) from None
        if weekdays:
            await services.calendar.set_weekdays(project.id, weekdays)
        await services.require_orchestrator().start_planning(project.id)
        return {"status": "started", "slug": project.slug}

    @server.tool()
    async def request_replan(
        project: str, instructions: Annotated[str, Field(min_length=1)], ctx: Context
    ) -> dict[str, str]:
        """Перепланирование: планировщик получит инструкции, diff плана придёт пользователю."""
        await check(ctx)
        row = await project_by_slug(project)
        await services.require_orchestrator().start_replan(row.id, instructions)
        return {"status": "started", "message": "Diff плана придёт пользователю на утверждение"}

    @server.tool()
    async def add_task(
        project: str,
        title: Annotated[str, Field(min_length=1, max_length=100)],
        description: Annotated[str, Field(min_length=1)],
        ctx: Context,
        acceptance_criteria: list[str] | None = None,
        when: Literal["now", "next"] = "next",
    ) -> dict[str, Any]:
        """Разовая задача вне плана; выполнит обычный исполнитель по обычному git-процессу."""
        await check(ctx)
        row = await project_by_slug(project)
        task = await services.tasks.add_adhoc(
            row.id,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria or [],
            when=AdhocWhen(when),
        )
        if when == "now":
            await services.runs.enqueue(
                [row.id], mode=RunMode.CONTINUE, trigger=RunTrigger.SYSTEM_AGENT
            )
            services.require_orchestrator().kick_runner()
        return {"status": "done", "task": task.key}

    @server.tool()
    async def update_calendar(changes: list[CalendarChange], ctx: Context) -> dict[str, Any]:
        """Изменить календарь. Смена шаблона или больше 5 дат — после подтверждения."""
        session_id = await check(ctx)
        raw = [c.model_dump(mode="json") for c in changes]
        if calendar_needs_confirmation(raw):
            action_id = await services.actions.create(
                "bulk_calendar",
                {"changes": raw},
                description="Изменить календарь: " + _describe_changes(raw),
                session_id=session_id,
            )
            return {"status": "pending_confirmation", "action_id": action_id}
        try:
            result = await apply_calendar_changes(services, raw)
        except (CalendarChangeError, ProjectError, ValueError) as exc:
            raise tool_error("invalid_change", str(exc)) from None
        return {"status": "done", "message": result}

    @server.tool()
    async def set_priority(order: list[str], ctx: Context) -> dict[str, str]:
        """Порядок проектов в очереди дня: полный список slug неархивных проектов."""
        await check(ctx)
        try:
            await services.projects.set_priority(order)
        except ProjectError as exc:
            raise tool_error("invalid_order", str(exc)) from None
        return {"status": "done"}

    @server.tool()
    async def update_project_settings(
        project: str,
        ctx: Context,
        name: str | None = None,
        badge: str | None = None,
        planner_agent: AgentChoice | None = None,
        executor_agent: AgentChoice | None = None,
    ) -> dict[str, str]:
        """Название, метка, агент/модель/reasoning планировщика и исполнителя."""
        await check(ctx)
        row = await project_by_slug(project)
        await services.projects.update_settings(
            row.id,
            name=name,
            badge=badge,
            planner_agent=planner_agent.model_dump() if planner_agent else None,
            executor_agent=executor_agent.model_dump() if executor_agent else None,
        )
        return {"status": "done"}

    @server.tool()
    async def pause_project(project: str, ctx: Context) -> dict[str, str]:
        """Поставить проект на паузу: запуски по расписанию не выполняются."""
        await check(ctx)
        row = await project_by_slug(project)
        try:
            await services.projects.pause(row.id)
        except ProjectError as exc:
            raise tool_error("invalid_state", str(exc)) from None
        return {"status": "done"}

    @server.tool()
    async def resume_project(project: str, ctx: Context) -> dict[str, str]:
        """Снять проект с паузы."""
        await check(ctx)
        row = await project_by_slug(project)
        has_open = await services.questions.has_open_question(project_id=row.id)
        try:
            await services.projects.resume(row.id, has_open_questions=has_open)
        except ProjectError as exc:
            raise tool_error("invalid_state", str(exc)) from None
        return {"status": "done"}

    @server.tool()
    async def answer_question(question_id: int, answers: list[str], ctx: Context) -> dict[str, Any]:
        """Ответить на вопрос агента. Секреты так не передаются — только пользователем в боте."""
        await check(ctx)
        question = await services.questions.get(question_id)
        if question.kind is QuestionKind.SECRET:
            raise tool_error("secret", "секрет пользователь передаёт сам, ответом в боте")
        try:
            outcome = await services.questions.answer(question_id, answers)
        except QuestionError as exc:
            raise tool_error("invalid_answer", str(exc)) from None
        return {"status": "done", "project_unblocked": outcome.project_unblocked}

    @server.tool()
    async def start_run(
        projects: list[str], mode: Literal["continue", "next"], ctx: Context
    ) -> dict[str, Any]:
        """То же, что /run (continue — доделать) и /next (начать следующий день досрочно)."""
        await check(ctx)
        ids = [(await project_by_slug(slug)).id for slug in projects]
        runs = await services.runs.enqueue(ids, mode=RunMode(mode), trigger=RunTrigger.SYSTEM_AGENT)
        services.require_orchestrator().kick_runner()
        return {"status": "started", "queued": len(runs)}

    async def confirm(
        kind: str, row: Project, description: str, session_id: int, **extra: Any
    ) -> dict[str, Any]:
        action_id = await services.actions.create(
            kind, {"project_id": row.id, **extra}, description=description, session_id=session_id
        )
        return {
            "status": "pending_confirmation",
            "action_id": action_id,
            "message": "Пользователю отправлен запрос на подтверждение",
        }

    @server.tool()
    async def release(project: str, ctx: Context) -> dict[str, Any]:
        """Ручной релиз dev → main с тегом (после подтверждения)."""
        session_id = await check(ctx)
        row = await project_by_slug(project)
        return await confirm("release", row, f"Релиз {row.slug}: dev → main", session_id)

    @server.tool()
    async def archive_project(project: str, ctx: Context) -> dict[str, Any]:
        """Архивировать проект (после подтверждения): скрыт из списков, репозиторий остаётся."""
        session_id = await check(ctx)
        row = await project_by_slug(project)
        return await confirm("archive_project", row, f"Архивировать {row.slug}", session_id)

    @server.tool()
    async def delete_project(
        project: str, ctx: Context, delete_remote: bool = False
    ) -> dict[str, Any]:
        """Удалить проект (после подтверждения).

        Удалённый репозиторий удаляется, только если явно указано delete_remote.
        """
        session_id = await check(ctx)
        row = await project_by_slug(project)
        where = (
            "локальная копия, запись в системе и репозиторий на GitHub"
            if delete_remote
            else ("локальная копия и запись в системе; репозиторий на GitHub остаётся")
        )
        return await confirm(
            "delete_project",
            row,
            f"Удалить проект {row.slug} ({where})",
            session_id,
            delete_remote=delete_remote,
        )


def _tail(path: Path, count: int) -> deque[str]:
    lines: deque[str] = deque(maxlen=count)
    if path.exists():
        with path.open(errors="replace") as file:
            lines.extend(line.rstrip("\n") for line in file)
    return lines


def _describe_changes(changes: list[dict[str, Any]]) -> str:
    parts = []
    for change in changes:
        target = change.get("project") or "все проекты"
        if change["op"] == "set_weekdays":
            names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
            days = ", ".join(names[d] for d in sorted(change.get("weekdays") or [])) or "нет дней"
            parts.append(f"{target}: шаблон недели — {days}")
        else:
            parts.append(f"{target}: {change['op']} {len(change.get('dates') or [])} дат")
    return "; ".join(parts)
