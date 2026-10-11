from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from dayforge import __version__

router = Router(name="common")


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message) -> None:
    await message.answer(
        f"Dayforge {__version__} работает.\n"
        "Команды появятся по мере реализации — см. docs/telegram-bot.md."
    )
