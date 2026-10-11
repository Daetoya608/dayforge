"""Блокирующие вопросы агентов и запросы секретов (docs/run-lifecycle.md#пауза-и-продолжение)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import PauseReason, ProjectStatus, QuestionKind, QuestionStatus, TaskStatus
from dayforge.core.events import (
    Notifier,
    ProjectRef,
    QuestionAnswered,
    QuestionAsked,
    SecretRequested,
)
from dayforge.core.secrets import SECRET_NAME_RE, EnvFile
from dayforge.db.base import utcnow
from dayforge.db.models import Project, Question, Secret, Task

# До утверждения плана ответы планировщику дописываются в docs/IDEA.md (docs/agents.md).
PRE_PLAN_STATUSES = (
    ProjectStatus.DRAFT,
    ProjectStatus.CLARIFYING,
    ProjectStatus.PLANNING,
    ProjectStatus.PLAN_REVIEW,
)
IDEA_SECTION = "## Уточнения"


class QuestionError(ValueError):
    pass


@dataclass(frozen=True)
class AnswerOutcome:
    question_id: int
    project_id: int
    task_id: int | None
    session_id: int | None
    project_unblocked: bool


class QuestionService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], notifier: Notifier
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier

    async def ask(
        self,
        *,
        project_id: int,
        task_id: int | None,
        session_id: int | None,
        questions: list[dict[str, Any]],
        context: str | None = None,
    ) -> int:
        """Регистрирует вопросы; задача встаёт на паузу, активный проект — в blocked."""
        normalized = [_normalize_question(q) for q in questions]
        async with self._session_factory() as session, session.begin():
            project, task = await self._pause(session, project_id, task_id, PauseReason.QUESTION)
            question = Question(
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                kind=QuestionKind.QUESTION,
                payload={"questions": normalized, "context": context},
            )
            session.add(question)
            await session.flush()
            event = QuestionAsked(
                question_id=question.id,
                project=ProjectRef.of(project),
                task_key=task.key if task else None,
                context=context,
                questions=normalized,
            )
        await self._notifier.send(event)
        return question.id

    async def request_secret(
        self,
        *,
        project_id: int,
        task_id: int | None,
        session_id: int | None,
        name: str,
        description: str,
        how_to_get: str | None = None,
    ) -> int | None:
        """None — секрет уже есть в .env; иначе id запроса, а задача встаёт на паузу."""
        if not SECRET_NAME_RE.fullmatch(name):
            raise QuestionError(f"имя секрета {name!r}: заглавные латинские буквы, цифры и _")
        async with self._session_factory() as session, session.begin():
            project = await _get(session, Project, project_id)
            if _env(project).has(name):
                return None
            project, task = await self._pause(session, project_id, task_id, PauseReason.SECRET)
            question = Question(
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                kind=QuestionKind.SECRET,
                payload={"name": name, "description": description, "how_to_get": how_to_get},
            )
            session.add(question)
            await session.flush()
            event = SecretRequested(
                question_id=question.id,
                project=ProjectRef.of(project),
                task_key=task.key if task else None,
                name=name,
                description=description,
                how_to_get=how_to_get,
            )
        await self._notifier.send(event)
        return question.id

    async def answer(self, question_id: int, answers: list[str]) -> AnswerOutcome:
        async with self._session_factory() as session, session.begin():
            question = await self._open_question(session, question_id, QuestionKind.QUESTION)
            items = question.payload["questions"]
            if len(answers) != len(items):
                raise QuestionError(f"нужно {len(items)} ответ(а), получено {len(answers)}")
            for item, answer in zip(items, answers, strict=True):
                if not answer.strip():
                    raise QuestionError("пустой ответ")
                if not item["allow_free_text"] and answer not in item["options"]:
                    raise QuestionError(f"ответ должен быть одним из: {', '.join(item['options'])}")
            question.answers = [a.strip() for a in answers]
            project = await _get(session, Project, question.project_id)
            if question.task_id is None and project.status in PRE_PLAN_STATUSES:
                _append_to_idea(project, items, question.answers)
            outcome = await self._close(session, question, project)
        await self._notify_answered(outcome)
        return outcome

    async def provide_secret(self, question_id: int, value: str) -> AnswerOutcome:
        """Значение — в .env проекта; в БД остаются только имя и описание."""
        if not value.strip():
            raise QuestionError("пустое значение секрета")
        async with self._session_factory() as session, session.begin():
            question = await self._open_question(session, question_id, QuestionKind.SECRET)
            project = await _get(session, Project, question.project_id)
            name = question.payload["name"]
            _env(project).set(name, value.strip())
            secret = await session.scalar(
                select(Secret).where(Secret.project_id == project.id, Secret.name == name)
            )
            if secret is None:
                session.add(
                    Secret(
                        project_id=project.id,
                        name=name,
                        description=question.payload["description"],
                    )
                )
            else:
                secret.updated_at = utcnow()
            outcome = await self._close(session, question, project)
        await self._notify_answered(outcome)
        return outcome

    async def get(self, question_id: int) -> Question:
        async with self._session_factory() as session:
            return await _get(session, Question, question_id)

    async def open_questions(self, project_id: int | None = None) -> list[Question]:
        query = select(Question).where(Question.status == QuestionStatus.OPEN).order_by(Question.id)
        if project_id is not None:
            query = query.where(Question.project_id == project_id)
        async with self._session_factory() as session:
            return list(await session.scalars(query))

    async def has_open_question(self, *, project_id: int, task_id: int | None = None) -> bool:
        conditions = [Question.project_id == project_id, Question.status == QuestionStatus.OPEN]
        if task_id is not None:
            conditions.append(Question.task_id == task_id)
        async with self._session_factory() as session:
            return bool(await session.scalar(select(exists().where(*conditions))))

    async def set_message_ids(self, question_id: int, message_ids: list[int]) -> None:
        async with self._session_factory() as session, session.begin():
            question = await _get(session, Question, question_id)
            question.tg_message_ids = message_ids

    async def due_reminders(self, *, now: datetime, every: timedelta) -> list[Question]:
        """Открытые вопросы, о которых пора напомнить; отметка о напоминании ставится сразу."""
        async with self._session_factory() as session, session.begin():
            questions = await session.scalars(
                select(Question).where(Question.status == QuestionStatus.OPEN)
            )
            due = [q for q in questions if (q.last_reminded_at or q.created_at) <= now - every]
            for question in due:
                question.last_reminded_at = now
            return due

    async def cancel(self, question_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            question = await _get(session, Question, question_id)
            question.status = QuestionStatus.CANCELLED
            project = await _get(session, Project, question.project_id)
            await self._unblock_if_answered(session, project)

    @staticmethod
    async def _pause(
        session: AsyncSession, project_id: int, task_id: int | None, reason: PauseReason
    ) -> tuple[Project, Task | None]:
        project = await _get(session, Project, project_id)
        task = await _get(session, Task, task_id) if task_id is not None else None
        if task is not None:
            task.status = TaskStatus.PAUSED
            task.pause_reason = reason
        if project.status is ProjectStatus.ACTIVE:
            project.status = ProjectStatus.BLOCKED
        return project, task

    @staticmethod
    async def _open_question(
        session: AsyncSession, question_id: int, kind: QuestionKind
    ) -> Question:
        question = await _get(session, Question, question_id)
        if question.kind is not kind:
            raise QuestionError(f"запрос {question_id} — не {kind}")
        if question.status is not QuestionStatus.OPEN:
            raise QuestionError(f"на запрос {question_id} уже ответили или он отменён")
        return question

    async def _close(
        self, session: AsyncSession, question: Question, project: Project
    ) -> AnswerOutcome:
        question.status = QuestionStatus.ANSWERED
        question.answered_at = utcnow()
        await session.flush()
        unblocked = await self._unblock_if_answered(session, project)
        return AnswerOutcome(
            question_id=question.id,
            project_id=project.id,
            task_id=question.task_id,
            session_id=question.session_id,
            project_unblocked=unblocked,
        )

    @staticmethod
    async def _unblock_if_answered(session: AsyncSession, project: Project) -> bool:
        still_open = await session.scalar(
            select(
                exists().where(
                    Question.project_id == project.id, Question.status == QuestionStatus.OPEN
                )
            )
        )
        if still_open:
            return False
        if project.status is ProjectStatus.BLOCKED:
            project.status = ProjectStatus.ACTIVE
        return True

    async def _notify_answered(self, outcome: AnswerOutcome) -> None:
        async with self._session_factory() as session:
            project = await _get(session, Project, outcome.project_id)
            task = await session.get(Task, outcome.task_id) if outcome.task_id else None
        await self._notifier.send(
            QuestionAnswered(
                question_id=outcome.question_id,
                project=ProjectRef.of(project),
                task_key=task.key if task else None,
                project_unblocked=outcome.project_unblocked,
            )
        )


def _normalize_question(raw: dict[str, Any]) -> dict[str, Any]:
    text = str(raw.get("text", "")).strip()
    if not text:
        raise QuestionError("у вопроса нет текста")
    options = [str(o).strip() for o in raw.get("options") or [] if str(o).strip()]
    allow_free_text = bool(raw.get("allow_free_text", True)) or not options
    return {"text": text, "options": options, "allow_free_text": allow_free_text}


def _env(project: Project) -> EnvFile:
    return EnvFile(Path(project.repo_path) / ".env")


def _append_to_idea(project: Project, items: list[dict[str, Any]], answers: list[str]) -> None:
    path = Path(project.repo_path) / "docs" / "IDEA.md"
    if not path.exists():
        return
    block = "\n".join(
        f"**Вопрос:** {item['text']}\n**Ответ:** {answer}\n"
        for item, answer in zip(items, answers, strict=True)
    )
    text = path.read_text()
    if IDEA_SECTION not in text:
        text = text.rstrip("\n") + f"\n\n{IDEA_SECTION}\n"
    path.write_text(text.rstrip("\n") + "\n\n" + block)


async def _get[M](session: AsyncSession, model: type[M], id_: int) -> M:
    row = await session.get(model, id_)
    if row is None:
        raise QuestionError(f"{model.__name__} {id_} не найден")
    return row
