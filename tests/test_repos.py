from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.repos import RepoSync
from dayforge.db.models import Project
from dayforge.vcs.base import RepoInfo
from dayforge.vcs.git import Git
from tests.test_models import make_project


class LocalProvider:
    """Провайдер, у которого «удалённые» репозитории — bare-репозитории на диске."""

    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.created: list[str] = []

    async def create_repo(self, name: str, *, private: bool, description: str) -> RepoInfo:
        self.created.append(name)
        await Git(self.root).run("init", "-q", "--bare", str(self.root / f"{name}.git"))
        return RepoInfo(owner="me", name=name)

    def remote_url(self, repo: RepoInfo) -> str:
        return str(self.root / f"{repo.name}.git")

    def git_env(self) -> dict[str, str]:
        return {}


@pytest.fixture
async def project(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> Project:
    git = Git(tmp_path / "todo-bot")
    git.cwd.mkdir()
    await git.init()
    (git.cwd / "README.md").write_text("x\n")
    await git.commit_all("chore: init")
    await git.create_branch("dev")
    async with session_factory() as session, session.begin():
        project = make_project(repo_path=str(git.cwd))
        session.add(project)
    return project


async def test_publish_creates_remote_once_and_pushes(
    session_factory: async_sessionmaker[AsyncSession], project: Project, tmp_path: Path
) -> None:
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    provider = LocalProvider(remotes)
    sync = RepoSync(session_factory, provider)  # type: ignore[arg-type]

    url = await sync.publish(project.id)
    await sync.publish(project.id)

    assert provider.created == ["todo-bot"]
    assert url == str(remotes / "todo-bot.git")
    async with session_factory() as session:
        stored = await session.get(Project, project.id)
        assert stored is not None and stored.remote_url == url
    bare = Git(remotes / "todo-bot.git")
    assert await bare.rev_parse("dev") == await Git(Path(project.repo_path)).rev_parse("dev")


async def test_without_provider_everything_stays_local(
    session_factory: async_sessionmaker[AsyncSession], project: Project
) -> None:
    sync = RepoSync(session_factory, None)

    assert await sync.publish(project.id) is None
    assert not await Git(Path(project.repo_path)).has_remote()


async def test_status_compares_local_and_remote_refs(
    session_factory: async_sessionmaker[AsyncSession], project: Project, tmp_path: Path
) -> None:
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    sync = RepoSync(session_factory, LocalProvider(remotes))  # type: ignore[arg-type]
    await sync.publish(project.id)

    status = await sync.status(project.id)
    assert status["published"] and status["all_in_sync"]
    assert {r["ref"] for r in status["refs"]} == {"refs/heads/main", "refs/heads/dev"}

    git = Git(Path(project.repo_path))
    await git.checkout("dev")
    (git.cwd / "new.txt").write_text("x\n")
    await git.commit_all("feat: new")
    await git.tag("v0.1.0", "release")

    status = await sync.status(project.id)
    states = {r["ref"]: r["state"] for r in status["refs"]}
    assert states == {
        "refs/heads/dev": "расходится",
        "refs/heads/main": "в синхроне",
        "refs/tags/v0.1.0": "не запушено",
    }
    assert not status["all_in_sync"]


async def test_status_without_provider(
    session_factory: async_sessionmaker[AsyncSession], project: Project
) -> None:
    status = await RepoSync(session_factory, None).status(project.id)

    assert status["published"] is False
    assert [r["ref"] for r in status["refs"]] == ["refs/heads/dev", "refs/heads/main"]
