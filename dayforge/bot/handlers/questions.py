"""Ответы на вопросы агентов (кнопки и reply) и секреты (docs/telegram-bot.md#вопросы-агентов)."""

import contextlib

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from dayforge.bot.callbacks import AnswerCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import h
from dayforge.core.enums import QuestionKind, QuestionStatus
from dayforge.core.questions import QuestionError
from dayforge.db.models import Question

router = Router(name="questions")
PARTIAL_KEY = "answers:"


async def _question_for_reply(app: AppContext, message_id: int) -> tuple[Question, int] | None:
    for question in await app.services.questions.open_questions():
        if message_id in question.tg_message_ids:
            return question, question.tg_message_ids.index(message_id)
    return None


async def _record(app: AppContext, question: Question, index: int, answer: str) -> str:
    """Сохраняет ответ на один вопрос; когда отвечены все — передаёт их агенту."""
    key = PARTIAL_KEY + str(question.id)
    partial: dict[str, str] = await app.kv.get(key, {})
    partial[str(index)] = answer
    total = len(question.payload["questions"])
    if len(partial) < total:
        await app.kv.set(key, partial)
        return f"Принято ({len(partial)} из {total})."
    await app.kv.delete(key)
    outcome = await app.services.questions.answer(
        question.id, [partial[str(i)] for i in range(total)]
    )
    await app.orchestrator.after_answer(outcome)
    return "Ответы приняты."


@router.callback_query(AnswerCb.filter())
async def answer_button(query: CallbackQuery, callback_data: AnswerCb, app: AppContext) -> None:
    question = await app.services.questions.get(callback_data.question_id)
    if question.status is not QuestionStatus.OPEN:
        await query.answer("На этот вопрос уже ответили", show_alert=True)
        return
    item = question.payload["questions"][callback_data.index]
    answer = item["options"][callback_data.option]
    try:
        reply = await _record(app, question, callback_data.index, answer)
    except QuestionError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
    await query.answer(f"{answer}. {reply}")


@router.message(F.reply_to_message, F.text)
async def answer_reply(message: Message, app: AppContext) -> None:
    replied = message.reply_to_message
    assert replied is not None
    found = await _question_for_reply(app, replied.message_id)
    if found is None:
        await message.answer("Это сообщение не ждёт ответа. Чтобы спросить агента — /agent.")
        return
    question, index = found
    text = (message.text or "").strip()
    try:
        if question.kind is QuestionKind.SECRET:
            # Значение секрета не должно остаться в чате.
            with contextlib.suppress(Exception):
                await message.delete()
            outcome = await app.services.questions.provide_secret(question.id, text)
            await app.orchestrator.after_answer(outcome)
            name = question.payload["name"]
            await message.answer(f"🔑 Сохранено в <code>.env</code> как <code>{h(name)}</code>.")
            return
        item = question.payload["questions"][index]
        if not item["allow_free_text"]:
            await message.answer("На этот вопрос — только кнопкой с вариантом.")
            return
        await message.answer(await _record(app, question, index, text))
    except QuestionError as exc:
        await message.answer(h(str(exc)))
