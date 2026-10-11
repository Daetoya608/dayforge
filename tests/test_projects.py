from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import ProjectStatus
from dayforge.core.projects import NewProject, ProjectError, ProjectService, suggest_slug
from dayforge.core.settings import SettingsService
from dayforge.vcs.git import AGENT_EMAIL, Git


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    path = tmp_path / "projects"
    path.mkdir()
    return path


@pytest.fixture
def service(
    session_factory: async_sessionmaker[AsyncSession], projects_dir: Path
) -> ProjectService:
    return ProjectService(session_factory, SettingsService(session_factory), projects_dir)


def new(slug: str, **overrides: object) -> NewProject:
    fields: dict[str, object] = {
        "name": f"Проект {slug}",
        "slug": slug,
        "idea": "Бот учёта расходов",
        "planned_days": 10,
    }
    return NewProject(**(fields | overrides))  # type: ignore[arg-type]


async def test_create_initializes_project_and_workspace(
    service: ProjectService, projects_dir: Path
) -> None:
    project = await service.create(new("todo-bot", stack_hint="FastAPI"))

    assert project.status is ProjectStatus.DRAFT
    assert project.priority == 1
    assert project.badge == "🟦"
    assert project.planner_agent == {"agent": "claude_code", "model": "opus", "effort": "high"}

    workspace = projects_dir / "todo-bot"
    assert project.repo_path == str(workspace)
    idea = (workspace / "docs" / "IDEA.md").read_text()
    assert "Бот учёта расходов" in idea and "FastAPI" in idea
    assert "# Правила для агентов — Проект todo-bot" in (workspace / "AGENTS.md").read_text()
    assert (workspace / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert (workspace / ".gitignore").read_text() == ".env\n"

    git = Git(workspace)
    assert await git.current_branch() == "main"
    assert await git.run("config", "user.email") == AGENT_EMAIL
    # До утверждения плана коммитов нет.
    assert await git.run("rev-list", "--all", "--count") == "0"


async def test_priority_insertion_shifts_others(service: ProjectService) -> None:
    await service.create(new("aa"))
    await service.create(new("bb"))
    await service.create(new("cc", priority_position=1))
    await service.create(new("dd", priority_position=99))

    projects = await service.list_projects()

    assert [(p.slug, p.priority) for p in projects] == [
        ("cc", 1),
        ("aa", 2),
        ("bb", 3),
        ("dd", 4),
    ]
    assert len({p.badge for p in projects}) == 4


async def test_set_priority_requires_full_order(service: ProjectService) -> None:
    for slug in ("aa", "bb", "cc"):
        await service.create(new(slug))

    await service.set_priority(["cc", "aa", "bb"])
    assert [p.slug for p in await service.list_projects()] == ["cc", "aa", "bb"]

    with pytest.raises(ProjectError):
        await service.set_priority(["cc", "aa"])


@pytest.mark.parametrize("slug", ["A-bot", "-bot", "bot-", "b", "бот", "my_bot"])
async def test_invalid_slug_is_rejected(service: ProjectService, slug: str) -> None:
    with pytest.raises(ProjectError, match="slug"):
        await service.create(new(slug))


async def test_duplicate_slug_is_rejected_without_touching_workspace(
    service: ProjectService, projects_dir: Path
) -> None:
    await service.create(new("todo-bot"))
    marker = projects_dir / "todo-bot" / "marker"
    marker.write_text("keep")

    with pytest.raises(ProjectError, match="уже существует"):
        await service.create(new("todo-bot"))
    assert marker.exists()


async def test_existing_directory_is_not_overwritten(
    service: ProjectService, projects_dir: Path
) -> None:
    (projects_dir / "todo-bot").mkdir()

    with pytest.raises(ProjectError, match="директория"):
        await service.create(new("todo-bot"))
    assert await service.list_projects() == []


async def test_failed_workspace_init_rolls_back(
    service: ProjectService, projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_init(self: Git, *, branch: str = "main") -> None:
        raise RuntimeError("git is broken")

    monkeypatch.setattr(Git, "init", broken_init)

    with pytest.raises(RuntimeError):
        await service.create(new("todo-bot"))
    assert not (projects_dir / "todo-bot").exists()
    assert await service.list_projects() == []


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Бот учёта расходов", "bot-ucheta-rashodov"),
        ("Объявления", "obyavleniya"),
        ("Todo App 2.0", "todo-app-2-0"),
        ("!!!", "project"),
    ],
)
def test_suggest_slug(name: str, slug: str) -> None:
    assert suggest_slug(name) == slug
