"""Проекты: /projects, /status, /plan, /report, /pause, /resume, /release, /priority, /replan."""

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dayforge.bot.callbacks import PriorityCb, ProjectCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import h, project_line, project_status, send_text
from dayforge.bot.handlers.runs import enqueue_and_report
from dayforge.core.enums import ProjectStatus, RunMode
from dayforge.core.plans import PlanError
from dayforge.core.projects import ProjectError
from dayforge.db.models import Project
from dayforge.orchestrator import PLANNING_STATUSES

router = Router(name="projects")

RUNNING_STATUSES = (ProjectStatus.ACTIVE, ProjectStatus.BLOCKED, ProjectStatus.PAUSED)


@router.message(Command("projects"))
async def projects(message: Message, app: AppContext) -> None:
    rows = await app.services.projects.list_projects()
    if not rows:
        await message.answer("Проектов пока нет. Создай первый: /new")
        return
    today = await app.services.calendar.today()
    questions = await app.services.questions.open_questions()
    lines = []
    builder = InlineKeyboardBuilder()
    for project in rows:
        forecast = await app.services.calendar.forecast(project, today=today)
        open_count = sum(1 for q in questions if q.project_id == project.id)
        lines.append(project_line(project, forecast, open_count))
        builder.button(
            text=f"{project.badge} {project.name}",
            callback_data=ProjectCb(action="status", project_id=project.id),
        )
    builder.adjust(2)
    await message.answer("\n".join(lines), reply_markup=builder.as_markup())


async def _with_project(
    message: Message, app: AppContext, command: CommandObject, action: str
) -> Project | None:
    """Проект из аргумента команды; без аргумента — кнопки выбора."""
    if command.args:
        project = await app.project(command.args.split()[0])
        if project is None:
            await message.answer(f"Проекта <code>{h(command.args.split()[0])}</code> нет.")
        return project
    markup = await app.project_picker(action)
    await message.answer("Выбери проект:" if markup else "Проектов пока нет.", reply_markup=markup)
    return None


@router.message(Command("status"))
async def status_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if project := await _with_project(message, app, command, "status"):
        await _send_status(message, app, project)


async def _send_status(message: Message, app: AppContext, project: Project) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text="План", callback_data=ProjectCb(action="plan", project_id=project.id))
    builder.button(text="Отчёт", callback_data=ProjectCb(action="report", project_id=project.id))
    if project.status is ProjectStatus.PAUSED:
        builder.button(
            text="▶ Возобновить", callback_data=ProjectCb(action="resume", project_id=project.id)
        )
    elif project.status in (ProjectStatus.ACTIVE, ProjectStatus.BLOCKED):
        builder.button(
            text="⏸ Пауза", callback_data=ProjectCb(action="pause", project_id=project.id)
        )
        builder.button(
            text="⏭ Следующий день", callback_data=ProjectCb(action="next", project_id=project.id)
        )
    builder.adjust(2)
    await send_text(
        message.bot,  # type: ignore[arg-type]
        message.chat.id,
        await project_status(app.services, project),
        reply_markup=builder.as_markup(),
    )


@router.message(Command("plan"))
async def plan_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if project := await _with_project(message, app, command, "plan"):
        await _send_plan(message, app, project)


async def _send_plan(message: Message, app: AppContext, project: Project) -> None:
    try:
        text = await app.services.plans.render(project.id)
    except PlanError:
        await message.answer(f"У {h(project.name)} ещё нет утверждённого плана.")
        return
    goals = "\n".join(
        f"День {day}: {h(goal)}"
        for day, goal in sorted(project.day_goals.items(), key=lambda item: int(item[0]))
    )
    await message.answer_document(
        BufferedInputFile(text.encode(), filename=f"PLAN-{project.slug}.md"),
        caption=f"{project.badge} <b>{h(project.name)}</b>\n{goals}"[:1000],
    )


@router.message(Command("report"))
async def report_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if command.args:
        project = await app.project(command.args.split()[0])
        if project is None:
            await message.answer("Такого проекта нет.")
            return
        await _send_report(message, app, project)
        return
    sent = False
    for project in await app.services.projects.list_projects():
        sent = await _send_report(message, app, project, quiet=True) or sent
    if not sent:
        await message.answer("Отчётов пока нет.")


async def _send_report(
    message: Message, app: AppContext, project: Project, *, quiet: bool = False
) -> bool:
    report = await app.services.reports.latest(project.id)
    if report is None:
        if not quiet:
            await message.answer(f"У {h(project.name)} ещё нет отчётов.")
        return False
    await send_text(message.bot, message.chat.id, report.text)  # type: ignore[arg-type]
    return True


@router.message(Command("pause"))
async def pause_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if project := await _with_project(message, app, command, "pause"):
        await message.answer(await _pause(app, project))


@router.message(Command("resume"))
async def resume_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if project := await _with_project(message, app, command, "resume"):
        await message.answer(await _resume(app, project))


async def _pause(app: AppContext, project: Project) -> str:
    try:
        await app.services.projects.pause(project.id)
    except ProjectError as exc:
        return h(str(exc))
    return f"⏸ {h(project.name)} на паузе — запуски по расписанию не выполняются."


async def _resume(app: AppContext, project: Project) -> str:
    has_open = await app.services.questions.has_open_question(project_id=project.id)
    try:
        await app.services.projects.resume(project.id, has_open_questions=has_open)
    except ProjectError as exc:
        return h(str(exc))
    return f"▶ {h(project.name)} снова в работе."


@router.message(Command("release"))
async def release_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if project := await _with_project(message, app, command, "release"):
        await app.services.actions.create(
            "release", {"project_id": project.id}, description=f"Релиз {project.slug}: dev → main"
        )


@router.message(Command("replan"))
async def replan_command(message: Message, command: CommandObject, app: AppContext) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /replan <i>проект</i> <i>что изменить</i>")
        return
    project = await app.project(parts[0])
    if project is None or project.status not in RUNNING_STATUSES:
        await message.answer("Перепланировать можно только утверждённый незавершённый проект.")
        return
    await app.orchestrator.start_replan(project.id, parts[1])
    note = " (начнётся после текущей задачи)" if app.runner.lock(project.id).locked() else ""
    await message.answer(f"📐 Планировщик получил задачу{note}. Diff плана придёт на утверждение.")


@router.message(Command("priority"))
async def priority_command(message: Message, app: AppContext) -> None:
    text, markup = await _priority_view(app)
    await message.answer(text, reply_markup=markup)


async def _priority_view(app: AppContext):  # type: ignore[no-untyped-def]
    projects = await app.services.projects.list_projects()
    builder = InlineKeyboardBuilder()
    for project in projects:
        builder.button(
            text=f"{project.priority}. {project.badge} {project.name}", callback_data="noop"
        )
        builder.button(text="↑", callback_data=PriorityCb(project_id=project.id, up=True))
        builder.button(text="↓", callback_data=PriorityCb(project_id=project.id, up=False))
    builder.adjust(*([3] * len(projects)))
    return "Порядок проектов в очереди дня:", builder.as_markup()


@router.callback_query(PriorityCb.filter())
async def priority_move(query: CallbackQuery, callback_data: PriorityCb, app: AppContext) -> None:
    order = [p.slug for p in await app.services.projects.list_projects()]
    project = await app.services.projects.get_by_id(callback_data.project_id)
    index = order.index(project.slug)
    target = index - 1 if callback_data.up else index + 1
    if 0 <= target < len(order):
        order[index], order[target] = order[target], order[index]
        await app.services.projects.set_priority(order)
    text, markup = await _priority_view(app)
    await query.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await query.answer()


@router.callback_query(F.data == "noop")
async def noop(query: CallbackQuery) -> None:
    await query.answer()


@router.callback_query(ProjectCb.filter())
async def project_action(query: CallbackQuery, callback_data: ProjectCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    project = await app.services.projects.get_by_id(callback_data.project_id)
    match callback_data.action:
        case "status":
            await _send_status(message, app, project)
        case "plan":
            await _send_plan(message, app, project)
        case "report":
            await _send_report(message, app, project)
        case "pause":
            await message.answer(await _pause(app, project))
        case "resume":
            await message.answer(await _resume(app, project))
        case "release":
            await app.services.actions.create(
                "release",
                {"project_id": project.id},
                description=f"Релиз {project.slug}: dev → main",
            )
        case "next":
            await enqueue_and_report(message, app, [project.id], RunMode.NEXT)
        case "replan_restart":
            if project.status in PLANNING_STATUSES:
                await app.orchestrator.start_planning(project.id)
                await message.answer(f"📐 Планирование {h(project.name)} запущено заново.")
            else:
                await message.answer("Проект уже не в планировании.")
    await query.answer()
