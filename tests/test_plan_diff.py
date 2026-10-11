import copy
from typing import Any

from dayforge.core.plan_diff import diff_plans
from dayforge.core.plan_format import PlanContent
from tests.test_plan_format import make_plan, task


def parse(raw: dict[str, Any]) -> PlanContent:
    return PlanContent.model_validate(raw)


def test_identical_plans_have_no_diff() -> None:
    assert diff_plans(parse(make_plan()), parse(make_plan()), done_keys=frozenset()) == []


def test_task_changes() -> None:
    old = make_plan()
    new = copy.deepcopy(old)
    new["days"][1]["tasks"][0]["description"] = "Новое описание"  # T3 изменена
    new["days"][1]["tasks"].append(task("T5"))  # T5 добавлена
    moved = new["days"][0]["tasks"].pop(1)  # T2 перенесена в день 2
    new["days"][1]["tasks"].insert(0, moved)
    new["days"][2]["tasks"] = [task("T6", milestone="M2")]  # T4 удалена

    lines = diff_plans(parse(old), parse(new), done_keys=frozenset())

    assert lines == [
        "↪ T2 Задача T2: день 1 → 2",
        "~ T3 Задача T3: описание",
        "↪ T3 Задача T3: другой порядок в дне 2",
        "+ T5 Задача T5 (день 2)",
        "+ T6 Задача T6 (день 3)",
        "− T4 Задача T4 (был в дне 3)",
    ]


def test_done_tasks_are_not_reported_as_removed() -> None:
    old = make_plan()
    new = copy.deepcopy(old)
    new["days"] = new["days"][1:]

    lines = diff_plans(parse(old), parse(new), done_keys=frozenset({"T1", "T2"}))

    assert lines == []


def test_milestones_test_command_and_day_count() -> None:
    old = make_plan()
    new = copy.deepcopy(old)
    new["milestones"][1]["version"] = "v1.0.0"
    new["milestones"].insert(1, {"key": "M3", "title": "Экспорт", "version": "v0.3.0"})
    new["days"][1]["tasks"][0]["milestone"] = "M3"
    new["test_command"] = "make test"
    new["days"].append({"day": 4, "goal": "Полировка", "tasks": [task("T7", milestone="M2")]})

    lines = diff_plans(parse(old), parse(new), done_keys=frozenset())

    assert lines == [
        "~ T3 Задача T3: веха",
        "+ T7 Задача T7 (день 4)",
        "+ веха M3 Экспорт (v0.3.0)",
        "~ веха M2: Отчёты (v0.2.0) → Отчёты (v1.0.0)",
        "~ команда тестов: `uv run pytest -q` → `make test`",
        "~ дней в плане: 3 → 4",
    ]
