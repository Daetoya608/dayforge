"""События ядра.

Ядро не знает про Telegram: оно отдаёт события Notifier, а бот решает, как их показать.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from dayforge.db.base import utcnow

if TYPE_CHECKING:
    from dayforge.db.models import Project


@dataclass(frozen=True, kw_only=True)
class Event:
    at: datetime = field(default_factory=utcnow)


class Notifier(Protocol):
    async def send(self, event: Event) -> None: ...


@dataclass(frozen=True, kw_only=True)
class ProjectRef:
    id: int
    slug: str
    name: str
    badge: str

    @classmethod
    def of(cls, project: "Project") -> "ProjectRef":
        return cls(id=project.id, slug=project.slug, name=project.name, badge=project.badge)


@dataclass(frozen=True, kw_only=True)
class QuestionAsked(Event):
    question_id: int
    project: ProjectRef
    task_key: str | None
    context: str | None
    # [{"text": …, "options": [...], "allow_free_text": bool}]
    questions: list[dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class SecretRequested(Event):
    question_id: int
    project: ProjectRef
    task_key: str | None
    name: str
    description: str
    how_to_get: str | None


@dataclass(frozen=True, kw_only=True)
class QuestionAnswered(Event):
    question_id: int
    project: ProjectRef
    task_key: str | None
    # Все вопросы проекта отвечены — можно предложить «Продолжить сейчас».
    project_unblocked: bool


@dataclass(frozen=True, kw_only=True)
class PendingActionCreated(Event):
    action_id: int
    description: str


class RecordingNotifier:
    """Notifier для тестов и отладки: просто запоминает события."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def send(self, event: Event) -> None:
        self.events.append(event)

    def of_type[E: Event](self, kind: type[E]) -> list[E]:
        return [e for e in self.events if isinstance(e, kind)]


@dataclass(frozen=True, kw_only=True)
class PlanProposed(Event):
    project: ProjectRef
    plan_id: int
    version: int
    summary: str
    change_summary: str | None
    days: int
    # [(key, title, version)]
    milestones: list[tuple[str, str, str]]
    warnings: list[str]
    # None — первый план проекта.
    diff: list[str] | None
    forecast_end: str | None
    missing_days: int


@dataclass(frozen=True, kw_only=True)
class TaskFailed(Event):
    project: ProjectRef
    task_id: int
    task_key: str
    reason: str
    suggestion: str | None


@dataclass(frozen=True, kw_only=True)
class PlanChangeProposed(Event):
    project: ProjectRef
    task_key: str | None
    reason: str
    proposal: str


class LogNotifier:
    """Notifier, который только пишет события в лог — пока бот не подключён."""

    async def send(self, event: Event) -> None:
        import logging

        logging.getLogger("dayforge.events").info("%s", event)


@dataclass(frozen=True, kw_only=True)
class TaskPaused(Event):
    project: ProjectRef
    task_key: str
    reason: str
    details: str | None = None


@dataclass(frozen=True, kw_only=True)
class ProjectSkipped(Event):
    project: ProjectRef
    reason: str


@dataclass(frozen=True, kw_only=True)
class LimitReached(Event):
    project: ProjectRef
    cancelled: list[ProjectRef]
    resets_at: datetime | None
    auto_resume: bool


@dataclass(frozen=True, kw_only=True)
class RunReport(Event):
    project: ProjectRef
    run_id: int
    text: str


@dataclass(frozen=True, kw_only=True)
class RunFailed(Event):
    project: ProjectRef
    run_id: int
    error: str


@dataclass(frozen=True, kw_only=True)
class MilestoneReleased(Event):
    project: ProjectRef
    tag: str
    title: str
    url: str | None


@dataclass(frozen=True, kw_only=True)
class ProjectCompleted(Event):
    project: ProjectRef
    url: str | None
    days: int
    tasks_done: int
    versions: list[str]


@dataclass(frozen=True, kw_only=True)
class SystemRestarted(Event):
    # [(проект, ключ задачи)]
    interrupted: list[tuple[ProjectRef, str]]
