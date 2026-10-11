"""Тексты бота: статусы, прогноз, строки проектов, длинные сообщения (docs/telegram-bot.md)."""

import re
from html import escape

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import select

from dayforge.core.calendar import Forecast
from dayforge.core.enums import MilestoneStatus, ProjectStatus, TaskStatus
from dayforge.db.models import Commit, Milestone, Project, Task
from dayforge.services import Services

MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1000

STATUS_LABELS = {
    ProjectStatus.DRAFT: "черновик",
    ProjectStatus.CLARIFYING: "уточнение идеи",
    ProjectStatus.PLANNING: "планирование",
    ProjectStatus.PLAN_REVIEW: "план на согласовании",
    ProjectStatus.ACTIVE: "в работе",
    ProjectStatus.BLOCKED: "ждёт ответа",
    ProjectStatus.PAUSED: "на паузе",
    ProjectStatus.COMPLETED: "завершён",
    ProjectStatus.ARCHIVED: "в архиве",
}
TASK_ICONS = {
    TaskStatus.PENDING: "▫️",
    TaskStatus.RUNNING: "🔨",
    TaskStatus.VERIFYING: "🧪",
    TaskStatus.PAUSED: "⏸",
    TaskStatus.FAILED: "✖",
    TaskStatus.DONE: "✅",
    TaskStatus.CANCELLED: "🚫",
}
_TAG_RE = re.compile(r"<[^>]+>")
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def h(text: object) -> str:
    return escape(str(text))


def forecast_text(forecast: Forecast) -> str:
    if forecast.end_date is not None:
        return f"до {forecast.end_date:%d.%m}"
    if forecast.missing_days:
        return f"не хватает дней: {forecast.missing_days}"
    return "—"


def project_title(project: Project) -> str:
    return f"{project.badge} <b>{h(project.name)}</b>"


def project_line(project: Project, forecast: Forecast, open_questions: int) -> str:
    parts = [
        project_title(project),
        f"<code>{h(project.slug)}</code>",
        STATUS_LABELS[project.status],
    ]
    if project.status in (ProjectStatus.ACTIVE, ProjectStatus.BLOCKED, ProjectStatus.PAUSED):
        parts.append(f"день {project.current_day}/{project.planned_days}")
        parts.append(forecast_text(forecast))
    line = " · ".join(parts)
    return line + (f" ⚠️ вопросов: {open_questions}" if open_questions else "")


async def project_status(services: Services, project: Project) -> str:
    today = await services.calendar.today()
    forecast = await services.calendar.forecast(project, today=today)
    async with services.session_factory() as session:
        tasks = list(
            await session.scalars(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.day_number, Task.position, Task.id)
            )
        )
        milestones = list(
            await session.scalars(
                select(Milestone).where(Milestone.project_id == project.id).order_by(Milestone.id)
            )
        )
        commits = list(
            await session.scalars(
                select(Commit)
                .where(Commit.project_id == project.id)
                .order_by(Commit.committed_at.desc())
                .limit(5)
            )
        )

    lines = [
        f"{project_title(project)} · {STATUS_LABELS[project.status]}",
        f"День {project.current_day} из {project.planned_days} · прогноз {forecast_text(forecast)}",
    ]
    current = [t for t in tasks if t.day_number == project.current_day and t.day_number]
    carry = [
        t
        for t in tasks
        if t not in current
        and t.status in (TaskStatus.PAUSED, TaskStatus.FAILED, TaskStatus.RUNNING)
    ]
    if current:
        lines += ["", f"<b>День {project.current_day}</b>"]
        lines += [f"{TASK_ICONS[t.status]} {t.key} {h(t.title)}" for t in current]
    if carry:
        lines += ["", "<b>Незавершённое</b>"]
        lines += [f"{TASK_ICONS[t.status]} {t.key} {h(t.title)}" for t in carry]
    if milestones:
        marks = [f"{_released_icon(m)} {m.key} {h(m.title)} ({m.version})" for m in milestones]
        lines += ["", "<b>Вехи</b>", *marks]
    if commits:
        lines += ["", "<b>Последние коммиты</b>"]
        lines += [f"<code>{c.sha[:7]}</code> {h(c.message.splitlines()[0][:80])}" for c in commits]
    return "\n".join(lines)


def _released_icon(milestone: Milestone) -> str:
    return "✅" if milestone.status is MilestoneStatus.RELEASED else "▫️"


async def send_text(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    filename: str = "message.md",
    summary: str | None = None,
) -> Message:
    """Сообщение; если не влезает в лимит Telegram — файлом с кратким текстом в подписи."""
    if len(text) <= MESSAGE_LIMIT:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup)
    document = BufferedInputFile(_TAG_RE.sub("", text).encode(), filename=filename)
    if summary is not None:
        return await bot.send_document(
            chat_id, document, caption=summary[:CAPTION_LIMIT], reply_markup=reply_markup
        )
    # Обрезанный HTML может оказаться с незакрытым тегом — подпись без разметки.
    plain = _TAG_RE.sub("", text)[: CAPTION_LIMIT - 1] + "…"
    return await bot.send_document(
        chat_id, document, caption=plain, parse_mode=None, reply_markup=reply_markup
    )
