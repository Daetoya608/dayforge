"""Согласование плана, невыполнимые задачи, предложения агента изменить план."""

from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from dayforge.bot.callbacks import FailedTaskCb, PlanCb, ProposalCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import forecast_text, h
from dayforge.bot.notifier import PROPOSAL_KEY_PREFIX
from dayforge.core.enums import ProjectStatus, TaskStatus
from dayforge.core.plans import PlanError
from dayforge.core.tasks import TaskError
from dayforge.db.models import Plan

router = Router(name="planning")


class PlanFeedback(StatesGroup):
    text = State()


@router.callback_query(PlanCb.filter())
async def plan_action(
    query: CallbackQuery, callback_data: PlanCb, app: AppContext, state: FSMContext
) -> None:
    message = query.message
    assert isinstance(message, Message)
    plan_id = callback_data.plan_id
    match callback_data.action:
        case "file":
            try:
                text = await app.services.plans.render_proposed(plan_id)
            except PlanError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await message.answer_document(BufferedInputFile(text.encode(), filename="PLAN.md"))
        case "arch":
            async with app.services.session_factory() as session:
                plan = await session.get(Plan, plan_id)
            project = await app.services.projects.get_by_id(plan.project_id) if plan else None
            path = Path(project.repo_path) / "docs" / "ARCHITECTURE.md" if project else None
            if path is None or not path.exists():
                await query.answer("ARCHITECTURE.md ещё нет", show_alert=True)
                return
            await message.answer_document(
                BufferedInputFile(path.read_bytes(), filename="ARCHITECTURE.md")
            )
        case "approve":
            try:
                project = await app.orchestrator.approve_plan(plan_id)
            except ValueError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await message.edit_reply_markup(reply_markup=None)
            forecast = await app.services.calendar.forecast(
                project, today=await app.services.calendar.today()
            )
            await message.answer(
                f"✅ План {h(project.name)} утверждён. "
                f"Прогноз окончания: {forecast_text(forecast)}."
            )
        case "edit":
            await state.set_state(PlanFeedback.text)
            await state.update_data(plan_id=plan_id)
            await message.answer("Напиши, что поменять в плане — передам планировщику.")
        case "reject":
            try:
                project = await app.orchestrator.reject_plan(plan_id)
            except ValueError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await message.edit_reply_markup(reply_markup=None)
            if project.status is ProjectStatus.ARCHIVED:
                await message.answer(f"✖ План отклонён, проект {h(project.name)} архивирован.")
            else:
                await message.answer("✖ Перепланирование отменено, действует прежний план.")
    await query.answer()


@router.message(PlanFeedback.text, F.text, ~F.text.startswith("/"))
async def plan_feedback(message: Message, app: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        await app.orchestrator.send_plan_feedback(data["plan_id"], message.text or "")
    except ValueError as exc:
        await message.answer(h(str(exc)))
        return
    await message.answer("✏ Правки переданы планировщику — новый план придёт сюда.")


@router.callback_query(FailedTaskCb.filter())
async def failed_task(query: CallbackQuery, callback_data: FailedTaskCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    task = await app.services.tasks.get(callback_data.task_id)
    try:
        match callback_data.action:
            case "replan":
                instructions = (
                    f"Задача {task.key} «{task.title}» не может быть выполнена: {task.summary}"
                )
                await app.orchestrator.start_replan(task.project_id, instructions)
                reply = "📐 Передал планировщику — diff плана придёт на утверждение."
            case "retry":
                await app.services.tasks.set_status(task.id, TaskStatus.PENDING)
                reply = f"{task.key} вернётся в работу в ближайший запуск."
            case _:
                await app.services.tasks.set_status(task.id, TaskStatus.CANCELLED)
                reply = f"{task.key} отменена."
    except TaskError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await message.edit_reply_markup(reply_markup=None)
    await message.answer(reply)
    await query.answer()


@router.callback_query(ProposalCb.filter())
async def proposal(query: CallbackQuery, callback_data: ProposalCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    key = PROPOSAL_KEY_PREFIX + callback_data.key
    stored = await app.kv.get(key)
    await app.kv.delete(key)
    await message.edit_reply_markup(reply_markup=None)
    if stored is None:
        await query.answer("Предложение уже обработано", show_alert=True)
        return
    if callback_data.action == "replan":
        await app.orchestrator.start_replan(stored["project_id"], stored["text"])
        await message.answer("📐 Передал планировщику — diff плана придёт на утверждение.")
    else:
        await message.answer("Предложение отклонено.")
    await query.answer()
