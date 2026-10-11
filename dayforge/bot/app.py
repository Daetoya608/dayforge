from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from dayforge.bot import handlers
from dayforge.bot.access import OwnerOnlyMiddleware


def create_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher(owner_id: int) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(owner_id))
    dispatcher.include_router(handlers.router)
    return dispatcher
