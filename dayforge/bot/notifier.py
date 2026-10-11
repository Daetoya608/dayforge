"""TelegramNotifier: события ядра → сообщения владельцу (docs/telegram-bot.md#уведомления)."""

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dayforge.bot.callbacks import (
    ActionCb,
    AnswerCb,
    ContinueCb,
    FailedTaskCb,
    PlanCb,
    ProjectCb,
    ProposalCb,
    ShortageCb,
)
from dayforge.bot.formatting import h, send_text
from dayforge.bot.storage import BotKV
from dayforge.core import events as ev
from dayforge.services import Services

log = logging.getLogger(__name__)
SECRET_KEY_PREFIX = "secret_msg:"
PROPOSAL_KEY_PREFIX = "proposal:"


class TelegramNotifier:
    def __init__(self, bot: Bot, owner_id: int) -> None:
        self._bot = bot
        self._chat = owner_id
        self._services: Services | None = None
        self._kv: BotKV | None = None
        self._handlers: dict[type[ev.Event], Callable[[Any], Awaitable[None]]] = {
            ev.QuestionAsked: self._question,
            ev.SecretRequested: self._secret,
            ev.QuestionAnswered: self._answered,
            ev.PendingActionCreated: self._pending_action,
            ev.PendingActionExpired: self._action_expired,
            ev.PlanProposed: self._plan_proposed,
            ev.TaskFailed: self._task_failed,
            ev.PlanChangeProposed: self._plan_change,
            ev.TaskPaused: self._task_paused,
            ev.ProjectSkipped: self._skipped,
            ev.LimitReached: self._limit,
            ev.RunReport: self._report,
            ev.RunFailed: self._run_failed,
            ev.MilestoneReleased: self._released,
            ev.ProjectCompleted: self._completed,
            ev.SystemRestarted: self._restarted,
            ev.PlanningFailed: self._planning_failed,
            ev.DaysShortage: self._shortage,
            ev.BackupFailed: self._backup_failed,
        }

    def attach(self, services: Services, kv: BotKV) -> None:
        """Сервисам нужен notifier, а notifier'у — сервисы: связь после сборки."""
        self._services = services
        self._kv = kv

    @property
    def services(self) -> Services:
        assert self._services is not None, "notifier не подключён к сервисам"
        return self._services

    @property
    def kv(self) -> BotKV:
        assert self._kv is not None
        return self._kv

    async def send(self, event: ev.Event) -> None:
        handler = self._handlers.get(type(event))
        if handler is None:
            log.info("no telegram handler for %s", type(event).__name__)
            return
        try:
            await handler(event)
        except Exception:
            # Ошибка Telegram не должна ломать раннер или MCP-вызов агента.
            log.exception("failed to deliver %s", type(event).__name__)

    async def _text(self, text: str, markup: InlineKeyboardMarkup | None = None) -> int:
        message = await send_text(self._bot, self._chat, text, reply_markup=markup)
        return message.message_id

    # --- вопросы и секреты ---

    async def _question(self, event: ev.QuestionAsked) -> None:
        where = f"{event.project.badge} {h(event.project.name)}"
        if event.task_key:
            where += f" · {event.task_key}"
        ids = []
        total = len(event.questions)
        for index, item in enumerate(event.questions):
            header = f"❓ {where} · вопрос {index + 1} из {total}" if total > 1 else f"❓ {where}"
            parts = [header]
            if index == 0 and event.context:
                parts.append(h(event.context))
            parts += ["", f"<b>{h(item['text'])}</b>"]
            builder = InlineKeyboardBuilder()
            for option_index, option in enumerate(item["options"]):
                builder.button(
                    text=option,
                    callback_data=AnswerCb(
                        question_id=event.question_id, index=index, option=option_index
                    ),
                )
            builder.adjust(2)
            if item["allow_free_text"]:
                parts.append("<i>Или ответь на это сообщение текстом.</i>")
            ids.append(await self._text("\n".join(parts), builder.as_markup()))
        await self.services.questions.set_message_ids(event.question_id, ids)

    async def _secret(self, event: ev.SecretRequested) -> None:
        where = f"{event.project.badge} {h(event.project.name)}"
        if event.task_key:
            where += f" · {event.task_key}"
        lines = [
            f"🔑 {where} запрашивает <code>{h(event.name)}</code>",
            h(event.description),
        ]
        if event.how_to_get:
            lines.append(f"Где взять: {h(event.how_to_get)}")
        lines += [
            "",
            "<i>Ответь на это сообщение значением. Сообщение со значением будет удалено.</i>",
        ]
        message_id = await self._text("\n".join(lines))
        await self.services.questions.set_message_ids(event.question_id, [message_id])

    async def _answered(self, event: ev.QuestionAnswered) -> None:
        if event.task_key is None:
            await self._text(f"✅ Ответы переданы планировщику {h(event.project.name)}.")
            return
        if not event.project_unblocked:
            await self._text(f"✅ Ответ получен. У {h(event.project.name)} есть ещё вопросы.")
            return
        builder = InlineKeyboardBuilder()
        builder.button(
            text="▶ Продолжить сейчас",
            callback_data=ContinueCb(project_id=event.project.id, now=True),
        )
        builder.button(
            text="Позже", callback_data=ContinueCb(project_id=event.project.id, now=False)
        )
        await self._text(
            f"✅ Ответы получены. {h(event.project.name)} готов продолжить {event.task_key}.",
            builder.as_markup(),
        )

    # --- подтверждения ---

    async def _pending_action(self, event: ev.PendingActionCreated) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Подтвердить", callback_data=ActionCb(action_id=event.action_id, confirm=True)
        )
        builder.button(
            text="Отмена", callback_data=ActionCb(action_id=event.action_id, confirm=False)
        )
        message_id = await self._text(
            f"⚠️ Подтверди действие системного агента\n{h(event.description)}", builder.as_markup()
        )
        await self.services.actions.set_message_id(event.action_id, message_id)

    async def _action_expired(self, event: ev.PendingActionExpired) -> None:
        if event.tg_message_id is None:
            return
        await self._bot.edit_message_text(
            "⌛ Истекло — действие не выполнено", chat_id=self._chat, message_id=event.tg_message_id
        )

    # --- план ---

    async def _plan_proposed(self, event: ev.PlanProposed) -> None:
        title = "📐" + (" Перепланирование" if event.diff is not None else "")
        lines = [
            f"{title} {event.project.badge} <b>{h(event.project.name)}</b> · план v{event.version} "
            f"на {event.days} дн.",
            h(event.summary),
        ]
        if event.change_summary:
            lines.append(f"<i>{h(event.change_summary)}</i>")
        lines.append("Вехи: " + ", ".join(f"{k} {h(t)} ({v})" for k, t, v in event.milestones))
        if event.forecast_end:
            lines.append(f"Прогноз окончания: {date.fromisoformat(event.forecast_end):%d.%m.%Y}")
        if event.missing_days:
            lines.append(f"⚠️ В календаре не хватает дней: {event.missing_days}")
        lines += [f"⚠️ {h(w)}" for w in event.warnings]
        if event.diff:
            lines += ["", "<b>Изменения</b>", *(h(line) for line in event.diff)]
        elif event.diff is not None:
            lines += ["", "Изменений в задачах нет."]

        builder = InlineKeyboardBuilder()
        builder.button(text="PLAN.md", callback_data=PlanCb(action="file", plan_id=event.plan_id))
        builder.button(
            text="ARCHITECTURE.md", callback_data=PlanCb(action="arch", plan_id=event.plan_id)
        )
        builder.button(
            text="✅ Утвердить", callback_data=PlanCb(action="approve", plan_id=event.plan_id)
        )
        builder.button(text="✏ Правки", callback_data=PlanCb(action="edit", plan_id=event.plan_id))
        builder.button(
            text="✖ Отклонить", callback_data=PlanCb(action="reject", plan_id=event.plan_id)
        )
        builder.adjust(2, 3)
        await self._text("\n".join(lines), builder.as_markup())

    async def _task_failed(self, event: ev.TaskFailed) -> None:
        lines = [
            f"✖ {h(event.project.name)} · {event.task_key} не может быть выполнена",
            h(event.reason),
        ]
        if event.suggestion:
            lines.append(f"Предложение агента: {h(event.suggestion)}")
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Передать планировщику",
            callback_data=FailedTaskCb(action="replan", task_id=event.task_id),
        )
        builder.button(
            text="Вернуть в работу",
            callback_data=FailedTaskCb(action="retry", task_id=event.task_id),
        )
        builder.button(
            text="Отменить задачу",
            callback_data=FailedTaskCb(action="cancel", task_id=event.task_id),
        )
        builder.adjust(1)
        await self._text("\n".join(lines), builder.as_markup())

    async def _plan_change(self, event: ev.PlanChangeProposed) -> None:
        key = secrets.token_hex(6)
        await self.kv.set(
            PROPOSAL_KEY_PREFIX + key,
            {
                "project_id": event.project.id,
                "text": f"{event.reason}\n\nПредложение: {event.proposal}",
            },
        )
        who = f"{h(event.project.name)}" + (f" · {event.task_key}" if event.task_key else "")
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Передать планировщику", callback_data=ProposalCb(action="replan", key=key)
        )
        builder.button(text="Отклонить", callback_data=ProposalCb(action="dismiss", key=key))
        await self._text(
            f"💭 {who} предлагает изменить план\n{h(event.reason)}\n\n<b>{h(event.proposal)}</b>",
            builder.as_markup(),
        )

    # --- запуски ---

    async def _task_paused(self, event: ev.TaskPaused) -> None:
        text = f"⏸ {h(event.project.name)} · {event.task_key}: {h(event.reason)}"
        if event.details:
            text += f"\n<pre>{h(event.details[-1500:])}</pre>"
        await self._text(text, _continue_markup(event.project.id))

    async def _skipped(self, event: ev.ProjectSkipped) -> None:
        await self._text(f"⏭ {h(event.project.name)}: {h(event.reason)}")

    async def _limit(self, event: ev.LimitReached) -> None:
        lines = [f"🛑 Лимит подписки в {h(event.project.name)} — запуск остановлен."]
        if event.cancelled:
            lines.append("Не выполнены: " + ", ".join(h(p.name) for p in event.cancelled))
        if event.resets_at:
            lines.append(f"Сброс лимита: {event.resets_at:%d.%m %H:%M} UTC")
        if event.auto_resume:
            lines.append("Продолжу автоматически после сброса.")
        await self._text("\n".join(lines), None if event.auto_resume else _continue_markup(0))

    async def _report(self, event: ev.RunReport) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Статус", callback_data=ProjectCb(action="status", project_id=event.project.id)
        )
        builder.button(
            text="План", callback_data=ProjectCb(action="plan", project_id=event.project.id)
        )
        await self._text(event.text, builder.as_markup())

    async def _run_failed(self, event: ev.RunFailed) -> None:
        await self._text(f"💥 Запуск {h(event.project.name)} упал: <code>{h(event.error)}</code>")

    async def _released(self, event: ev.MilestoneReleased) -> None:
        tag = f'<a href="{h(event.url)}">{h(event.tag)}</a>' if event.url else h(event.tag)
        await self._text(f"🏷 {h(event.project.name)}: выпущена {tag} — {h(event.title)}")

    async def _completed(self, event: ev.ProjectCompleted) -> None:
        lines = [
            f"🎉 Проект {h(event.project.name)} завершён",
            f"Дней: {event.days} · задач: {event.tasks_done}",
        ]
        if event.versions:
            lines.append("Версии: " + ", ".join(h(v) for v in event.versions))
        if event.url:
            lines.append(f'<a href="{h(event.url)}">Репозиторий</a>')
        await self._text("\n".join(lines))

    async def _restarted(self, event: ev.SystemRestarted) -> None:
        items = ", ".join(f"{h(p.name)} · {key}" for p, key in event.interrupted)
        await self._text(f"🔄 Система перезапускалась, прервано: {items}", _continue_markup(0))

    async def _planning_failed(self, event: ev.PlanningFailed) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="Перезапустить планирование",
            callback_data=ProjectCb(action="replan_restart", project_id=event.project.id),
        )
        await self._text(
            f"⚠️ Планирование {h(event.project.name)} остановилось: {h(event.reason)}",
            builder.as_markup(),
        )

    async def _shortage(self, event: ev.DaysShortage) -> None:
        await self._text(
            f"📅 Проекту {project_title_ref(event.project)} не хватает {event.missing_days} дн. "
            "в календаре. Прогноз окончания невозможен.",
            shortage_markup(event.project.id),
        )

    async def _backup_failed(self, event: ev.BackupFailed) -> None:
        await self._text(f"💾 Бэкап не удался: <code>{h(event.error)}</code>")


def project_title_ref(project: ev.ProjectRef) -> str:
    return f"{project.badge} <b>{h(project.name)}</b>"


def _continue_markup(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶ Продолжить сейчас",
                    callback_data=ContinueCb(project_id=project_id, now=True).pack(),
                )
            ]
        ]
    )


def shortage_markup(project_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for days in (3, 5, 10):
        builder.button(
            text=f"+{days} будней", callback_data=ShortageCb(project_id=project_id, days=days)
        )
    builder.button(
        text="Выбрать в календаре", callback_data=ShortageCb(project_id=project_id, days=0)
    )
    builder.adjust(3, 1)
    return builder.as_markup()
