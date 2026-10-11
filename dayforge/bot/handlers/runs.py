"""Запуски вне расписания: /run, /next, «Продолжить сейчас»."""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dayforge.bot.callbacks import ContinueCb, NextCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import h
from dayforge.core.enums import ProjectStatus, RunMode, RunTrigger

router = Router(name="runs")
QUEUEABLE = (ProjectStatus.ACTIVE, ProjectStatus.BLOCKED)
NEXT_SELECTION_KEY = "next_selection:"


async def enqueue_and_report(
    message: Message, app: AppContext, project_ids: list[int], mode: RunMode
) -> None:
    runs = await app.services.runs.enqueue(project_ids, mode=mode, trigger=RunTrigger.BOT)
    if not runs:
        await message.answer("Нечего запускать: проекты на паузе, не утверждены или уже в очереди.")
        return
    app.runner.kick()
    queue = await app.services.runs.queued()
    names = []
    for run in runs:
        project = await app.services.projects.get_by_id(run.project_id)
        names.append(h(project.name))
    position = len(queue) - len(runs) + 1
    busy = " Сейчас работает другой проект — встанут после него." if app.runner.busy else ""
    await message.answer(f"▶ В очереди: {', '.join(names)} (с позиции {position}).{busy}")


@router.message(Command("run"))
async def run_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if command.args:
        project = await app.project(command.args.split()[0])
        if project is None:
            await message.answer("Такого проекта нет.")
            return
        await enqueue_and_report(message, app, [project.id], RunMode.CONTINUE)
        return
    ids = await _with_carry_over(app)
    if not ids:
        await message.answer("Незавершённого нет.")
        return
    await enqueue_and_report(message, app, ids, RunMode.CONTINUE)


async def _with_carry_over(app: AppContext) -> list[int]:
    ids = []
    for project in await app.services.projects.list_projects():
        if project.status in QUEUEABLE and await app.services.tasks.carry_over(project.id):
            ids.append(project.id)
    return ids


@router.message(Command("next"))
async def next_command(message: Message, app: AppContext) -> None:
    projects = [p for p in await app.services.projects.list_projects() if p.status in QUEUEABLE]
    if not projects:
        await message.answer("Нет проектов в работе.")
        return
    sent = await message.answer(
        "Досрочно начать следующий день плана — выбери проекты:",
        reply_markup=await _next_markup(app, set()),
    )
    await app.kv.set(NEXT_SELECTION_KEY + str(sent.message_id), [])


async def _next_markup(app: AppContext, selected: set[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    projects = [p for p in await app.services.projects.list_projects() if p.status in QUEUEABLE]
    for project in projects:
        mark = "☑" if project.id in selected else "☐"
        builder.button(
            text=f"{mark} {project.badge} {project.name}",
            callback_data=NextCb(action="toggle", project_id=project.id),
        )
    builder.button(text="▶ Начать", callback_data=NextCb(action="start", project_id=0))
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(NextCb.filter())
async def next_select(query: CallbackQuery, callback_data: NextCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    key = NEXT_SELECTION_KEY + str(message.message_id)
    selected = set(await app.kv.get(key, []))
    if callback_data.action == "toggle":
        selected ^= {callback_data.project_id}
        await app.kv.set(key, sorted(selected))
        await message.edit_reply_markup(reply_markup=await _next_markup(app, selected))
        await query.answer()
        return
    if not selected:
        await query.answer("Ничего не выбрано", show_alert=True)
        return
    await app.kv.delete(key)
    await message.edit_reply_markup(reply_markup=None)
    await enqueue_and_report(message, app, sorted(selected), RunMode.NEXT)
    await query.answer()


@router.callback_query(ContinueCb.filter())
async def continue_now(query: CallbackQuery, callback_data: ContinueCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    await message.edit_reply_markup(reply_markup=None)
    if not callback_data.now:
        await message.answer("Хорошо — продолжу в ближайший запуск проекта.")
    elif callback_data.project_id:
        await enqueue_and_report(message, app, [callback_data.project_id], RunMode.CONTINUE)
    else:
        ids = await _with_carry_over(app)
        if ids:
            await enqueue_and_report(message, app, ids, RunMode.CONTINUE)
        else:
            await message.answer("Незавершённого нет.")
    await query.answer()
