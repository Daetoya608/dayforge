"""Публикация репозиториев проектов на хостинге (docs/git-workflow.md#удалённые-ветки).

Без провайдера (GitHub не настроен) всё работает локально, публикация пропускается.
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.db.models import Project
from dayforge.vcs.base import VcsProvider
from dayforge.vcs.git import Git

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 300


class RepoSync:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], provider: VcsProvider | None
    ) -> None:
        self._session_factory = session_factory
        self.provider = provider

    async def publish(self, project_id: int) -> str | None:
        """Создаёт удалённый репозиторий, если его ещё нет, и пушит main, dev и теги.

        Повторный вызов безопасен: так же досылаются push, упавшие из-за сети.
        """
        if self.provider is None:
            return None
        async with self._session_factory() as session, session.begin():
            project = await session.get(Project, project_id)
            if project is None:
                raise ValueError(f"проекта {project_id} нет")
            if project.remote_url is None:
                repo = await self.provider.create_repo(
                    project.slug, private=True, description=_description(project)
                )
                project.remote_url = self.provider.remote_url(repo)
            remote_url = project.remote_url
            repo_path = Path(project.repo_path)

        git = Git(repo_path)
        if not await git.has_remote():
            await git.add_remote(remote_url)
        await self.push(repo_path, "main", "dev", tags=True)
        return remote_url

    async def status(self, project_id: int) -> dict[str, Any]:
        """Ветки и теги рабочей копии против удалённого репозитория — для системного агента."""
        async with self._session_factory() as session:
            project = await session.get(Project, project_id)
        if project is None:
            raise ValueError(f"проекта {project_id} нет")
        git = Git(Path(project.repo_path))
        local = await git.show_refs()
        result: dict[str, Any] = {
            "branch": await git.current_branch(),
            "clean": await git.is_clean(),
            "remote_url": project.remote_url,
        }
        if self.provider is None or project.remote_url is None:
            result["published"] = False
            result["refs"] = [{"ref": ref, "local": sha[:7]} for ref, sha in sorted(local.items())]
            return result

        remote = await git.ls_remote(project.remote_url, env=self.provider.git_env())
        refs = []
        for ref in sorted(local.keys() | remote.keys()):
            here, there = local.get(ref), remote.get(ref)
            if here == there:
                state = "в синхроне"
            elif there is None:
                state = "не запушено"
            elif here is None:
                state = "только на удалённом"
            else:
                state = "расходится"
            refs.append(
                {
                    "ref": ref,
                    "local": here[:7] if here else None,
                    "remote": there[:7] if there else None,
                    "state": state,
                }
            )
        result["published"] = True
        result["refs"] = refs
        result["all_in_sync"] = all(r["state"] == "в синхроне" for r in refs)
        return result

    async def push(self, repo_path: Path, *refspecs: str, tags: bool = False) -> None:
        if self.provider is None:
            return
        await Git(repo_path).push(*refspecs, env=self.provider.git_env(), tags=tags)

    async def delete_remote_branch(self, repo_path: Path, branch: str) -> None:
        if self.provider is None:
            return
        await Git(repo_path).delete_remote_branch(branch, env=self.provider.git_env())


def _description(project: Project) -> str:
    first_line = project.idea.strip().splitlines()[0] if project.idea.strip() else project.name
    return first_line[:DESCRIPTION_LIMIT]
