"""Модели БД. Описание полей и смысл статусов — docs/data-model.md."""

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from dayforge.core.enums import (
    AdhocWhen,
    AgentRole,
    CalendarDateKind,
    CommitKind,
    DayStatus,
    DayTrigger,
    MilestoneStatus,
    PauseReason,
    PendingActionStatus,
    PlanAuthor,
    PlanStatus,
    ProjectStatus,
    QuestionKind,
    QuestionStatus,
    RunMode,
    RunStatus,
    RunTrigger,
    SessionOutcome,
    SessionStatus,
    TaskOrigin,
    TaskStatus,
)
from dayforge.db.base import Base, CreatedAtMixin, IdMixin, UTCDateTime, str_enum, utcnow

JsonDict = dict[str, Any]


def _project_fk() -> Mapped[int]:
    return mapped_column(sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class Project(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "projects"

    slug: Mapped[str] = mapped_column(sa.String(64), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    badge: Mapped[str] = mapped_column(sa.String(8))
    idea: Mapped[str] = mapped_column(sa.Text)
    stack_hint: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[ProjectStatus] = mapped_column(
        str_enum(ProjectStatus), default=ProjectStatus.DRAFT
    )
    priority: Mapped[int]
    planned_days: Mapped[int]
    current_day: Mapped[int] = mapped_column(default=0)
    planner_agent: Mapped[JsonDict] = mapped_column(sa.JSON)
    executor_agent: Mapped[JsonDict] = mapped_column(sa.JSON)
    test_command: Mapped[str | None] = mapped_column(sa.Text)
    # Цели дней из всех утверждённых версий плана: {"3": "Категории"}. Нужны PLAN.md,
    # потому что выполненные дни в новые версии плана не входят.
    day_goals: Mapped[dict[str, str]] = mapped_column(
        sa.JSON, default=dict, server_default=sa.text("'{}'")
    )
    repo_path: Mapped[str] = mapped_column(sa.Text)
    vcs_provider: Mapped[str] = mapped_column(sa.String(32), default="github")
    remote_url: Mapped[str | None] = mapped_column(sa.Text)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Plan(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "plans"
    __table_args__ = (sa.UniqueConstraint("project_id", "version"),)

    project_id: Mapped[int] = _project_fk()
    version: Mapped[int]
    status: Mapped[PlanStatus] = mapped_column(str_enum(PlanStatus), default=PlanStatus.PROPOSED)
    content: Mapped[JsonDict] = mapped_column(sa.JSON)
    summary: Mapped[str] = mapped_column(sa.Text)
    change_summary: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[PlanAuthor] = mapped_column(str_enum(PlanAuthor))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Task(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (sa.UniqueConstraint("project_id", "key"),)

    project_id: Mapped[int] = _project_fk()
    key: Mapped[str] = mapped_column(sa.String(16))
    origin: Mapped[TaskOrigin] = mapped_column(str_enum(TaskOrigin))
    day_number: Mapped[int | None]
    position: Mapped[int] = mapped_column(default=0)
    title: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str] = mapped_column(sa.Text)
    acceptance_criteria: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    depends_on: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    milestone_key: Mapped[str | None] = mapped_column(sa.String(16))
    adhoc_when: Mapped[AdhocWhen | None] = mapped_column(str_enum(AdhocWhen))
    status: Mapped[TaskStatus] = mapped_column(str_enum(TaskStatus), default=TaskStatus.PENDING)
    pause_reason: Mapped[PauseReason | None] = mapped_column(str_enum(PauseReason))
    branch: Mapped[str | None] = mapped_column(sa.String(128))
    attempts: Mapped[int] = mapped_column(default=0)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    decisions: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    merge_sha: Mapped[str | None] = mapped_column(sa.String(64))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Milestone(IdMixin, Base):
    __tablename__ = "milestones"
    __table_args__ = (sa.UniqueConstraint("project_id", "key"),)

    project_id: Mapped[int] = _project_fk()
    key: Mapped[str] = mapped_column(sa.String(16))
    title: Mapped[str] = mapped_column(sa.String(200))
    version: Mapped[str] = mapped_column(sa.String(32))
    status: Mapped[MilestoneStatus] = mapped_column(
        str_enum(MilestoneStatus), default=MilestoneStatus.PENDING
    )


class CalendarWeekday(IdMixin, Base):
    __tablename__ = "calendar_weekdays"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "weekday"),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
    )

    project_id: Mapped[int] = _project_fk()
    weekday: Mapped[int]


class CalendarDate(IdMixin, Base):
    __tablename__ = "calendar_dates"
    __table_args__ = (sa.UniqueConstraint("project_id", "date"),)

    # NULL — правило для всех проектов (общий выходной).
    project_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date] = mapped_column(sa.Date, index=True)
    kind: Mapped[CalendarDateKind] = mapped_column(str_enum(CalendarDateKind))


class DayExecution(IdMixin, Base):
    __tablename__ = "day_executions"
    __table_args__ = (sa.UniqueConstraint("project_id", "day_number"),)

    project_id: Mapped[int] = _project_fk()
    day_number: Mapped[int]
    date: Mapped[date] = mapped_column(sa.Date)
    trigger: Mapped[DayTrigger] = mapped_column(str_enum(DayTrigger))
    status: Mapped[DayStatus] = mapped_column(str_enum(DayStatus), default=DayStatus.IN_PROGRESS)


class Run(IdMixin, Base):
    __tablename__ = "runs"

    project_id: Mapped[int] = _project_fk()
    batch_id: Mapped[str] = mapped_column(sa.String(36), index=True)
    mode: Mapped[RunMode] = mapped_column(str_enum(RunMode))
    trigger: Mapped[RunTrigger] = mapped_column(str_enum(RunTrigger))
    slot_date: Mapped[date | None] = mapped_column(sa.Date)
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus), default=RunStatus.QUEUED)
    day_number: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(sa.Text)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AgentSession(IdMixin, Base):
    __tablename__ = "agent_sessions"

    role: Mapped[AgentRole] = mapped_column(str_enum(AgentRole))
    project_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[int | None] = mapped_column(sa.ForeignKey("runs.id", ondelete="SET NULL"))
    agent: Mapped[str] = mapped_column(sa.String(32))
    model: Mapped[str] = mapped_column(sa.String(64))
    effort: Mapped[str | None] = mapped_column(sa.String(16))
    cli_session_id: Mapped[str | None] = mapped_column(sa.String(128))
    mcp_token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    status: Mapped[SessionStatus] = mapped_column(
        str_enum(SessionStatus), default=SessionStatus.RUNNING
    )
    outcome: Mapped[SessionOutcome | None] = mapped_column(str_enum(SessionOutcome))
    usage: Mapped[JsonDict] = mapped_column(sa.JSON, default=dict)
    log_path: Mapped[str] = mapped_column(sa.Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Question(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "questions"

    project_id: Mapped[int] = _project_fk()
    task_id: Mapped[int | None] = mapped_column(sa.ForeignKey("tasks.id", ondelete="SET NULL"))
    session_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("agent_sessions.id", ondelete="SET NULL")
    )
    kind: Mapped[QuestionKind] = mapped_column(str_enum(QuestionKind))
    payload: Mapped[JsonDict] = mapped_column(sa.JSON)
    # У секретов значение здесь не хранится — только в .env проекта.
    answers: Mapped[list[str] | None] = mapped_column(sa.JSON)
    status: Mapped[QuestionStatus] = mapped_column(
        str_enum(QuestionStatus), default=QuestionStatus.OPEN
    )
    tg_message_ids: Mapped[list[int]] = mapped_column(sa.JSON, default=list)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Secret(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "secrets"
    __table_args__ = (sa.UniqueConstraint("project_id", "name"),)

    project_id: Mapped[int] = _project_fk()
    name: Mapped[str] = mapped_column(sa.String(64))
    description: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Commit(Base):
    __tablename__ = "commits"

    sha: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    project_id: Mapped[int] = _project_fk()
    task_id: Mapped[int | None] = mapped_column(sa.ForeignKey("tasks.id", ondelete="SET NULL"))
    branch: Mapped[str] = mapped_column(sa.String(128))
    message: Mapped[str] = mapped_column(sa.Text)
    kind: Mapped[CommitKind] = mapped_column(str_enum(CommitKind))
    committed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Release(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "releases"

    project_id: Mapped[int] = _project_fk()
    milestone_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("milestones.id", ondelete="SET NULL")
    )
    tag: Mapped[str] = mapped_column(sa.String(32))
    sha: Mapped[str] = mapped_column(sa.String(64))


class Report(IdMixin, Base):
    __tablename__ = "reports"

    run_id: Mapped[int | None] = mapped_column(sa.ForeignKey("runs.id", ondelete="SET NULL"))
    project_id: Mapped[int] = _project_fk()
    text: Mapped[str] = mapped_column(sa.Text)
    data: Mapped[JsonDict] = mapped_column(sa.JSON)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PendingAction(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "pending_actions"

    kind: Mapped[str] = mapped_column(sa.String(32))
    payload: Mapped[JsonDict] = mapped_column(sa.JSON)
    session_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("agent_sessions.id", ondelete="SET NULL")
    )
    status: Mapped[PendingActionStatus] = mapped_column(
        str_enum(PendingActionStatus), default=PendingActionStatus.PENDING
    )
    tg_message_id: Mapped[int | None]
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(sa.JSON)


class BotValue(Base):
    """Ключ–значение для бота: состояния диалогов (FSM), частичные ответы, служебные связи."""

    __tablename__ = "bot_kv"

    key: Mapped[str] = mapped_column(sa.String(200), primary_key=True)
    value: Mapped[Any] = mapped_column(sa.JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
