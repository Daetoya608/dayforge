"""Вехи, релизы и учёт коммитов в БД (docs/git-workflow.md#релизы)."""

import re
from collections.abc import Iterable

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import CommitKind, MilestoneStatus, TaskStatus
from dayforge.db.models import Commit, Milestone, Release, Task
from dayforge.vcs.git import CommitInfo

_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class ReleaseService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def completed_milestones(self, project_id: int) -> list[Milestone]:
        """Невыпущенные вехи, все задачи которых выполнены, — в порядке плана."""
        async with self._session_factory() as session:
            milestones = list(
                await session.scalars(
                    select(Milestone)
                    .where(
                        Milestone.project_id == project_id,
                        Milestone.status == MilestoneStatus.PENDING,
                    )
                    .order_by(Milestone.id)
                )
            )
            result = []
            for milestone in milestones:
                has_tasks = await session.scalar(
                    select(
                        exists().where(
                            Task.project_id == project_id,
                            Task.milestone_key == milestone.key,
                            Task.status != TaskStatus.CANCELLED,
                        )
                    )
                )
                unfinished = await session.scalar(
                    select(
                        exists().where(
                            Task.project_id == project_id,
                            Task.milestone_key == milestone.key,
                            Task.status.not_in((TaskStatus.DONE, TaskStatus.CANCELLED)),
                        )
                    )
                )
                if has_tasks and not unfinished:
                    result.append(milestone)
            return result

    async def pending_milestones(self, project_id: int) -> list[Milestone]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(Milestone)
                    .where(
                        Milestone.project_id == project_id,
                        Milestone.status == MilestoneStatus.PENDING,
                    )
                    .order_by(Milestone.id)
                )
            )

    async def milestone_tasks(self, project_id: int, milestone_key: str) -> list[Task]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(Task)
                    .where(
                        Task.project_id == project_id,
                        Task.milestone_key == milestone_key,
                        Task.status == TaskStatus.DONE,
                    )
                    .order_by(Task.day_number, Task.position)
                )
            )

    async def next_manual_version(self, project_id: int) -> str:
        """Ручной релиз без вехи — следующая минорная версия после последнего релиза."""
        async with self._session_factory() as session:
            tags = list(
                await session.scalars(select(Release.tag).where(Release.project_id == project_id))
            )
        versions = [tuple(map(int, m.groups())) for t in tags if (m := _VERSION_RE.match(t))]
        if not versions:
            return "v0.1.0"
        major, minor, _ = max(versions)
        return f"v{major}.{minor + 1}.0"

    async def tag_exists(self, project_id: int, tag: str) -> bool:
        async with self._session_factory() as session:
            return bool(
                await session.scalar(
                    select(exists().where(Release.project_id == project_id, Release.tag == tag))
                )
            )

    async def record_release(
        self, project_id: int, *, tag: str, sha: str, milestone_id: int | None
    ) -> None:
        async with self._session_factory() as session, session.begin():
            session.add(Release(project_id=project_id, milestone_id=milestone_id, tag=tag, sha=sha))
            if milestone_id is not None:
                milestone = await session.get(Milestone, milestone_id)
                if milestone is not None:
                    milestone.status = MilestoneStatus.RELEASED

    async def record_commits(
        self,
        project_id: int,
        commits: Iterable[CommitInfo],
        *,
        branch: str,
        kind: CommitKind,
        task_id: int | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            for commit in commits:
                if await session.get(Commit, commit.sha) is not None:
                    continue
                session.add(
                    Commit(
                        sha=commit.sha,
                        project_id=project_id,
                        task_id=task_id,
                        branch=branch,
                        message=commit.message,
                        kind=kind,
                        committed_at=commit.committed_at,
                    )
                )
