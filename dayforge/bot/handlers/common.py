from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from dayforge import __version__

router = Router(name="common")

HELP = f"""<b>Dayforge {__version__}</b>

/new — создать проект
/projects — список проектов
/status <i>проект</i> — подробный статус
/plan <i>проект</i> — текущий план
/replan <i>проект</i> <i>что изменить</i> — перепланировать
/calendar — календарь
/priority — порядок проектов
/pause, /resume <i>проект</i> — пауза
/run [<i>проект</i>] — доделать незавершённое
/next — досрочно начать следующий день
/release <i>проект</i> — релиз dev → main
/report [<i>проект</i>] — последний отчёт
/agent <i>запрос</i> — системный агент (или просто напиши текст)
/agent_reset — новый диалог с агентом
/settings — настройки"""


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message) -> None:
    await message.answer(HELP)
