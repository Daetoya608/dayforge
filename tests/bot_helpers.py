"""Бот без Telegram: поддельная сессия aiogram запоминает вызовы API и отвечает правдоподобно."""

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendChatAction,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

OWNER_ID = 42
BOT_ID = 1000
CHAT = Chat(id=OWNER_ID, type="private")
OWNER = User(id=OWNER_ID, is_bot=False, first_name="Owner")


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(100)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.calls.append(method)
        now = datetime.now(UTC)
        match method:
            case SendMessage():
                return Message(
                    message_id=next(self._ids),
                    date=now,
                    chat=CHAT,
                    text=method.text,
                    reply_markup=method.reply_markup,
                )
            case SendDocument():
                return Message(
                    message_id=next(self._ids), date=now, chat=CHAT, caption=method.caption
                )
            case EditMessageText():
                return Message(
                    message_id=method.message_id or 0, date=now, chat=CHAT, text=method.text
                )
            case GetMe():
                return User(id=BOT_ID, is_bot=True, first_name="Dayforge", username="dayforge_bot")
            case (
                AnswerCallbackQuery()
                | DeleteMessage()
                | SendChatAction()
                | EditMessageReplyMarkup()
            ):
                return True
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        return None

    # --- удобства для проверок ---

    def sent(self) -> list[SendMessage | SendDocument]:
        return [c for c in self.calls if isinstance(c, SendMessage | SendDocument)]

    def texts(self) -> list[str]:
        return [(c.text if isinstance(c, SendMessage) else c.caption) or "" for c in self.sent()]

    def last_text(self) -> str:
        return self.texts()[-1]

    def buttons(self, call: SendMessage | SendDocument | None = None) -> list[tuple[str, str]]:
        call = call or self.sent()[-1]
        markup = call.reply_markup
        if markup is None or not hasattr(markup, "inline_keyboard"):
            return []
        return [(b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row]

    def clear(self) -> None:
        self.calls.clear()


def make_bot() -> tuple[Bot, FakeSession]:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    session = FakeSession()
    bot = Bot("1000:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, session


_update_ids = itertools.count(1)
_message_ids = itertools.count(10_000)


def message(text: str, *, reply_to: int | None = None, user: User = OWNER) -> Update:
    now = datetime.now(UTC)
    reply = Message(message_id=reply_to, date=now, chat=CHAT, text="…") if reply_to else None
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_message_ids),
            date=now,
            chat=CHAT,
            from_user=user,
            text=text,
            reply_to_message=reply,
        ),
    )


def callback(data: str, *, message_id: int = 1) -> Update:
    now = datetime.now(UTC)
    return Update(
        update_id=next(_update_ids),
        callback_query=CallbackQuery(
            id=str(next(_update_ids)),
            from_user=OWNER,
            chat_instance="ci",
            data=data,
            message=Message(message_id=message_id, date=now, chat=CHAT, text="…"),
        ),
    )
