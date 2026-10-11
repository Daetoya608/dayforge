from typing import Any

from aiogram.types import Update, User

from dayforge.bot.access import OwnerOnlyMiddleware

OWNER_ID = 42


async def _handler(_event: Any, _data: dict[str, Any]) -> str:
    return "handled"


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="test")


async def test_owner_update_is_passed_through() -> None:
    middleware = OwnerOnlyMiddleware(OWNER_ID)
    data = {"event_from_user": _user(OWNER_ID)}

    assert await middleware(_handler, Update(update_id=1), data) == "handled"


async def test_other_users_are_dropped() -> None:
    middleware = OwnerOnlyMiddleware(OWNER_ID)

    assert await middleware(_handler, Update(update_id=1), {"event_from_user": _user(7)}) is None
    assert await middleware(_handler, Update(update_id=2), {}) is None
