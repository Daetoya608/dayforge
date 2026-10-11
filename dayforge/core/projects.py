"""Создание проектов, список, приоритеты (docs/architecture.md#сервисы-ядра)."""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge import prompts
from dayforge.core.enums import ProjectStatus
from dayforge.core.settings import SettingsService
from dayforge.db.base import utcnow
from dayforge.db.models import Project
from dayforge.vcs.git import Git

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
AGENTS = ("claude_code", "codex")
BADGES = ["🟦", "🟩", "🟧", "🟪", "🟥", "🟨", "🟫", "⬛", "⬜"]

_TRANSLIT = str.maketrans(
    dict(zip("абвгдезийклмнопрстуфхыэ", "abvgdeziyklmnoprstufhye", strict=True))
    | {"ё": "e", "ж": "zh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch"}
    | {"ъ": "", "ь": "", "ю": "yu", "я": "ya"}
)


class ProjectError(ValueError):
    pass


@dataclass(frozen=True, kw_only=True)
class NewProject:
    name: str
    slug: str
    idea: str
    planned_days: int
    stack_hint: str | None = None
    # Место в очереди дня, начиная с 1; None — последним.
    priority_position: int | None = None
    planner_agent: dict[str, Any] | None = None
    executor_agent: dict[str, Any] | None = None


def suggest_slug(name: str) -> str:
    """Slug из названия: транслитерация, латиница, цифры и дефисы."""
    text = name.lower().translate(_TRANSLIT)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:64].strip("-") or "project"


class ProjectService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: SettingsService,
        projects_dir: Path,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._projects_dir = projects_dir

    async def create(self, new: NewProject) -> Project:
        if not SLUG_RE.fullmatch(new.slug):
            raise ProjectError(
                f"slug {new.slug!r}: только латиница в нижнем регистре, цифры и дефисы, "
                "2–64 символа"
            )
        if new.planned_days < 1:
            raise ProjectError("число дней должно быть не меньше 1")

        repo_path = self._projects_dir / new.slug
        planner_agent = new.planner_agent or await self._settings.get("default_planner_agent")
        executor_agent = new.executor_agent or await self._settings.get("default_executor_agent")

        async with self._session_factory() as session, session.begin():
            if await session.scalar(select(Project.id).where(Project.slug == new.slug)):
                raise ProjectError(f"проект {new.slug} уже существует")
            if repo_path.exists():
                raise ProjectError(f"директория {repo_path} уже существует")

            active = await self._active_projects(session)
            position = _clamp_position(new.priority_position, len(active))
            for project in active[position - 1 :]:
                project.priority += 1

            project = Project(
                slug=new.slug,
                name=new.name.strip(),
                badge=_free_badge({p.badge for p in active}),
                idea=new.idea.strip(),
                stack_hint=new.stack_hint.strip() if new.stack_hint else None,
                priority=position,
                planned_days=new.planned_days,
                planner_agent=planner_agent,
                executor_agent=executor_agent,
                repo_path=str(repo_path),
            )
            session.add(project)
            await session.flush()
            try:
                await _init_workspace(project, repo_path)
            except BaseException:
                shutil.rmtree(repo_path, ignore_errors=True)
                raise
        return project

    async def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        query = select(Project).order_by(Project.priority, Project.id)
        if not include_archived:
            query = query.where(Project.status != ProjectStatus.ARCHIVED)
        async with self._session_factory() as session:
            return list(await session.scalars(query))

    async def get(self, slug: str) -> Project:
        async with self._session_factory() as session:
            project = await session.scalar(select(Project).where(Project.slug == slug))
        if project is None:
            raise ProjectError(f"проекта {slug} нет")
        return project

    async def get_by_id(self, project_id: int) -> Project:
        async with self._session_factory() as session:
            project = await session.get(Project, project_id)
        if project is None:
            raise ProjectError(f"проекта {project_id} нет")
        return project

    async def set_status(self, project_id: int, status: ProjectStatus) -> Project:
        async with self._session_factory() as session, session.begin():
            project = await session.get(Project, project_id)
            if project is None:
                raise ProjectError(f"проекта {project_id} нет")
            project.status = status
            if status is ProjectStatus.COMPLETED:
                project.completed_at = utcnow()
            return project

    async def pause(self, project_id: int) -> Project:
        project = await self.get_by_id(project_id)
        if project.status not in (ProjectStatus.ACTIVE, ProjectStatus.BLOCKED):
            raise ProjectError(
                f"проект {project.slug} в статусе {project.status}, пауза невозможна"
            )
        return await self.set_status(project_id, ProjectStatus.PAUSED)

    async def resume(self, project_id: int, *, has_open_questions: bool) -> Project:
        project = await self.get_by_id(project_id)
        if project.status is not ProjectStatus.PAUSED:
            raise ProjectError(f"проект {project.slug} не на паузе")
        status = ProjectStatus.BLOCKED if has_open_questions else ProjectStatus.ACTIVE
        return await self.set_status(project_id, status)

    async def update_settings(
        self,
        project_id: int,
        *,
        name: str | None = None,
        badge: str | None = None,
        planner_agent: dict[str, Any] | None = None,
        executor_agent: dict[str, Any] | None = None,
    ) -> Project:
        async with self._session_factory() as session, session.begin():
            project = await session.get(Project, project_id)
            if project is None:
                raise ProjectError(f"проекта {project_id} нет")
            if name is not None and name.strip():
                project.name = name.strip()
            if badge is not None and badge.strip():
                project.badge = badge.strip()[:8]
            if planner_agent is not None:
                project.planner_agent = _agent_config(planner_agent)
            if executor_agent is not None:
                project.executor_agent = _agent_config(executor_agent)
            return project

    async def set_priority(self, order: list[str]) -> None:
        """Порядок проектов в очереди дня: полный список slug неархивных проектов."""
        async with self._session_factory() as session, session.begin():
            active = await self._active_projects(session)
            if sorted(order) != sorted(p.slug for p in active):
                raise ProjectError("в порядке должны быть все неархивные проекты, по одному разу")
            by_slug = {p.slug: p for p in active}
            for priority, slug in enumerate(order, start=1):
                by_slug[slug].priority = priority

    @staticmethod
    async def _active_projects(session: AsyncSession) -> list[Project]:
        query = (
            select(Project)
            .where(Project.status != ProjectStatus.ARCHIVED)
            .order_by(Project.priority, Project.id)
        )
        return list(await session.scalars(query))


def _agent_config(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("agent") not in AGENTS or not raw.get("model"):
        raise ProjectError(f"агент — один из {', '.join(AGENTS)}, модель обязательна")
    return {"agent": raw["agent"], "model": str(raw["model"]), "effort": raw.get("effort")}


def _clamp_position(position: int | None, count: int) -> int:
    if position is None:
        return count + 1
    return min(max(position, 1), count + 1)


def _free_badge(used: set[str]) -> str:
    for badge in BADGES:
        if badge not in used:
            return badge
    # Цветные метки кончились — дальше буквы.
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if letter not in used:
            return letter
    return "•"


async def _init_workspace(project: Project, path: Path) -> None:
    """Локальный репозиторий проекта. Коммитов нет до утверждения плана (docs/git-workflow.md)."""
    (path / "docs").mkdir(parents=True)
    (path / "docs" / "IDEA.md").write_text(
        prompts.render(
            "project_idea.md", name=project.name, idea=project.idea, stack_hint=project.stack_hint
        )
    )
    (path / "AGENTS.md").write_text(prompts.render("project_agents.md", name=project.name))
    (path / "CLAUDE.md").write_text("@AGENTS.md\n")
    (path / ".gitignore").write_text(".env\n")
    await Git(path).init()
