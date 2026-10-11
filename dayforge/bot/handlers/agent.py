"""/agent и свободный текст → системный агент. Роутер подключается последним."""

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from dayforge.bot.context import AppContext
from dayforge.bot.formatting import h, send_text

router = Router(name="agent")


@router.message(Command("agent_reset"))
async def reset(message: Message, app: AppContext) -> None:
    app.chat.reset()
    await message.answer("Начат новый диалог с системным агентом.")


@router.message(Command("agent"))
async def agent_command(message: Message, command: CommandObject, app: AppContext) -> None:
    if not command.args:
        await message.answer("Формат: /agent <i>запрос</i> — или просто напиши текст.")
        return
    await _ask(message, app, command.args)


@router.message(F.text, ~F.text.startswith("/"))
async def free_text(message: Message, app: AppContext) -> None:
    await _ask(message, app, message.text or "")


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Не знаю такой команды. Список — /help.")


async def _ask(message: Message, app: AppContext, text: str) -> None:
    if app.chat.busy:
        await message.answer("Отвечу после предыдущего сообщения…")
    async with ChatActionSender(bot=message.bot, chat_id=message.chat.id, action=ChatAction.TYPING):  # type: ignore[arg-type]
        try:
            reply = await app.chat.ask(text)
        except Exception as exc:
            reply = f"Системный агент недоступен: <code>{h(exc)}</code>"
            await message.answer(reply)
            return
    # Ответ агента — обычный текст, не HTML.
    await send_text(message.bot, message.chat.id, h(reply), filename="answer.md")  # type: ignore[arg-type]
