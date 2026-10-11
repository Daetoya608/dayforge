from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import BotCommand

from dayforge.bot.access import OwnerOnlyMiddleware
from dayforge.bot.context import AppContext
from dayforge.bot.handlers import ROUTERS

COMMANDS = [
    ("new", "Создать проект"),
    ("projects", "Список проектов"),
    ("status", "Статус проекта"),
    ("plan", "План проекта"),
    ("replan", "Перепланировать"),
    ("calendar", "Календарь"),
    ("priority", "Порядок проектов"),
    ("pause", "Пауза проекта"),
    ("resume", "Возобновить проект"),
    ("run", "Доделать незавершённое"),
    ("next", "Начать следующий день досрочно"),
    ("release", "Релиз dev → main"),
    ("report", "Последний отчёт"),
    ("agent", "Системный агент"),
    ("agent_reset", "Новый диалог с агентом"),
    ("settings", "Настройки"),
    ("help", "Помощь"),
]


def create_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher(
    owner_id: int, app: AppContext, storage: BaseStorage | None = None
) -> Dispatcher:
    dispatcher = Dispatcher(storage=storage) if storage is not None else Dispatcher()
    dispatcher["app"] = app
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(owner_id))
    for router in ROUTERS:
        dispatcher.include_router(router)
    return dispatcher


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
