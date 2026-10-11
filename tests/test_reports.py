from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.calendar import Forecast
from dayforge.core.enums import RunStatus
from dayforge.core.events import ProjectRef
from dayforge.core.reports import (
    DoneTask,
    Link,
    OpenTask,
    ReportService,
    RunSummary,
    format_batch_header,
    format_report,
)
from dayforge.db.models import Project

PROJECT = ProjectRef(id=1, slug="todo-bot", name="Todo <bot>", badge="🟦")


def summary(**overrides: object) -> RunSummary:
    fields: dict[str, object] = {
        "run_id": 1,
        "project": PROJECT,
        "date": date(2026, 10, 14),
        "status": RunStatus.FINISHED,
        "day_number": 4,
        "planned_days": 10,
        "done": [
            DoneTask(key="T9", title="Модель категорий", summary="Модель, 6 тестов"),
            DoneTask(
                key="T10",
                title="Команды /category",
                summary="add/list/remove",
                decisions=["Категории на пользователя"],
            ),
        ],
        "open": [OpenTask(key="T11", title="Импорт CSV", reason="ждёт ответа на вопрос")],
        "commits": [
            Link(text="abc1234", url="https://github.com/x/commit/abc1234"),
            Link(text="f00ba12", url="https://github.com/x/commit/f00ba12"),
        ],
        "releases": [Link(text="v0.1.0", url="https://github.com/x/releases/v0.1.0")],
        "tests_green": True,
        "forecast": Forecast(date(2026, 11, 13)),
        "previous_forecast": Forecast(date(2026, 11, 13)),
    }
    return RunSummary(**(fields | overrides))  # type: ignore[arg-type]


def test_full_report() -> None:
    assert format_report(summary()) == (
        "📋 <b>Todo &lt;bot&gt;</b> · день 4 из 10 · 14.10\n"
        "\n"
        "✅ <b>T9</b> Модель категорий\n"
        "   Модель, 6 тестов\n"
        "✅ <b>T10</b> Команды /category\n"
        "   add/list/remove\n"
        "   💡 Категории на пользователя\n"
        "⏸ <b>T11</b> Импорт CSV — ждёт ответа на вопрос\n"
        "\n"
        '🧪 Тесты dev: зелёные · коммитов: 2 (<a href="https://github.com/x/commit/abc1234">'
        'abc1234</a>…<a href="https://github.com/x/commit/f00ba12">f00ba12</a>)\n'
        '🏷 Выпущено: <a href="https://github.com/x/releases/v0.1.0">v0.1.0</a>\n'
        "📅 Прогноз окончания: 13.11 (без изменений)"
    )


def test_minimal_report_without_links() -> None:
    text = format_report(
        summary(
            done=[],
            open=[],
            commits=[Link(text="abc1234")],
            releases=[],
            tests_green=None,
            day_number=None,
            note="Проект ждёт ответа на вопрос — слот пропущен",
            forecast=Forecast(date(2026, 11, 16)),
        )
    )

    assert text == (
        "📋 <b>Todo &lt;bot&gt;</b> · доделывание · 14.10\n"
        "\n"
        "Проект ждёт ответа на вопрос — слот пропущен\n"
        "\n"
        "коммитов: 1 (<code>abc1234</code>)\n"
        "📅 Прогноз окончания: 16.11 (было 13.11)"
    )


def test_batch_header() -> None:
    header = format_batch_header(date(2026, 10, 14), [summary(), summary(open=[])])

    assert header == "Запуск 14.10: проектов 2, ✅ 1, ⏸ 1"


async def test_store_and_latest(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from tests.test_models import make_project

    async with session_factory() as session, session.begin():
        project = make_project()
        session.add(project)
    service = ReportService(session_factory)
    ref = ProjectRef.of(project)

    await service.store(summary(project=ref, run_id=None))
    await service.store(summary(project=ref, run_id=None, day_number=5))

    latest = await service.latest(project.id)
    assert latest is not None
    assert "день 5 из 10" in latest.text
    assert latest.data["done"][0]["key"] == "T9"
    assert latest.data["date"] == "2026-10-14"
    assert await service.latest(project.id + 1) is None
    assert isinstance(project, Project)
