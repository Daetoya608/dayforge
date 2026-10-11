import asyncio
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.methods import DeleteMessage, SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.actions import register_action_handlers
from dayforge.bot.app import create_dispatcher
from dayforge.bot.context import AppContext
from dayforge.bot.handlers import ROUTERS
from dayforge.bot.notifier import TelegramNotifier
from dayforge.bot.storage import BotKV, SqliteStorage
from dayforge.core.enums import CalendarDateKind, ProjectStatus, RunMode
from dayforge.core.secrets import EnvFile
from dayforge.db.models import CalendarDate, Project, Question
from dayforge.services import Services, build_services
from tests.bot_helpers import OWNER_ID, FakeSession, callback, make_bot, message
from tests.mcp_helpers import FakeOrchestrator
from tests.test_runner import approved_project

SessionFactory = async_sessionmaker[AsyncSession]


class FakeRunner:
    def __init__(self) -> None:
        self.kicks = 0
        self.busy = False
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def kick(self) -> None:
        self.kicks += 1

    def lock(self, project_id: int) -> asyncio.Lock:
        return self._locks[project_id]


class FakeChat:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.resets = 0
        self.busy = False

    async def ask(self, text: str) -> str:
        self.asked.append(text)
        return f"Ответ на: {text} <b>"

    def reset(self) -> None:
        self.resets += 1


class Harness:
    def __init__(self, services: Services, dispatcher: Dispatcher, bot: Any, session: FakeSession):
        self.services = services
        self.dispatcher = dispatcher
        self.bot = bot
        self.session = session
        self.app: AppContext = dispatcher["app"]

    async def send(self, update: Any) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def text(self, text: str, **kwargs: Any) -> None:
        await self.send(message(text, **kwargs))

    async def press(self, data: str) -> None:
        await self.send(callback(data))


@pytest.fixture
async def harness(session_factory: SessionFactory, tmp_path: Path) -> Any:
    (tmp_path / "ws").mkdir()
    bot, fake = make_bot()
    notifier = TelegramNotifier(bot, OWNER_ID)
    services = build_services(
        session_factory,
        notifier,
        projects_dir=tmp_path / "ws",
        sessions_log_dir=tmp_path / "logs",
        vcs_provider=None,
    )
    kv = BotKV(session_factory)
    notifier.attach(services, kv)
    orchestrator = FakeOrchestrator()
    services.orchestrator = orchestrator  # type: ignore[assignment]
    register_action_handlers(services)
    app = AppContext(
        services=services,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        runner=FakeRunner(),  # type: ignore[arg-type]
        chat=FakeChat(),  # type: ignore[arg-type]
        kv=kv,
    )
    dispatcher = create_dispatcher(OWNER_ID, app, SqliteStorage(kv))
    yield Harness(services, dispatcher, bot, fake)
    # Роутеры — объекты модулей, а диспетчер в каждом тесте свой: отсоединяем.
    for router in ROUTERS:
        router._parent_router = None


async def test_non_owner_is_ignored(harness: Harness) -> None:
    from aiogram.types import User

    await harness.text("/projects", user=User(id=7, is_bot=False, first_name="x"))

    assert harness.session.sent() == []


async def test_projects_and_status(harness: Harness) -> None:
    await harness.text("/projects")
    assert "Проектов пока нет" in harness.session.last_text()

    project = await approved_project(harness.services)
    await harness.text("/projects")
    assert "<code>todo-bot</code> · в работе · день 0/2" in harness.session.last_text()

    await harness.text("/status todo-bot")
    text = harness.session.last_text()
    assert text.startswith(f"{project.badge} <b>todo-bot</b> · в работе")
    assert "▫️ M1 База (v0.1.0)" in text
    assert ("⏸ Пауза", f"prj:pause:{project.id}") in harness.session.buttons()

    await harness.press(f"prj:pause:{project.id}")
    assert (await harness.services.projects.get_by_id(project.id)).status is ProjectStatus.PAUSED


async def test_new_project_dialog(harness: Harness) -> None:
    await harness.text("/new")
    await harness.text("Бот учёта расходов")
    await harness.text("с категориями и отчётами")
    await harness.press("new:idea_done:0")
    await harness.text("Python, aiogram")
    await harness.text("abc")
    assert "от 1 до 365" in harness.session.last_text()
    await harness.text("10")
    await harness.press("new:prio:0")
    await harness.text("claude_code opus high\ncodex gpt-5-codex medium")
    await harness.press("new:weekday:0")
    await harness.press("new:weekday:2")
    await harness.press("new:weekdays_done:0")
    assert "slug: <code>bot-ucheta-rashodov</code>" in harness.session.last_text()
    await harness.text("Расходы | expenses")
    await harness.press("new:confirm:0")

    project = await harness.services.projects.get("expenses")
    assert (project.name, project.planned_days, project.stack_hint) == (
        "Расходы",
        10,
        "Python, aiogram",
    )
    assert project.idea == "Бот учёта расходов\n\nс категориями и отчётами"
    assert project.executor_agent == {"agent": "codex", "model": "gpt-5-codex", "effort": "medium"}
    cal = await harness.services.calendar.project_calendar(
        project, today=await harness.services.calendar.today()
    )
    assert cal.weekdays == {0, 2}
    assert harness.app.orchestrator.calls == [("start_planning", project.id)]  # type: ignore[attr-defined]
    assert "Агент изучает идею" in harness.session.last_text()


async def test_question_answers_by_button_and_reply(
    harness: Harness, session_factory: SessionFactory
) -> None:
    project = await approved_project(harness.services)
    question_id = await harness.services.questions.ask(
        project_id=project.id,
        task_id=None,
        session_id=None,
        questions=[{"text": "Провайдер?", "options": ["Stripe", "ЮKassa"]}, {"text": "Валюта?"}],
    )
    first, second = harness.session.sent()[-2:]
    assert isinstance(first, SendMessage) and isinstance(second, SendMessage)
    first_id, second_id = (await harness.services.questions.get(question_id)).tg_message_ids

    await harness.press(f"ans:{question_id}:0:1")
    await harness.text("Рубли", reply_to=second_id)

    async with session_factory() as session:
        question = await session.get(Question, question_id)
    assert question is not None and question.answers == ["ЮKassa", "Рубли"]
    assert harness.app.orchestrator.calls[-1][0] == "after_answer"  # type: ignore[attr-defined]
    assert first_id != second_id


async def test_secret_reply_is_deleted_and_saved(harness: Harness) -> None:
    project = await approved_project(harness.services)
    question_id = await harness.services.questions.request_secret(
        project_id=project.id, task_id=None, session_id=None, name="API_KEY", description="ключ"
    )
    (message_id,) = (await harness.services.questions.get(question_id or 0)).tg_message_ids

    await harness.text("sk_live_123", reply_to=message_id)

    assert any(isinstance(c, DeleteMessage) for c in harness.session.calls)
    assert EnvFile(Path(project.repo_path) / ".env").values() == ["sk_live_123"]
    assert "sk_live_123" not in " ".join(harness.session.texts())
    assert "<code>API_KEY</code>" in harness.session.last_text()


async def test_next_selection_enqueues_runs(harness: Harness) -> None:
    project = await approved_project(harness.services)

    await harness.text("/next")
    await harness.press("next:start:0")
    await harness.press(f"next:toggle:{project.id}")
    await harness.press("next:start:0")

    (run,) = await harness.services.runs.queued()
    assert (run.project_id, run.mode) == (project.id, RunMode.NEXT)
    assert harness.app.runner.kicks == 1  # type: ignore[attr-defined]


async def test_plan_buttons_call_orchestrator(harness: Harness) -> None:
    project = await approved_project(harness.services)
    orchestrator = harness.app.orchestrator
    original = orchestrator.approve_plan

    async def approve(plan_id: int) -> Project:
        await original(plan_id)
        return project

    orchestrator.approve_plan = approve  # type: ignore[method-assign]
    await harness.press("plan:approve:5")
    assert "✅ План todo-bot утверждён. Прогноз окончания: до" in harness.session.last_text()
    await harness.press("plan:edit:5")
    await harness.text("Объедини дни 2 и 3")

    calls = harness.app.orchestrator.calls  # type: ignore[attr-defined]
    assert calls == [("approve_plan", 5), ("send_plan_feedback", (5, "Объедини дни 2 и 3"))]


async def test_calendar_toggle_and_shortage(
    harness: Harness, session_factory: SessionFactory
) -> None:
    project = await approved_project(harness.services)  # шаблон — все дни недели
    today = await harness.services.calendar.today()
    day = today + timedelta(days=3)

    await harness.text("/calendar")
    assert harness.session.last_text().startswith("📅 Календарь")
    await harness.press(f"cal:toggle:{day.year}:{day.month}:{day.day}:{project.id}:-1")

    async with session_factory() as session:
        rows = list(await session.scalars(select(CalendarDate)))
    assert [(r.date, r.kind) for r in rows] == [(day, CalendarDateKind.EXCLUDE)]

    await harness.services.calendar.set_weekdays(project.id, [])
    await harness.press(f"short:{project.id}:3")
    assert "Добавлены дни" in harness.session.last_text()


async def test_settings_change(harness: Harness) -> None:
    await harness.text("/settings")
    await harness.press("set:run_time")
    await harness.text("25:00")
    assert "ЧЧ:ММ" in harness.session.last_text()
    await harness.text("04:30")

    assert await harness.services.settings.get("run_time") == "04:30"

    await harness.press("set:limit_auto_resume")
    assert await harness.services.settings.get("limit_auto_resume") is True


async def test_free_text_goes_to_system_agent_escaped(harness: Harness) -> None:
    await harness.text("что с проектами?")
    await harness.text("/agent_reset")
    await harness.text("/nonsense")

    assert harness.app.chat.asked == ["что с проектами?"]  # type: ignore[attr-defined]
    texts = harness.session.texts()
    assert "Ответ на: что с проектами? &lt;b&gt;" in texts
    assert harness.app.chat.resets == 1  # type: ignore[attr-defined]
    assert "Не знаю такой команды" in texts[-1]


async def test_pending_action_confirmation(harness: Harness) -> None:
    project = await approved_project(harness.services)
    await harness.text("/release todo-bot")
    action_buttons = harness.session.buttons()
    assert action_buttons[0][0] == "Подтвердить"

    await harness.press(action_buttons[0][1])

    assert harness.app.orchestrator.calls == [("release", project.id)]  # type: ignore[attr-defined]
    assert "released" in harness.session.last_text()
    assert isinstance(project, Project)
