"""Зависимости обработчиков бота: aiogram передаёт их по имени аргумента `app`."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dayforge.bot.callbacks import ProjectCb
from dayforge.bot.storage import BotKV
from dayforge.core.enums import ProjectStatus
from dayforge.core.projects import ProjectError
from dayforge.db.models import Project
from dayforge.services import Services

if TYPE_CHECKING:
    from dayforge.chat import ChatAgent
    from dayforge.orchestrator import Orchestrator
    from dayforge.runner.runner import Runner
    from dayforge.scheduler import DayforgeScheduler


@dataclass
class AppContext:
    services: Services
    orchestrator: "Orchestrator"
    runner: "Runner"
    chat: "ChatAgent"
    kv: BotKV
    scheduler: "DayforgeScheduler | None" = None

    async def project(self, slug: str) -> Project | None:
        try:
            return await self.services.projects.get(slug.strip())
        except ProjectError:
            return None

    async def project_picker(
        self, action: str, *, statuses: tuple[ProjectStatus, ...] | None = None
    ) -> InlineKeyboardMarkup | None:
        """Кнопки выбора проекта, когда в команде он не указан."""
        projects = await self.services.projects.list_projects()
        if statuses is not None:
            projects = [p for p in projects if p.status in statuses]
        if not projects:
            return None
        builder = InlineKeyboardBuilder()
        for project in projects:
            builder.button(
                text=f"{project.badge} {project.name}",
                callback_data=ProjectCb(action=action, project_id=project.id),
            )
        builder.adjust(1)
        return builder.as_markup()
