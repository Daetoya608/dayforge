import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

log = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Пропускает только апдейты владельца, остальные молча отбрасывает.

    См. docs/telegram-bot.md#доступ.
    """

    def __init__(self, owner_id: int) -> None:
        self._owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id != self._owner_id:
            # Содержимое не логируем: это может быть что угодно от кого угодно.
            log.info("dropped update from non-owner user_id=%s", user.id if user else None)
            return None
        return await handler(event, data)
