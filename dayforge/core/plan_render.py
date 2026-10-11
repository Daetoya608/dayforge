"""Генерация docs/PLAN.md из состояния в БД (docs/plan-format.md#planmd)."""

from collections import defaultdict
from dataclasses import dataclass

from dayforge.core.calendar import Forecast
from dayforge.core.enums import PauseReason, TaskStatus


@dataclass(frozen=True, kw_only=True)
class TaskView:
    key: str
    title: str
    description: str
    acceptance_criteria: list[str]
    day_number: int | None  # None — разовая задача вне плана
    position: int
    status: TaskStatus
    pause_reason: PauseReason | None = None


@dataclass(frozen=True, kw_only=True)
class MilestoneView:
    key: str
    title: str
    version: str
    released: bool


@dataclass(frozen=True, kw_only=True)
class PlanView:
    name: str
    version: int
    summary: str
    test_command: str
    current_day: int
    planned_days: int
    forecast: Forecast
    day_goals: dict[int, str]
    milestones: list[MilestoneView]
    tasks: list[TaskView]


def render_plan(view: PlanView) -> str:
    tasks = [t for t in view.tasks if t.status is not TaskStatus.CANCELLED]
    by_day: dict[int, list[TaskView]] = defaultdict(list)
    adhoc = []
    for task in sorted(tasks, key=lambda t: (t.day_number or 0, t.position, t.key)):
        if task.day_number is None:
            adhoc.append(task)
        else:
            by_day[task.day_number].append(task)

    lines = [
        f"# План: {view.name}",
        "",
        "> Сгенерировано Dayforge. Не редактировать вручную.",
        f"> Версия плана: {view.version} · {_forecast_text(view.forecast)} · "
        f"День {view.current_day} из {view.planned_days}",
        "",
        f"**{view.summary}**",
        "",
        f"Тесты: `{view.test_command}`",
        "",
        "## Вехи",
        "",
    ]
    for milestone in view.milestones:
        mark = "x" if milestone.released else " "
        lines.append(f"- [{mark}] {milestone.key} — {milestone.title} ({milestone.version})")

    for day in sorted(by_day.keys() | {d for d in view.day_goals if d <= view.planned_days}):
        day_tasks = by_day.get(day, [])
        goal = view.day_goals.get(day, "")
        header = f"## День {day}" + (f" — {goal}" if goal else "")
        icon = _day_icon(day, day_tasks, view.current_day)
        lines += ["", f"{header} {icon}".rstrip(), ""]
        lines += [line for task in day_tasks for line in _task_lines(task)]

    if adhoc:
        lines += ["", "## Вне плана", ""]
        lines += [line for task in adhoc for line in _task_lines(task)]
    return _squeeze_blank_lines(lines)


def _squeeze_blank_lines(lines: list[str]) -> str:
    result: list[str] = []
    for line in lines:
        if line == "" and (not result or result[-1] == ""):
            continue
        result.append(line)
    while result and result[-1] == "":
        result.pop()
    return "\n".join(result) + "\n"


def _forecast_text(forecast: Forecast) -> str:
    if forecast.end_date is not None:
        return f"Прогноз окончания: {forecast.end_date:%d.%m.%Y}"
    if forecast.missing_days:
        return f"В календаре не хватает дней: {forecast.missing_days}"
    return "Проект завершён"


def _day_icon(day: int, tasks: list[TaskView], current_day: int) -> str:
    if tasks and all(t.status is TaskStatus.DONE for t in tasks):
        return "✅"
    if day <= current_day:
        return "⏳"
    return ""


def _task_lines(task: TaskView) -> list[str]:
    done = task.status is TaskStatus.DONE
    line = f"- [{'x' if done else ' '}] **{task.key}** {task.title}"
    marker = _status_marker(task)
    lines = [f"{line} {marker}" if marker else line]
    if not done:
        lines += ["", f"  {task.description}", "", "  Критерии:"]
        lines += [f"  - {criterion}" for criterion in task.acceptance_criteria]
        lines.append("")
    return lines


def _status_marker(task: TaskView) -> str:
    match task.status:
        case TaskStatus.RUNNING | TaskStatus.VERIFYING:
            return "🔨 в работе"
        case TaskStatus.FAILED:
            return "✖ не выполнена"
        case TaskStatus.PAUSED if task.pause_reason in (PauseReason.QUESTION, PauseReason.SECRET):
            return "⏸ ждёт ответа"
        case TaskStatus.PAUSED:
            return "⏸ приостановлена"
        case _:
            return ""
