from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram.methods import EditMessageText, SendDocument
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.bot.notifier import TelegramNotifier
from dayforge.bot.storage import BotKV
from dayforge.core import events as ev
from dayforge.core.questions import QuestionService
from dayforge.db.models import Project, Question
from dayforge.services import Services, build_services
from tests.bot_helpers import OWNER_ID, FakeSession, make_bot

REF = ev.ProjectRef(id=1, slug="todo-bot", name="Todo <bot>", badge="🟦")


@pytest.fixture
def bot_session() -> tuple[TelegramNotifier, FakeSession]:
    bot, session = make_bot()
    return TelegramNotifier(bot, OWNER_ID), session


@pytest.fixture
def services(
    session_factory: async_sessionmaker[AsyncSession],
    bot_session: tuple[TelegramNotifier, FakeSession],
    tmp_path: Path,
) -> Services:
    notifier, _ = bot_session
    services = build_services(
        session_factory,
        notifier,
        projects_dir=tmp_path / "ws",
        sessions_log_dir=tmp_path / "logs",
        vcs_provider=None,
    )
    notifier.attach(services, BotKV(session_factory))
    return services


async def test_questions_are_sent_one_message_each_and_ids_stored(
    services: Services,
    bot_session: tuple[TelegramNotifier, FakeSession],
    active_project: Project,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, session = bot_session
    questions = QuestionService(session_factory, services.notifier)

    question_id = await questions.ask(
        project_id=active_project.id,
        task_id=None,
        session_id=None,
        questions=[
            {"text": "Провайдер?", "options": ["Stripe", "ЮKassa"], "allow_free_text": False},
            {"text": "Валюта?"},
        ],
        context="Нужны платежи",
    )

    texts = session.texts()
    assert len(texts) == 2
    assert texts[0].startswith("❓ 🟦 Todo · вопрос 1 из 2\nНужны платежи")
    assert "Или ответь" not in texts[0] and "Или ответь" in texts[1]
    assert [b[0] for b in session.buttons(session.sent()[0])] == ["Stripe", "ЮKassa"]
    async with session_factory() as db:
        stored = await db.get(Question, question_id)
    assert stored is not None and stored.tg_message_ids == [100, 101]


async def test_plan_proposal_buttons_and_diff(
    services: Services, bot_session: tuple[TelegramNotifier, FakeSession]
) -> None:
    notifier, session = bot_session

    await notifier.send(
        ev.PlanProposed(
            project=REF,
            plan_id=7,
            version=2,
            summary="Бот",
            change_summary="Экспорт",
            days=10,
            milestones=[("M1", "База", "v0.1.0")],
            warnings=["План на 12 дн., заказано 10"],
            diff=["+ T5 Экспорт (день 3)"],
            forecast_end="2026-11-13",
            missing_days=0,
        )
    )

    text = session.last_text()
    assert "📐 Перепланирование 🟦 <b>Todo &lt;bot&gt;</b> · план v2 на 10 дн." in text
    assert "Прогноз окончания: 13.11.2026" in text
    assert "+ T5 Экспорт (день 3)" in text
    assert [b[1] for b in session.buttons()] == [
        "plan:file:7",
        "plan:arch:7",
        "plan:approve:7",
        "plan:edit:7",
        "plan:reject:7",
    ]


async def test_long_report_goes_as_document(
    services: Services, bot_session: tuple[TelegramNotifier, FakeSession]
) -> None:
    notifier, session = bot_session

    await notifier.send(ev.RunReport(project=REF, run_id=1, text="<b>x</b>" + "строка\n" * 1000))

    (call,) = session.sent()
    assert isinstance(call, SendDocument)
    assert "<b>" not in (call.caption or "")


async def test_expired_action_edits_message(
    services: Services, bot_session: tuple[TelegramNotifier, FakeSession]
) -> None:
    notifier, session = bot_session

    await notifier.send(ev.PendingActionExpired(action_id=1, tg_message_id=555))

    (call,) = session.calls
    assert isinstance(call, EditMessageText) and call.message_id == 555


async def test_events_render_without_errors(
    services: Services, bot_session: tuple[TelegramNotifier, FakeSession]
) -> None:
    notifier, session = bot_session
    events: list[ev.Event] = [
        ev.TaskPaused(project=REF, task_key="T1", reason="тесты не прошли", details="FAILED"),
        ev.ProjectSkipped(project=REF, reason="ждёт ответа"),
        ev.LimitReached(
            project=REF, cancelled=[REF], resets_at=datetime.now(UTC), auto_resume=False
        ),
        ev.RunFailed(project=REF, run_id=1, error="boom"),
        ev.MilestoneReleased(project=REF, tag="v0.1.0", title="База", url="https://x"),
        ev.ProjectCompleted(project=REF, url=None, days=3, tasks_done=5, versions=["v0.1.0"]),
        ev.SystemRestarted(interrupted=[(REF, "T1")]),
        ev.PlanningFailed(project=REF, reason="ошибка"),
        ev.DaysShortage(project=REF, missing_days=3),
        ev.BackupFailed(error="restic"),
        ev.TaskFailed(project=REF, task_id=3, task_key="T3", reason="нет API", suggestion="CSV"),
        ev.PlanChangeProposed(project=REF, task_key="T2", reason="связаны", proposal="объединить"),
    ]

    for event in events:
        await notifier.send(event)

    assert len(session.sent()) == len(events)
    assert "не хватает 3 дн." in session.texts()[8]
    assert session.buttons(session.sent()[8])[0] == ("+3 будней", "short:1:3")
