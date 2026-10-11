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
