import copy
from typing import Any

import pytest

from dayforge.core.plan_format import PlanContext, PlanValidationError, check_plan


def task(key: str, *, milestone: str = "M1", depends_on: list[str] | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "title": f"Задача {key}",
        "description": f"Описание {key}",
        "acceptance_criteria": ["тесты проходят"],
        "depends_on": depends_on or [],
        "milestone": milestone,
    }


def make_plan() -> dict[str, Any]:
    return {
        "summary": "Бот учёта расходов",
        "estimate_comment": "3 дня: каркас, функции, отчёты",
        "test_command": "uv run pytest -q",
        "milestones": [
            {"key": "M1", "title": "Базовый бот", "version": "v0.1.0"},
            {"key": "M2", "title": "Отчёты", "version": "v0.2.0"},
        ],
        "days": [
            {"day": 1, "goal": "Каркас", "tasks": [task("T1"), task("T2", depends_on=["T1"])]},
            {"day": 2, "goal": "Функции", "tasks": [task("T3", depends_on=["T2"])]},
            {"day": 3, "goal": "Отчёты", "tasks": [task("T4", milestone="M2")]},
        ],
    }


def errors_of(raw: dict[str, Any], context: PlanContext | None = None) -> list[str]:
    with pytest.raises(PlanValidationError) as exc:
        check_plan(raw, context or PlanContext(planned_days=3))
    return exc.value.details


def test_valid_plan_passes_without_warnings() -> None:
    checked = check_plan(make_plan(), PlanContext(planned_days=3))

    assert [t.key for _, t in checked.plan.iter_tasks()] == ["T1", "T2", "T3", "T4"]
    assert checked.warnings == []


def test_schema_errors_have_readable_paths() -> None:
    raw = make_plan()
    raw["days"][0]["tasks"][1]["title"] = "x" * 101
    raw["days"][1]["tasks"][0]["acceptance_criteria"] = []
    raw["milestones"][0]["version"] = "0.1"
    del raw["test_command"]

    errors = errors_of(raw)

    assert any(e.startswith("days[0].tasks[1].title:") for e in errors)
    assert any(e.startswith("days[1].tasks[0].acceptance_criteria:") for e in errors)
    assert any(e.startswith("milestones[0].version:") for e in errors)
    assert any(e.startswith("test_command:") for e in errors)


def test_unknown_fields_are_rejected() -> None:
    raw = make_plan()
    raw["days"][0]["tasks"][0]["priority"] = "high"

    assert any(e.startswith("days[0].tasks[0].priority:") for e in errors_of(raw))


def test_days_must_be_consecutive() -> None:
    raw = make_plan()
    raw["days"][2]["day"] = 5

    assert errors_of(raw) == ["days[2].day: ожидается 3, получено 5"]


def test_duplicate_keys_are_rejected() -> None:
    raw = make_plan()
    raw["days"][2]["tasks"].append(task("T1", milestone="M2"))
    raw["milestones"].append({"key": "M1", "title": "Ещё раз", "version": "v0.3.0"})

    errors = errors_of(raw)

    assert any("задача T1 встречается в плане повторно" in e for e in errors)
    assert any("веха M1 повторяется" in e for e in errors)


@pytest.mark.parametrize(
    ("deps", "message"),
    [
        (["T9"], "задачи T9 нет в плане"),
        (["T3"], "задача не может зависеть от самой себя"),
        (["T4"], "задача T4 выполняется позже T3"),
    ],
)
def test_dependency_rules(deps: list[str], message: str) -> None:
    raw = make_plan()
    raw["days"][1]["tasks"][0]["depends_on"] = deps

    errors = errors_of(raw)

    assert len(errors) == 1
    assert errors[0].startswith("days[1].tasks[0].depends_on:")
    assert message in errors[0]


def test_milestone_rules() -> None:
    raw = make_plan()
    raw["days"][0]["tasks"][0]["milestone"] = "M7"
    raw["days"][2]["tasks"][0]["milestone"] = "M1"

    errors = errors_of(raw)

    assert "days[0].tasks[0].milestone: вехи M7 нет в milestones" in errors
    assert "milestones[1]: у вехи M2 нет задач" in errors
    assert any(e.startswith("days[2].tasks[0].milestone: последняя задача") for e in errors)


def test_day_count_mismatch_is_a_warning_not_an_error() -> None:
    checked = check_plan(make_plan(), PlanContext(planned_days=5))

    assert checked.warnings == ["План на 3 дн., заказано 5 — пользователь увидит предупреждение"]


class TestReplan:
    """Перепланирование: дни 1–2 начаты, T1 и T2 выполнены, T3 в работе."""

    context = PlanContext(
        planned_days=4,
        first_day=2,
        done_keys=frozenset({"T1", "T2"}),
        active_keys=frozenset({"T3"}),
    )

    def replan(self) -> dict[str, Any]:
        raw = copy.deepcopy(make_plan())
        raw["change_summary"] = "Добавлен экспорт"
        raw["days"] = [
            {"day": 2, "goal": "Функции", "tasks": [task("T3", depends_on=["T2"])]},
            {"day": 3, "goal": "Экспорт", "tasks": [task("T5", depends_on=["T3"])]},
            {"day": 4, "goal": "Отчёты", "tasks": [task("T4", milestone="M2")]},
        ]
        return raw

    def test_valid_replan(self) -> None:
        checked = check_plan(self.replan(), self.context)

        assert checked.warnings == []

    def test_change_summary_is_required(self) -> None:
        raw = self.replan()
        raw["change_summary"] = None

        assert errors_of(raw, self.context) == ["change_summary: обязателен при перепланировании"]

    def test_numbering_starts_from_first_day(self) -> None:
        raw = self.replan()
        for index, day in enumerate(raw["days"], start=1):
            day["day"] = index

        assert errors_of(raw, self.context)[0] == "days[0].day: ожидается 2, получено 1"

    def test_done_task_cannot_be_reused(self) -> None:
        raw = self.replan()
        raw["days"][1]["tasks"].append(task("T1"))

        assert any("задача T1 уже выполнена" in e for e in errors_of(raw, self.context))

    def test_milestone_with_done_tasks_may_have_no_new_tasks(self) -> None:
        raw = self.replan()
        for day in raw["days"]:
            for planned in day["tasks"]:
                planned["milestone"] = "M2"
        context = PlanContext(
            planned_days=4,
            first_day=2,
            done_keys=frozenset({"T1", "T2"}),
            active_keys=frozenset({"T3"}),
            done_milestones=frozenset({"M1"}),
        )

        assert check_plan(raw, context).warnings == []
        assert any("у вехи M1 нет задач" in e for e in errors_of(raw, self.context))

    def test_active_task_cannot_be_removed(self) -> None:
        raw = self.replan()
        raw["days"][0]["tasks"] = [task("T6")]
        raw["days"][1]["tasks"][0]["depends_on"] = ["T6"]

        assert errors_of(raw, self.context) == [
            "days: задача T3 в работе и не может быть удалена из плана"
        ]
