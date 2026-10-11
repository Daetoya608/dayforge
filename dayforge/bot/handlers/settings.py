"""/settings — глобальные настройки (docs/data-model.md#settings)."""

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dayforge.bot.callbacks import SettingsCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import h

router = Router(name="settings")

LABELS = {
    "timezone": "Часовой пояс",
    "run_time": "Время запуска",
    "backup_time": "Время бэкапа",
    "default_planner_agent": "Планировщик по умолчанию",
    "default_executor_agent": "Исполнитель по умолчанию",
    "system_agent": "Системный агент",
    "watchdog_idle_minutes": "Watchdog, мин без событий",
    "test_timeout_minutes": "Таймаут тестов, мин",
    "agent_chat_idle_hours": "Новый диалог /agent после, ч",
    "limit_auto_resume": "Автопродолжение после лимита",
}
HINTS = {
    "timezone": "Например: Europe/Moscow",
    "run_time": "ЧЧ:ММ, например 03:00",
    "backup_time": "ЧЧ:ММ, например 12:00",
    "default_planner_agent": "агент модель reasoning, например: claude_code opus high",
    "default_executor_agent": "агент модель reasoning, например: codex gpt-5-codex medium",
    "system_agent": "агент модель reasoning, например: claude_code sonnet medium",
    "watchdog_idle_minutes": "Число минут, например 45",
    "test_timeout_minutes": "Число минут, например 30",
    "agent_chat_idle_hours": "Число часов, например 6",
}
SCHEDULE_KEYS = ("timezone", "run_time", "backup_time")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsInput(StatesGroup):
    value = State()


def _show(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get(k) or "") for k in ("agent", "model", "effort")).strip()
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


async def _view(app: AppContext) -> tuple[str, InlineKeyboardMarkup]:
    values = await app.services.settings.get_all()
    lines = ["⚙️ <b>Настройки</b>"]
    builder = InlineKeyboardBuilder()
    for key, label in LABELS.items():
        lines.append(f"{label}: <code>{h(_show(values[key]))}</code>")
        builder.button(text=label, callback_data=SettingsCb(key=key))
    builder.adjust(2)
    return "\n".join(lines), builder.as_markup()


@router.message(Command("settings"))
async def settings_command(message: Message, app: AppContext) -> None:
    text, markup = await _view(app)
    await message.answer(text, reply_markup=markup)


@router.callback_query(SettingsCb.filter())
async def settings_pick(
    query: CallbackQuery, callback_data: SettingsCb, app: AppContext, state: FSMContext
) -> None:
    message = query.message
    assert isinstance(message, Message)
    key = callback_data.key
    if key == "limit_auto_resume":
        current = await app.services.settings.get(key)
        await app.services.settings.set(key, not current)
        text, markup = await _view(app)
        await message.edit_text(text, reply_markup=markup)
        await query.answer()
        return
    await state.set_state(SettingsInput.value)
    await state.update_data(key=key)
    await message.answer(f"{LABELS[key]} — новое значение?\n<i>{h(HINTS[key])}</i>")
    await query.answer()


@router.message(SettingsInput.value, F.text, ~F.text.startswith("/"))
async def settings_value(message: Message, app: AppContext, state: FSMContext) -> None:
    key = (await state.get_data())["key"]
    try:
        value = parse_setting(key, message.text or "")
    except ValueError as exc:
        await message.answer(f"{h(exc)} Попробуй ещё раз.")
        return
    await state.clear()
    await app.services.settings.set(key, value)
    if key in SCHEDULE_KEYS and app.scheduler is not None:
        await app.scheduler.reschedule()
    text, markup = await _view(app)
    await message.answer(text, reply_markup=markup)


def parse_setting(key: str, raw: str) -> Any:
    text = raw.strip()
    if key == "timezone":
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Неизвестный часовой пояс {text!r}.") from None
        return text
    if key in ("run_time", "backup_time"):
        if not _TIME_RE.fullmatch(text):
            raise ValueError("Нужно время в формате ЧЧ:ММ.")
        return text
    if key.endswith("_agent"):
        return parse_agent(text)
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        raise ValueError("Нужно число.") from None
    if number <= 0:
        raise ValueError("Число должно быть больше нуля.")
    return int(number) if number.is_integer() else number


def parse_agent(text: str) -> dict[str, Any]:
    parts = text.split()
    if len(parts) not in (2, 3) or parts[0] not in ("claude_code", "codex"):
        raise ValueError("Формат: claude_code|codex модель [reasoning].")
    return {"agent": parts[0], "model": parts[1], "effort": parts[2] if len(parts) == 3 else None}
