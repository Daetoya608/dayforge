from datetime import date

from dayforge.core.calendar import Forecast
from dayforge.core.enums import PauseReason, TaskStatus
from dayforge.core.plan_render import MilestoneView, PlanView, TaskView, render_plan


def view_task(
    key: str, day: int | None, status: TaskStatus, position: int = 0, **extra: object
) -> TaskView:
    return TaskView(
        key=key,
        title=f"Задача {key}",
        description=f"Описание {key}",
        acceptance_criteria=["тесты проходят"],
        day_number=day,
        position=position,
        status=status,
        **extra,  # type: ignore[arg-type]
    )


def make_view(**overrides: object) -> PlanView:
    fields: dict[str, object] = {
        "name": "todo-bot",
        "version": 3,
        "summary": "Бот учёта расходов",
        "test_command": "uv run pytest -q",
        "current_day": 2,
        "planned_days": 3,
        "forecast": Forecast(date(2026, 10, 28)),
        "day_goals": {1: "Каркас", 2: "Категории", 3: "Отчёты"},
        "milestones": [
            MilestoneView(key="M1", title="Базовый бот", version="v0.1.0", released=True),
            MilestoneView(key="M2", title="Отчёты", version="v0.2.0", released=False),
        ],
        "tasks": [
            view_task("T1", 1, TaskStatus.DONE),
            view_task("T2", 1, TaskStatus.DONE, position=1),
            view_task("T3", 2, TaskStatus.DONE),
            view_task("T4", 2, TaskStatus.PAUSED, position=1, pause_reason=PauseReason.QUESTION),
            view_task("T5", 3, TaskStatus.PENDING),
            view_task("T6", 3, TaskStatus.CANCELLED, position=1),
            view_task("A1", None, TaskStatus.DONE),
        ],
    }
    return PlanView(**(fields | overrides))  # type: ignore[arg-type]


def test_render_full_plan() -> None:
    assert render_plan(make_view()) == (
        "# План: todo-bot\n"
        "\n"
        "> Сгенерировано Dayforge. Не редактировать вручную.\n"
        "> Версия плана: 3 · Прогноз окончания: 28.10.2026 · День 2 из 3\n"
        "\n"
        "**Бот учёта расходов**\n"
        "\n"
        "Тесты: `uv run pytest -q`\n"
        "\n"
        "## Вехи\n"
        "\n"
        "- [x] M1 — Базовый бот (v0.1.0)\n"
        "- [ ] M2 — Отчёты (v0.2.0)\n"
        "\n"
        "## День 1 — Каркас ✅\n"
        "\n"
        "- [x] **T1** Задача T1\n"
        "- [x] **T2** Задача T2\n"
        "\n"
        "## День 2 — Категории ⏳\n"
        "\n"
        "- [x] **T3** Задача T3\n"
        "- [ ] **T4** Задача T4 ⏸ ждёт ответа\n"
        "\n"
        "  Описание T4\n"
        "\n"
        "  Критерии:\n"
        "  - тесты проходят\n"
        "\n"
        "## День 3 — Отчёты\n"
        "\n"
        "- [ ] **T5** Задача T5\n"
        "\n"
        "  Описание T5\n"
        "\n"
        "  Критерии:\n"
        "  - тесты проходят\n"
        "\n"
        "## Вне плана\n"
        "\n"
        "- [x] **A1** Задача A1\n"
    )


def test_forecast_variants() -> None:
    shortage = render_plan(make_view(forecast=Forecast(None, missing_days=2)))
    finished = render_plan(make_view(forecast=Forecast(None)))

    assert "В календаре не хватает дней: 2" in shortage
    assert "Проект завершён" in finished


def test_status_markers() -> None:
    text = render_plan(
        make_view(
            tasks=[
                view_task("T1", 1, TaskStatus.RUNNING),
                view_task("T2", 1, TaskStatus.FAILED, position=1),
                view_task("T3", 1, TaskStatus.PAUSED, position=2, pause_reason=PauseReason.LIMIT),
            ]
        )
    )

    assert "**T1** Задача T1 🔨 в работе" in text
    assert "**T2** Задача T2 ✖ не выполнена" in text
    assert "**T3** Задача T3 ⏸ приостановлена" in text
