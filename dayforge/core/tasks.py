"""Задачи: очередь на запуск, дни плана, статусы, разовые задачи (docs/run-lifecycle.md)."""

import re
from collections.abc import Sequence
from datetime import date

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import (
    AdhocWhen,
    DayStatus,
    DayTrigger,
    PauseReason,
    QuestionStatus,
    TaskOrigin,
    TaskStatus,
)
from dayforge.db.base import utcnow
from dayforge.db.models import DayExecution, Project, Question, Task

# Причины паузы, после которых задачу можно продолжать без участия пользователя.
AUTO_RESUMABLE = (
    PauseReason.LIMIT,
    PauseReason.WATCHDOG,
    PauseReason.TESTS_FAILED,
    PauseReason.NO_RESULT,
    PauseReason.ERROR,
    PauseReason.INTERRUPTED,
)
FINISHED = (TaskStatus.DONE, TaskStatus.CANCELLED)


class TaskError(ValueError):
    pass


class TaskService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, task_id: int) -> Task:
        async with self._session_factory() as session:
            return await _task(session, task_id)

    async def add_adhoc(
        self,
        project_id: int,
        *,
        title: str,
        description: str,
        acceptance_criteria: Sequence[str] = (),
        when: AdhocWhen = AdhocWhen.NEXT,
    ) -> Task:
        async with self._session_factory() as session, session.begin():
            if await session.get(Project, project_id) is None:
                raise TaskError(f"проекта {project_id} нет")
            keys = await session.scalars(
                select(Task.key).where(
                    Task.project_id == project_id, Task.origin == TaskOrigin.ADHOC
                )
            )
            number = max((int(re.sub(r"\D", "", k) or 0) for k in keys), default=0) + 1
            task = Task(
                project_id=project_id,
                key=f"A{number}",
                origin=TaskOrigin.ADHOC,
                title=title.strip()[:200],
                description=description.strip(),
                acceptance_criteria=[c for c in acceptance_criteria if c.strip()],
                adhoc_when=when,
            )
            session.add(task)
        return task

    async def carry_over(self, project_id: int) -> list[Task]:
        """Что доделать в начале запуска, в порядке выполнения.

        1. Разовые задачи. 2. Задачи на паузе, которые можно продолжать: автоматические причины
        или вопрос, на который уже ответили. 3. Невыполненные задачи начатых дней
        (например, если запуск прервался по лимиту).
        """
        async with self._session_factory() as session:
            project = await _project(session, project_id)
            tasks = list(
                await session.scalars(
                    select(Task)
                    .where(Task.project_id == project_id, Task.status.not_in(FINISHED))
                    .order_by(Task.day_number, Task.position, Task.id)
                )
            )
            open_question_tasks = set(
                await session.scalars(
                    select(Question.task_id).where(
                        Question.project_id == project_id,
                        Question.status == QuestionStatus.OPEN,
                        Question.task_id.is_not(None),
                    )
                )
            )

        adhoc = [
            t for t in tasks if t.origin is TaskOrigin.ADHOC and t.status is TaskStatus.PENDING
        ]
        paused = [
            t
            for t in tasks
            if t.status is TaskStatus.PAUSED
            and (t.pause_reason in AUTO_RESUMABLE or t.id not in open_question_tasks)
        ]
        started_days = [
            t
            for t in tasks
            if t.origin is TaskOrigin.PLAN
            and t.status is TaskStatus.PENDING
            and t.day_number is not None
            and t.day_number <= project.current_day
        ]
        return adhoc + paused + started_days

    async def start_next_day(
        self, project_id: int, *, trigger: DayTrigger, today: date
    ) -> tuple[int, list[Task]] | None:
        """Сдвигает указатель дня и возвращает задачи нового дня; None — дни плана кончились."""
        async with self._session_factory() as session, session.begin():
            project = await _project(session, project_id)
            if project.current_day >= project.planned_days:
                return None
            project.current_day += 1
            session.add(
                DayExecution(
                    project_id=project_id,
                    day_number=project.current_day,
                    date=today,
                    trigger=trigger,
                )
            )
            tasks = list(
                await session.scalars(
                    select(Task)
                    .where(
                        Task.project_id == project_id,
                        Task.day_number == project.current_day,
                        Task.origin == TaskOrigin.PLAN,
                        Task.status == TaskStatus.PENDING,
                    )
                    .order_by(Task.position)
                )
            )
            return project.current_day, tasks

    async def mark_running(self, task_id: int, *, branch: str) -> Task:
        async with self._session_factory() as session, session.begin():
            task = await _task(session, task_id)
            task.status = TaskStatus.RUNNING
            task.pause_reason = None
            task.branch = branch
            task.attempts += 1
            task.started_at = task.started_at or utcnow()
            return task

    async def mark_verifying(self, task_id: int, *, summary: str, decisions: Sequence[str]) -> None:
        async with self._session_factory() as session, session.begin():
            task = await _task(session, task_id)
            if task.status is not TaskStatus.RUNNING:
                raise TaskError(f"задача {task.key} не в работе ({task.status})")
            task.status = TaskStatus.VERIFYING
            task.summary = summary
            task.decisions = list(decisions)

    async def back_to_running(self, task_id: int) -> None:
        """Шлюз вернул задачу агенту (тесты, конфликт)."""
        async with self._session_factory() as session, session.begin():
            task = await _task(session, task_id)
            task.status = TaskStatus.RUNNING

    async def mark_done(self, task_id: int, *, merge_sha: str) -> None:
        async with self._session_factory() as session, session.begin():
            task = await _task(session, task_id)
            task.status = TaskStatus.DONE
            task.pause_reason = None
            task.merge_sha = merge_sha
            task.completed_at = utcnow()

    async def pause(self, task_id: int, reason: PauseReason) -> None:
        async with self._session_factory() as session, session.begin():
            task = await _task(session, task_id)
            task.status = TaskStatus.PAUSED
            task.pause_reason = reason

    async def fail(self, task_id: int, *, reason: str) -> None:
        async with self._session_factory() as session, session.begin():
            task = await _task(session, task_id)
            task.status = TaskStatus.FAILED
            task.summary = reason

    async def set_status(self, task_id: int, status: TaskStatus) -> Task:
        """Решение пользователя по невыполнимой задаче: вернуть в работу или отменить."""
        if status not in (TaskStatus.PENDING, TaskStatus.CANCELLED):
            raise TaskError("вручную задачу можно только вернуть в работу или отменить")
        async with self._session_factory() as session, session.begin():
            task = await _task(session, task_id)
            if task.status is not TaskStatus.FAILED:
                raise TaskError(f"задача {task.key} не в статусе failed")
            task.status = status
            return task

    async def close_finished_days(self, project_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            days = await session.scalars(
                select(DayExecution).where(
                    DayExecution.project_id == project_id,
                    DayExecution.status == DayStatus.IN_PROGRESS,
                )
            )
            for day in days:
                unfinished = await session.scalar(
                    select(
                        exists().where(
                            Task.project_id == project_id,
                            Task.day_number == day.day_number,
                            Task.status.not_in(FINISHED),
                        )
                    )
                )
                if not unfinished:
                    day.status = DayStatus.DONE

    async def is_project_finished(self, project_id: int) -> bool:
        async with self._session_factory() as session:
            project = await _project(session, project_id)
            open_tasks = await session.scalar(
                select(func.count()).where(
                    Task.project_id == project_id, Task.status.not_in(FINISHED)
                )
            )
        return project.current_day >= project.planned_days and open_tasks == 0

    async def depends_unfinished(self, task: Task) -> list[str]:
        if not task.depends_on:
            return []
        async with self._session_factory() as session:
            done = set(
                await session.scalars(
                    select(Task.key).where(
                        Task.project_id == task.project_id,
                        Task.key.in_(task.depends_on),
                        Task.status == TaskStatus.DONE,
                    )
                )
            )
        return [key for key in task.depends_on if key not in done]


async def _task(session: AsyncSession, task_id: int) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise TaskError(f"задачи {task_id} нет")
    return task


async def _project(session: AsyncSession, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise TaskError(f"проекта {project_id} нет")
    return project
