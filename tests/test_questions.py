from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import PauseReason, ProjectStatus, QuestionStatus, TaskStatus
from dayforge.core.events import QuestionAnswered, QuestionAsked, RecordingNotifier, SecretRequested
from dayforge.core.questions import QuestionError, QuestionService
from dayforge.core.secrets import EnvFile
from dayforge.db.base import utcnow
from dayforge.db.models import Project, Question, Secret, Task

SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def questions(session_factory: SessionFactory, notifier: RecordingNotifier) -> QuestionService:
    return QuestionService(session_factory, notifier)


async def task_id(session_factory: SessionFactory, project: Project) -> int:
    async with session_factory() as session:
        return (await session.scalars(select(Task.id).where(Task.project_id == project.id))).one()


async def state(session_factory: SessionFactory, project: Project) -> tuple[Project, Task]:
    async with session_factory() as session:
        stored = await session.get(Project, project.id)
        task = (await session.scalars(select(Task).where(Task.project_id == project.id))).one()
    assert stored is not None
    return stored, task


async def test_ask_pauses_task_blocks_project_and_notifies(
    questions: QuestionService,
    session_factory: SessionFactory,
    active_project: Project,
    notifier: RecordingNotifier,
) -> None:
    question_id = await questions.ask(
        project_id=active_project.id,
        task_id=await task_id(session_factory, active_project),
        session_id=None,
        questions=[{"text": "Какой провайдер?", "options": ["Stripe", "ЮKassa"]}],
        context="Нужен приём платежей",
    )

    project, task = await state(session_factory, active_project)
    assert project.status is ProjectStatus.BLOCKED
    assert (task.status, task.pause_reason) == (TaskStatus.PAUSED, PauseReason.QUESTION)
    (event,) = notifier.of_type(QuestionAsked)
    assert event.question_id == question_id
    assert event.task_key == "T1"
    assert event.questions == [
        {"text": "Какой провайдер?", "options": ["Stripe", "ЮKassa"], "allow_free_text": True}
    ]


async def test_answer_unblocks_project_when_nothing_else_is_open(
    questions: QuestionService,
    session_factory: SessionFactory,
    active_project: Project,
    notifier: RecordingNotifier,
) -> None:
    tid = await task_id(session_factory, active_project)
    first = await questions.ask(
        project_id=active_project.id, task_id=tid, session_id=None, questions=[{"text": "A?"}]
    )
    second = await questions.ask(
        project_id=active_project.id, task_id=tid, session_id=None, questions=[{"text": "B?"}]
    )

    outcome = await questions.answer(first, ["да"])
    assert not outcome.project_unblocked
    assert (await state(session_factory, active_project))[0].status is ProjectStatus.BLOCKED

    outcome = await questions.answer(second, ["нет"])
    assert outcome.project_unblocked
    project, task = await state(session_factory, active_project)
    assert project.status is ProjectStatus.ACTIVE
    # Задачу продолжает раннер — до тех пор она на паузе.
    assert task.status is TaskStatus.PAUSED
    assert [e.project_unblocked for e in notifier.of_type(QuestionAnswered)] == [False, True]


async def test_answer_validation(
    questions: QuestionService, session_factory: SessionFactory, active_project: Project
) -> None:
    question_id = await questions.ask(
        project_id=active_project.id,
        task_id=await task_id(session_factory, active_project),
        session_id=None,
        questions=[{"text": "Какой?", "options": ["A", "B"], "allow_free_text": False}],
    )

    with pytest.raises(QuestionError, match="нужно 1"):
        await questions.answer(question_id, ["A", "B"])
    with pytest.raises(QuestionError, match="одним из"):
        await questions.answer(question_id, ["C"])

    await questions.answer(question_id, ["B"])
    with pytest.raises(QuestionError, match="уже ответили"):
        await questions.answer(question_id, ["A"])


async def test_planner_answers_go_to_idea_before_approval(
    questions: QuestionService, session_factory: SessionFactory, active_project: Project
) -> None:
    async with session_factory() as session, session.begin():
        project = await session.get(Project, active_project.id)
        assert project is not None
        project.status = ProjectStatus.CLARIFYING

    question_id = await questions.ask(
        project_id=active_project.id,
        task_id=None,
        session_id=None,
        questions=[{"text": "Валюта?"}, {"text": "Мультиюзер?"}],
    )
    await questions.answer(question_id, ["Рубли", "Нет"])

    idea = (Path(active_project.repo_path) / "docs" / "IDEA.md").read_text()
    assert idea.endswith(
        "**Вопрос:** Валюта?\n**Ответ:** Рубли\n\n**Вопрос:** Мультиюзер?\n**Ответ:** Нет\n"
    )
    project, _ = await state(session_factory, active_project)
    assert project.status is ProjectStatus.CLARIFYING  # до утверждения не блокируется


async def test_secret_already_present_is_not_requested(
    questions: QuestionService, session_factory: SessionFactory, active_project: Project
) -> None:
    EnvFile(Path(active_project.repo_path) / ".env").set("STRIPE_API_KEY", "x")

    result = await questions.request_secret(
        project_id=active_project.id,
        task_id=await task_id(session_factory, active_project),
        session_id=None,
        name="STRIPE_API_KEY",
        description="ключ",
    )

    assert result is None
    assert (await state(session_factory, active_project))[1].status is TaskStatus.RUNNING


async def test_secret_request_and_value(
    questions: QuestionService,
    session_factory: SessionFactory,
    active_project: Project,
    notifier: RecordingNotifier,
) -> None:
    question_id = await questions.request_secret(
        project_id=active_project.id,
        task_id=await task_id(session_factory, active_project),
        session_id=None,
        name="STRIPE_API_KEY",
        description="ключ Stripe",
        how_to_get="Dashboard → API keys",
    )
    assert question_id is not None
    (event,) = notifier.of_type(SecretRequested)
    assert event.name == "STRIPE_API_KEY"
    _, task = await state(session_factory, active_project)
    assert task.pause_reason is PauseReason.SECRET

    outcome = await questions.provide_secret(question_id, "sk_test_1")

    assert outcome.project_unblocked
    assert EnvFile(Path(active_project.repo_path) / ".env").values() == ["sk_test_1"]
    async with session_factory() as session:
        question = await session.get(Question, question_id)
        secret = (await session.scalars(select(Secret))).one()
    assert question is not None and question.answers is None  # значение в БД не попадает
    assert secret.name == "STRIPE_API_KEY"


async def test_invalid_secret_name(questions: QuestionService, active_project: Project) -> None:
    with pytest.raises(QuestionError):
        await questions.request_secret(
            project_id=active_project.id, task_id=None, session_id=None, name="x", description=""
        )


async def test_reminders_and_cancel(
    questions: QuestionService, session_factory: SessionFactory, active_project: Project
) -> None:
    question_id = await questions.ask(
        project_id=active_project.id,
        task_id=await task_id(session_factory, active_project),
        session_id=None,
        questions=[{"text": "?"}],
    )
    now = utcnow()

    assert await questions.due_reminders(now=now, every=timedelta(hours=24)) == []
    later = now + timedelta(hours=25)
    assert [q.id for q in await questions.due_reminders(now=later, every=timedelta(hours=24))] == [
        question_id
    ]
    assert await questions.due_reminders(now=later, every=timedelta(hours=24)) == []

    await questions.cancel(question_id)
    assert (await questions.get(question_id)).status is QuestionStatus.CANCELLED
    assert (await state(session_factory, active_project))[0].status is ProjectStatus.ACTIVE
