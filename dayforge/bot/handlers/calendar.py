"""Календарь в боте (docs/telegram-bot.md#calendar, docs/calendar.md)."""

import calendar as pycalendar
from datetime import date, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dayforge.bot.callbacks import CalendarCb, ShortageCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import WEEKDAYS, h
from dayforge.core.calendar import ProjectCalendar
from dayforge.core.enums import CalendarDateKind, ProjectStatus
from dayforge.db.models import Project

router = Router(name="calendar")

MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]  # fmt: skip
SHOWN = (
    ProjectStatus.PLAN_REVIEW,
    ProjectStatus.ACTIVE,
    ProjectStatus.BLOCKED,
    ProjectStatus.PAUSED,
    ProjectStatus.COMPLETED,
)


async def _calendars(app: AppContext) -> list[tuple[Project, ProjectCalendar]]:
    today = await app.services.calendar.today()
    projects = [p for p in await app.services.projects.list_projects() if p.status in SHOWN]
    return [(p, await app.services.calendar.project_calendar(p, today=today)) for p in projects]


STRUCK_DAYS = 7


def _mark(project: Project, cal: ProjectCalendar, day: date) -> str:
    """Метка проекта в ячейке: слот — badge; у завершённого — «~» ещё неделю после завершения."""
    if project.status is not ProjectStatus.COMPLETED:
        return project.badge if cal.is_slot(day) else ""
    if project.completed_at is None or not cal.is_slot(day):
        return ""
    finished = project.completed_at.date()
    return "~" if finished < day <= finished + timedelta(days=STRUCK_DAYS) else ""


async def month_view(app: AppContext, year: int, month: int) -> tuple[str, InlineKeyboardMarkup]:
    calendars = await _calendars(app)
    today = await app.services.calendar.today()
    builder = InlineKeyboardBuilder()
    prev_month = date(year, month, 1) - timedelta(days=1)
    next_month = date(year, month, 28) + timedelta(days=4)
    builder.button(
        text="◀",
        callback_data=CalendarCb(action="month", year=prev_month.year, month=prev_month.month),
    )
    builder.button(text=f"{MONTHS[month - 1]} {year}", callback_data="noop")
    builder.button(
        text="▶",
        callback_data=CalendarCb(action="month", year=next_month.year, month=next_month.month),
    )
    for name in WEEKDAYS:
        builder.button(text=name, callback_data="noop")
    weeks = pycalendar.Calendar().monthdayscalendar(year, month)
    for week in weeks:
        for number in week:
            if number == 0:
                builder.button(text=" ", callback_data="noop")
                continue
            day = date(year, month, number)
            badges = "".join(_mark(p, cal, day) for p, cal in calendars)
            off = any(day in cal.days_off for _, cal in calendars)
            label = f"{number}{'🚫' if off else badges[:2]}"
            if day == today:
                label = f"[{label}]"
            builder.button(
                text=label,
                callback_data=CalendarCb(action="day", year=year, month=month, day=number),
            )
    builder.button(text="Недельный шаблон", callback_data=CalendarCb(action="template"))
    builder.adjust(3, 7, *([7] * len(weeks)), 1)
    legend = " ".join(f"{p.badge} {h(p.name)}" for p, _ in calendars) or "проектов в календаре нет"
    return f"📅 Календарь\n{legend}", builder.as_markup()


async def day_view(app: AppContext, day: date) -> tuple[str, InlineKeyboardMarkup]:
    calendars = await _calendars(app)
    builder = InlineKeyboardBuilder()
    for project, cal in calendars:
        if project.status is ProjectStatus.COMPLETED:
            continue
        mark = "✓" if cal.is_slot(day) else "✗"
        builder.button(
            text=f"{mark} {project.badge} {project.name}",
            callback_data=CalendarCb(
                action="toggle", year=day.year, month=day.month, day=day.day, project_id=project.id
            ),
        )
    off = any(day in cal.days_off for _, cal in calendars)
    builder.button(
        text=("✓" if off else "✗") + " Выходной для всех",
        callback_data=CalendarCb(action="off", year=day.year, month=day.month, day=day.day),
    )
    builder.button(
        text="◀ К месяцу", callback_data=CalendarCb(action="month", year=day.year, month=day.month)
    )
    builder.adjust(1)
    return f"📅 {day:%d.%m.%Y} ({WEEKDAYS[day.weekday()]})", builder.as_markup()


async def template_view(app: AppContext, project_id: int) -> tuple[str, InlineKeyboardMarkup]:
    calendars = await _calendars(app)
    builder = InlineKeyboardBuilder()
    rows = []
    for project, cal in calendars:
        if project.status is ProjectStatus.COMPLETED:
            continue
        builder.button(
            text=f"{'▸ ' if project.id == project_id else ''}{project.badge} {project.name}",
            callback_data=CalendarCb(action="template", project_id=project.id),
        )
        rows.append(1)
        if project.id == project_id:
            for weekday, name in enumerate(WEEKDAYS):
                mark = "✓" if weekday in cal.weekdays else "·"
                builder.button(
                    text=f"{mark}{name}",
                    callback_data=CalendarCb(
                        action="weekday", project_id=project.id, weekday=weekday
                    ),
                )
            rows.append(7)
    today = await app.services.calendar.today()
    builder.button(
        text="◀ К месяцу",
        callback_data=CalendarCb(action="month", year=today.year, month=today.month),
    )
    rows.append(1)
    builder.adjust(*rows)
    return "Недельный шаблон: выбери проект и отметь дни недели.", builder.as_markup()


@router.message(Command("calendar"))
async def calendar_command(message: Message, app: AppContext) -> None:
    today = await app.services.calendar.today()
    text, markup = await month_view(app, today.year, today.month)
    await message.answer(text, reply_markup=markup)


@router.callback_query(CalendarCb.filter())
async def calendar_action(query: CallbackQuery, callback_data: CalendarCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    cb = callback_data
    match cb.action:
        case "month":
            text, markup = await month_view(app, cb.year, cb.month)
        case "day":
            text, markup = await day_view(app, date(cb.year, cb.month, cb.day))
        case "toggle":
            day = date(cb.year, cb.month, cb.day)
            error = await toggle_project_day(app, cb.project_id, day)
            if error:
                await query.answer(error, show_alert=True)
                return
            text, markup = await day_view(app, day)
        case "off":
            day = date(cb.year, cb.month, cb.day)
            calendars = await _calendars(app)
            if any(day in cal.days_off for _, cal in calendars):
                await app.services.calendar.clear_dates(None, [day])
            else:
                await app.services.calendar.set_dates(None, [day], CalendarDateKind.EXCLUDE)
            text, markup = await day_view(app, day)
        case "template":
            text, markup = await template_view(app, cb.project_id)
        case "weekday":
            project = await app.services.projects.get_by_id(cb.project_id)
            cal = await app.services.calendar.project_calendar(
                project, today=await app.services.calendar.today()
            )
            await app.services.calendar.set_weekdays(cb.project_id, cal.weekdays ^ {cb.weekday})
            text, markup = await template_view(app, cb.project_id)
        case _:
            await query.answer()
            return
    await message.edit_text(text, reply_markup=markup)
    await query.answer()


async def toggle_project_day(app: AppContext, project_id: int, day: date) -> str | None:
    project = await app.services.projects.get_by_id(project_id)
    cal = await app.services.calendar.project_calendar(
        project, today=await app.services.calendar.today()
    )
    if day in cal.days_off:
        return "Это общий выходной — сначала сними его"
    if day < cal.starts_on:
        return "До утверждения плана слотов нет"
    if cal.is_slot(day):
        if day in cal.include:
            await app.services.calendar.clear_dates(project_id, [day])
        else:
            await app.services.calendar.set_dates(project_id, [day], CalendarDateKind.EXCLUDE)
    elif day in cal.exclude:
        await app.services.calendar.clear_dates(project_id, [day])
    else:
        await app.services.calendar.set_dates(project_id, [day], CalendarDateKind.INCLUDE)
    return None


async def add_workdays(app: AppContext, project_id: int, count: int) -> list[date]:
    """«+N будней»: ближайшие рабочие дни, которые ещё не слоты проекта."""
    project = await app.services.projects.get_by_id(project_id)
    today = await app.services.calendar.today()
    cal = await app.services.calendar.project_calendar(project, today=today)
    added: list[date] = []
    day = today
    while len(added) < count:
        day += timedelta(days=1)
        if day.weekday() < 5 and not cal.is_slot(day) and day not in cal.days_off:
            added.append(day)
    await app.services.calendar.set_dates(project_id, added, CalendarDateKind.INCLUDE)
    return added


@router.callback_query(ShortageCb.filter())
async def shortage(query: CallbackQuery, callback_data: ShortageCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    if callback_data.days == 0:
        today = await app.services.calendar.today()
        text, markup = await month_view(app, today.year, today.month)
        await message.answer(text, reply_markup=markup)
        await query.answer()
        return
    added = await add_workdays(app, callback_data.project_id, callback_data.days)
    project = await app.services.projects.get_by_id(callback_data.project_id)
    forecast = await app.services.calendar.forecast(
        project, today=await app.services.calendar.today()
    )
    await message.edit_reply_markup(reply_markup=None)
    dates = ", ".join(f"{d:%d.%m}" for d in added)
    tail = (
        f"Прогноз окончания: {forecast.end_date:%d.%m}."
        if forecast.end_date
        else f"Всё ещё не хватает дней: {forecast.missing_days}."
    )
    await message.answer(f"📅 Добавлены дни {dates}. {tail}")
    await query.answer()
