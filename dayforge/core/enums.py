"""Статусы и типы предметной области. Значения хранятся в БД строками."""

from enum import StrEnum


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    CLARIFYING = "clarifying"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    ACTIVE = "active"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class PlanStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class PlanAuthor(StrEnum):
    PLANNER = "planner"
    SYSTEM_AGENT = "system_agent"


class TaskOrigin(StrEnum):
    PLAN = "plan"
    ADHOC = "adhoc"


class AdhocWhen(StrEnum):
    NOW = "now"
    NEXT = "next"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    PAUSED = "paused"
    FAILED = "failed"
    DONE = "done"
    CANCELLED = "cancelled"


class PauseReason(StrEnum):
    QUESTION = "question"
    SECRET = "secret"
    LIMIT = "limit"
    WATCHDOG = "watchdog"
    TESTS_FAILED = "tests_failed"
    NO_RESULT = "no_result"
    ERROR = "error"
    INTERRUPTED = "interrupted"


class MilestoneStatus(StrEnum):
    PENDING = "pending"
    RELEASED = "released"


class CalendarDateKind(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


class DayTrigger(StrEnum):
    SCHEDULED = "scheduled"
    NEXT = "next"


class DayStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    DONE = "done"


class RunMode(StrEnum):
    SCHEDULED = "scheduled"
    NEXT = "next"
    CONTINUE = "continue"


class RunTrigger(StrEnum):
    SCHEDULER = "scheduler"
    BOT = "bot"
    SYSTEM_AGENT = "system_agent"
    AUTO_RESUME = "auto_resume"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    BLOCKED = "blocked"
    LIMIT = "limit"
    SKIPPED = "skipped"
    ERROR = "error"
    CANCELLED = "cancelled"


class AgentRole(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    SYSTEM = "system"


class SessionStatus(StrEnum):
    RUNNING = "running"
    ENDED = "ended"
    PAUSED = "paused"
    CRASHED = "crashed"
    KILLED = "killed"


class SessionOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    QUESTION = "question"
    SECRET = "secret"
    PLAN_SUBMITTED = "plan_submitted"
    LIMIT = "limit"
    WATCHDOG = "watchdog"
    NO_RESULT = "no_result"
    ERROR = "error"


class QuestionKind(StrEnum):
    QUESTION = "question"
    SECRET = "secret"


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CANCELLED = "cancelled"


class CommitKind(StrEnum):
    AGENT = "agent"
    SYSTEM = "system"


class PendingActionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"
