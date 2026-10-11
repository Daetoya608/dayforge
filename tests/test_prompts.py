import pytest

from dayforge import prompts

ANSWERS = [{"question": "Валюта?", "answer": "Рубли"}, {"question": "Мультиюзер?", "answer": "Нет"}]

CASES = {
    "planner_system.md": {},
    "planner_clarify.md": {"name": "Todo", "days": 10},
    "planner_answers.md": {"answers": ANSWERS},
    "planner_feedback.md": {"feedback": "Объедини дни 3 и 4\nИ используй FastAPI"},
    "planner_replan.md": {
        "name": "Todo",
        "instructions": "Добавь экспорт",
        "current_day": 3,
        "planned_days": 10,
        "first_day": 4,
        "done_keys": ["T1", "T2"],
        "active_keys": [],
    },
    "executor_system.md": {},
    "executor_task.md": {
        "key": "T3",
        "title": "Категории",
        "day": 2,
        "description": "Модель и команды",
        "criteria": ["тесты проходят", "есть /category"],
        "depends_on": ["T1"],
        "branch": "feature/T3-kategorii",
        "test_command": "uv run pytest -q",
        "restarted": True,
    },
    "executor_resume.md": {"secret_name": None, "answers": ANSWERS},
    "executor_continue.md": {"reason": "лимит подписки"},
    "executor_fix_tests.md": {"test_command": "pytest", "output": "FAILED test_x"},
    "executor_merge_conflict.md": {"files": ["README.md"]},
    "executor_need_result.md": {},
    "system_agent_system.md": {"today": "11.10.2026"},
}


@pytest.mark.parametrize("template", sorted(CASES))
def test_template_renders_without_blank_line_runs(template: str) -> None:
    text = prompts.render(template, **CASES[template])

    assert text.strip()
    assert "\n\n\n" not in text
    assert "{{" not in text and "{%" not in text


def test_lists_render_as_markdown() -> None:
    task = prompts.render("executor_task.md", **CASES["executor_task.md"])
    replan = prompts.render("planner_replan.md", **CASES["planner_replan.md"])
    feedback = prompts.render("planner_feedback.md", **CASES["planner_feedback.md"])

    assert "Критерии готовности:\n- тесты проходят\n- есть /category\n" in task
    assert "выполнены (в план не включать, ключи не переиспользовать): T1, T2;" in replan
    assert "в работе (из плана не удалять): —;" in replan
    assert "> Объедини дни 3 и 4\n> И используй FastAPI" in feedback


def test_secret_resume() -> None:
    text = prompts.render("executor_resume.md", secret_name="STRIPE_KEY", answers=[])

    assert text.startswith("Пользователь добавил секрет `STRIPE_KEY`")
