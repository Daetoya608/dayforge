"""Действия, которые выполняются только после подтверждения пользователем (pending actions)."""

from datetime import date
from typing import Any

from dayforge.core.enums import CalendarDateKind
from dayforge.services import Services

# Больше стольких дат за раз или смена недельного шаблона — нужно подтверждение.
BULK_CALENDAR_DATES = 5


class CalendarChangeError(ValueError):
    pass


def calendar_needs_confirmation(changes: list[dict[str, Any]]) -> bool:
    dates = sum(len(change.get("dates") or []) for change in changes)
    return dates > BULK_CALENDAR_DATES or any(c["op"] == "set_weekdays" for c in changes)


async def apply_calendar_changes(services: Services, changes: list[dict[str, Any]]) -> str:
    """Применяет операции update_calendar (docs/mcp-tools.md#изменения)."""
    applied = []
    for change in changes:
        op = change["op"]
        project_id = None
        if change.get("project"):
            project_id = (await services.projects.get(change["project"])).id
        dates = [date.fromisoformat(d) for d in change.get("dates") or []]
        match op:
            case "set_weekdays":
                if project_id is None:
                    raise CalendarChangeError("set_weekdays: нужен project")
                await services.calendar.set_weekdays(project_id, change.get("weekdays") or [])
            case "include" | "exclude":
                if project_id is None:
                    raise CalendarChangeError(f"{op}: нужен project")
                kind = CalendarDateKind.INCLUDE if op == "include" else CalendarDateKind.EXCLUDE
                await services.calendar.set_dates(project_id, dates, kind)
            case "day_off":
                await services.calendar.set_dates(None, dates, CalendarDateKind.EXCLUDE)
            case "clear":
                await services.calendar.clear_dates(project_id, dates)
            case _:
                raise CalendarChangeError(f"неизвестная операция {op}")
        applied.append(op)
    return f"Календарь обновлён: {', '.join(applied)}"


def register_action_handlers(services: Services) -> None:
    async def bulk_calendar(payload: dict[str, Any]) -> str:
        return await apply_calendar_changes(services, payload["changes"])

    async def release(payload: dict[str, Any]) -> str:
        return await services.require_orchestrator().release(payload["project_id"])

    async def archive_project(payload: dict[str, Any]) -> str:
        return await services.require_orchestrator().archive_project(payload["project_id"])

    async def delete_project(payload: dict[str, Any]) -> str:
        return await services.require_orchestrator().delete_project(
            payload["project_id"], delete_remote=payload.get("delete_remote", False)
        )

    services.actions.register("bulk_calendar", bulk_calendar)
    services.actions.register("release", release)
    services.actions.register("archive_project", archive_project)
    services.actions.register("delete_project", delete_project)
