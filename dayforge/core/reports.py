"""Отчёт о запуске проекта (docs/telegram-bot.md#ежедневный-отчёт).

Текст задач (summary, decisions) пишет агент, остальное — факты системы. Формат — HTML-подмножество
Telegram (<b>, <i>, <a>, <code>): отчёт показывается только в боте.
"""

from dataclasses import asdict, dataclass, field
from datetime import date
from html import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar import Forecast
from dayforge.core.enums import RunStatus
from dayforge.core.events import ProjectRef
from dayforge.db.models import Report

# Длина sha в тексте ссылок на коммиты.
SHORT_SHA = 7


@dataclass(frozen=True, kw_only=True)
class DoneTask:
    key: str
    title: str
    summary: str
    decisions: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class OpenTask:
    key: str
    title: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class Link:
    text: str
    url: str | None = None


@dataclass(frozen=True, kw_only=True)
class RunSummary:
    run_id: int
    project: ProjectRef
    date: date
    status: RunStatus
    day_number: int | None
    planned_days: int
    done: list[DoneTask] = field(default_factory=list)
    open: list[OpenTask] = field(default_factory=list)
    commits: list[Link] = field(default_factory=list)
    releases: list[Link] = field(default_factory=list)
    # None — тесты в этом запуске не запускались.
    tests_green: bool | None = None
    forecast: Forecast = field(default_factory=lambda: Forecast(None))
    previous_forecast: Forecast | None = None
    note: str | None = None


def format_report(summary: RunSummary) -> str:
    project = summary.project
    day = (
        f"день {summary.day_number} из {summary.planned_days}"
        if summary.day_number
        else "доделывание"
    )
    lines = [f"📋 <b>{escape(project.name)}</b> · {day} · {summary.date:%d.%m}"]
    if summary.note:
        lines += ["", escape(summary.note)]

    if summary.done or summary.open:
        lines.append("")
    for task in summary.done:
        lines.append(f"✅ <b>{task.key}</b> {escape(task.title)}")
        if task.summary:
            lines.append(f"   {escape(task.summary)}")
        lines += [f"   💡 {escape(decision)}" for decision in task.decisions]
    for task in summary.open:
        lines.append(f"⏸ <b>{task.key}</b> {escape(task.title)} — {escape(task.reason)}")

    facts = []
    if summary.tests_green is not None:
        facts.append("🧪 Тесты dev: " + ("зелёные" if summary.tests_green else "красные"))
    if summary.commits:
        facts.append(f"коммитов: {len(summary.commits)} ({_commit_range(summary.commits)})")
    if facts:
        lines += ["", " · ".join(facts)]
    if summary.releases:
        lines.append("🏷 Выпущено: " + ", ".join(_link(r) for r in summary.releases))
    lines.append("📅 " + _forecast_line(summary.forecast, summary.previous_forecast))
    return "\n".join(lines)


def format_batch_header(day: date, summaries: list[RunSummary]) -> str:
    done = sum(1 for s in summaries if s.status is RunStatus.FINISHED and not s.open)
    paused = len(summaries) - done
    return f"Запуск {day:%d.%m}: проектов {len(summaries)}, ✅ {done}, ⏸ {paused}"


def _commit_range(commits: list[Link]) -> str:
    first, last = commits[0], commits[-1]
    if first is last:
        return _link(first)
    return f"{_link(first)}…{_link(last)}"


def _link(link: Link) -> str:
    text = escape(link.text)
    return f'<a href="{escape(link.url)}">{text}</a>' if link.url else f"<code>{text}</code>"


def _forecast_line(current: Forecast, previous: Forecast | None) -> str:
    if current.end_date is None:
        if current.missing_days:
            return f"В календаре не хватает дней: {current.missing_days}"
        return "Проект завершён"
    line = f"Прогноз окончания: {current.end_date:%d.%m}"
    if previous is not None and previous.end_date is not None:
        if previous.end_date == current.end_date:
            line += " (без изменений)"
        else:
            line += f" (было {previous.end_date:%d.%m})"
    return line


class ReportService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def store(self, summary: RunSummary) -> Report:
        text = format_report(summary)
        data = asdict(summary)
        data["project"] = asdict(summary.project)
        async with self._session_factory() as session, session.begin():
            report = Report(
                run_id=summary.run_id,
                project_id=summary.project.id,
                text=text,
                data=_jsonable(data),
            )
            session.add(report)
        return report

    async def latest(self, project_id: int) -> Report | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(Report)
                .where(Report.project_id == project_id)
                .order_by(Report.id.desc())
                .limit(1)
            )


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, RunStatus):
        return value.value
    return value
