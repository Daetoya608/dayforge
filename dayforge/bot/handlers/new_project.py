"""/new — диалог создания проекта (docs/telegram-bot.md#new)."""

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dayforge.bot.callbacks import NewCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import WEEKDAYS, h
from dayforge.bot.handlers.settings import parse_agent
from dayforge.core.projects import NewProject, ProjectError, suggest_slug

router = Router(name="new_project")


class NewProjectForm(StatesGroup):
    idea = State()
    stack = State()
    days = State()
    priority = State()
    agents = State()
    weekdays = State()
    name = State()
    confirm = State()


def _button(text: str, action: str, value: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=NewCb(action=action, value=value))
    return builder.as_markup()


@router.message(Command("new"))
async def new_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewProjectForm.idea)
    await state.update_data(idea=[])
    await message.answer(
        "Опиши идею проекта — можно несколькими сообщениями. Когда закончишь — «Готово».\n"
        "<i>/cancel — отменить</i>",
        reply_markup=_button("Готово", "idea_done"),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(NewProjectForm.idea, F.text, ~F.text.startswith("/"))
async def idea_part(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(idea=[*data["idea"], message.text or ""])


@router.callback_query(NewCb.filter(F.action == "idea_done"), NewProjectForm.idea)
async def idea_done(query: CallbackQuery, state: FSMContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    if not (await state.get_data())["idea"]:
        await query.answer("Сначала опиши идею", show_alert=True)
        return
    await state.set_state(NewProjectForm.stack)
    await message.answer(
        "Пожелания по стеку? Напиши их или доверь выбор агенту.",
        reply_markup=_button("Пусть выберет агент", "stack_auto"),
    )
    await query.answer()


@router.message(NewProjectForm.stack, F.text, ~F.text.startswith("/"))
async def stack_text(message: Message, state: FSMContext) -> None:
    await state.update_data(stack=message.text)
    await _ask_days(message, state)


@router.callback_query(NewCb.filter(F.action == "stack_auto"), NewProjectForm.stack)
async def stack_auto(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(stack=None)
    assert isinstance(query.message, Message)
    await _ask_days(query.message, state)
    await query.answer()


async def _ask_days(message: Message, state: FSMContext) -> None:
    await state.set_state(NewProjectForm.days)
    await message.answer("За сколько рабочих дней сделать проект? Число.")


@router.message(NewProjectForm.days, F.text, ~F.text.startswith("/"))
async def days(message: Message, state: FSMContext, app: AppContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= 365:
        await message.answer("Нужно число от 1 до 365.")
        return
    await state.update_data(days=int(text))
    await state.set_state(NewProjectForm.priority)
    builder = InlineKeyboardBuilder()
    builder.button(text="Первым", callback_data=NewCb(action="prio", value=1))
    builder.button(text="Последним", callback_data=NewCb(action="prio", value=0))
    for project in await app.services.projects.list_projects():
        builder.button(
            text=f"После {project.badge} {project.name}",
            callback_data=NewCb(action="prio", value=project.priority + 1),
        )
    builder.adjust(2, 1)
    await message.answer("Место в очереди дня?", reply_markup=builder.as_markup())


@router.callback_query(NewCb.filter(F.action == "prio"), NewProjectForm.priority)
async def priority(query: CallbackQuery, callback_data: NewCb, state: FSMContext) -> None:
    await state.update_data(priority=callback_data.value or None)
    await state.set_state(NewProjectForm.agents)
    assert isinstance(query.message, Message)
    await query.message.answer(
        "Агенты: по умолчанию из /settings или свои — две строки (планировщик, исполнитель):\n"
        "<code>claude_code opus high\nclaude_code opus medium</code>",
        reply_markup=_button("По умолчанию", "agents_default"),
    )
    await query.answer()


@router.callback_query(NewCb.filter(F.action == "agents_default"), NewProjectForm.agents)
async def agents_default(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(planner_agent=None, executor_agent=None)
    assert isinstance(query.message, Message)
    await _ask_weekdays(query.message, state)
    await query.answer()


@router.message(NewProjectForm.agents, F.text, ~F.text.startswith("/"))
async def agents_custom(message: Message, state: FSMContext) -> None:
    lines = [line for line in (message.text or "").splitlines() if line.strip()]
    try:
        if len(lines) != 2:
            raise ValueError("Нужно две строки: планировщик и исполнитель.")
        planner, executor = (parse_agent(line) for line in lines)
    except ValueError as exc:
        await message.answer(h(exc))
        return
    await state.update_data(planner_agent=planner, executor_agent=executor)
    await _ask_weekdays(message, state)


def _weekdays_markup(selected: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, name in enumerate(WEEKDAYS):
        mark = "✓" if index in selected else "·"
        builder.button(text=f"{mark}{name}", callback_data=NewCb(action="weekday", value=index))
    builder.button(text="Готово", callback_data=NewCb(action="weekdays_done"))
    builder.button(text="Настрою позже", callback_data=NewCb(action="weekdays_done", value=1))
    builder.adjust(7, 2)
    return builder.as_markup()


async def _ask_weekdays(message: Message, state: FSMContext) -> None:
    await state.set_state(NewProjectForm.weekdays)
    await state.update_data(weekdays=[])
    await message.answer(
        "В какие дни недели работать над проектом?", reply_markup=_weekdays_markup([])
    )


@router.callback_query(NewCb.filter(F.action == "weekday"), NewProjectForm.weekdays)
async def weekday_toggle(query: CallbackQuery, callback_data: NewCb, state: FSMContext) -> None:
    selected = set((await state.get_data())["weekdays"]) ^ {callback_data.value}
    await state.update_data(weekdays=sorted(selected))
    assert isinstance(query.message, Message)
    await query.message.edit_reply_markup(reply_markup=_weekdays_markup(sorted(selected)))
    await query.answer()


@router.callback_query(NewCb.filter(F.action == "weekdays_done"), NewProjectForm.weekdays)
async def weekdays_done(query: CallbackQuery, callback_data: NewCb, state: FSMContext) -> None:
    if callback_data.value == 1:
        await state.update_data(weekdays=[])
    data = await state.get_data()
    idea_text = "\n".join(data["idea"])
    name = idea_text.strip().splitlines()[0][:40].strip(" .,:;")
    await state.update_data(name=name, slug=suggest_slug(name))
    await state.set_state(NewProjectForm.name)
    assert isinstance(query.message, Message)
    await query.message.answer(
        f"Название: <b>{h(name)}</b>, slug: <code>{h(suggest_slug(name))}</code>.\n"
        "Подходит? Или пришли своё: <code>Название | slug</code>",
        reply_markup=_button("Подходит", "name_ok"),
    )
    await query.answer()


@router.message(NewProjectForm.name, F.text, ~F.text.startswith("/"))
async def name_custom(message: Message, state: FSMContext) -> None:
    name, _, slug = (message.text or "").partition("|")
    name = name.strip()
    if not name:
        await message.answer("Формат: <code>Название | slug</code>")
        return
    await state.update_data(name=name, slug=slug.strip() or suggest_slug(name))
    await _confirm(message, state)


@router.callback_query(NewCb.filter(F.action == "name_ok"), NewProjectForm.name)
async def name_ok(query: CallbackQuery, state: FSMContext) -> None:
    assert isinstance(query.message, Message)
    await _confirm(query.message, state)
    await query.answer()


async def _confirm(message: Message, state: FSMContext) -> None:
    await state.set_state(NewProjectForm.confirm)
    data = await state.get_data()
    days = ", ".join(WEEKDAYS[d] for d in data["weekdays"]) or "настрою позже"
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать", callback_data=NewCb(action="confirm"))
    builder.button(text="Отмена", callback_data=NewCb(action="cancel"))
    await message.answer(
        f"<b>{h(data['name'])}</b> (<code>{h(data['slug'])}</code>)\n"
        f"Дней: {data['days']} · дни недели: {days}\n"
        f"Стек: {h(data['stack'] or 'выберет агент')}",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(NewCb.filter(F.action == "cancel"))
async def cancel_button(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    assert isinstance(query.message, Message)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer("Отменено.")
    await query.answer()


@router.callback_query(NewCb.filter(F.action == "confirm"), NewProjectForm.confirm)
async def create(query: CallbackQuery, state: FSMContext, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    data: dict[str, Any] = await state.get_data()
    try:
        project = await app.services.projects.create(
            NewProject(
                name=data["name"],
                slug=data["slug"],
                idea="\n\n".join(data["idea"]),
                planned_days=data["days"],
                stack_hint=data["stack"],
                priority_position=data["priority"],
                planner_agent=data["planner_agent"],
                executor_agent=data["executor_agent"],
            )
        )
    except ProjectError as exc:
        await state.set_state(NewProjectForm.name)
        await message.answer(f"{h(exc)}\nПришли другое: <code>Название | slug</code>")
        await query.answer()
        return
    await state.clear()
    if data["weekdays"]:
        await app.services.calendar.set_weekdays(project.id, data["weekdays"])
    await app.orchestrator.start_planning(project.id)
    await message.edit_reply_markup(reply_markup=None)
    await message.answer(
        f"{project.badge} Проект <b>{h(project.name)}</b> создан. "
        "Агент изучает идею — скоро будут вопросы или план."
    )
    await query.answer()
