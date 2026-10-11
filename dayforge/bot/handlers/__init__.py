"""Обработчики бота. Порядок важен: диалоги с состоянием — раньше, системный агент — последним."""

from aiogram import Router

from dayforge.bot.handlers import (
    actions,
    agent,
    calendar,
    common,
    new_project,
    planning,
    projects,
    questions,
    runs,
    settings,
)

ROUTERS: list[Router] = [
    common.router,
    new_project.router,
    settings.router,
    planning.router,
    questions.router,
    projects.router,
    runs.router,
    calendar.router,
    actions.router,
    agent.router,
]
