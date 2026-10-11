"""Diff двух версий плана по ключам задач (docs/plan-format.md#diff)."""

from dayforge.core.plan_format import PlanContent, PlanTask

_TASK_FIELDS = {
    "title": "название",
    "description": "описание",
    "acceptance_criteria": "критерии",
    "depends_on": "зависимости",
    "milestone": "веха",
}


def diff_plans(old: PlanContent, new: PlanContent, *, done_keys: frozenset[str]) -> list[str]:
    """Строки diff для пользователя: + добавлено, − удалено, ~ изменено, ↪ перенесено.

    Выполненные задачи в новый план не входят, но удалёнными не считаются.
    """
    old_tasks = _positions(old)
    new_tasks = _positions(new)
    lines: list[str] = []

    for key, (day, index, task) in new_tasks.items():
        if key not in old_tasks:
            lines.append(f"+ {key} {task.title} (день {day})")
            continue
        old_day, old_index, old_task = old_tasks[key]
        changed = [
            label
            for field, label in _TASK_FIELDS.items()
            if getattr(old_task, field) != getattr(task, field)
        ]
        if changed:
            lines.append(f"~ {key} {task.title}: {', '.join(changed)}")
        if old_day != day:
            lines.append(f"↪ {key} {task.title}: день {old_day} → {day}")
        elif old_index != index:
            lines.append(f"↪ {key} {task.title}: другой порядок в дне {day}")

    for key, (day, _, task) in old_tasks.items():
        if key not in new_tasks and key not in done_keys:
            lines.append(f"− {key} {task.title} (был в дне {day})")

    lines.extend(_milestone_lines(old, new))
    if old.test_command != new.test_command:
        lines.append(f"~ команда тестов: `{old.test_command}` → `{new.test_command}`")
    if old.days[-1].day != new.days[-1].day:
        lines.append(f"~ дней в плане: {old.days[-1].day} → {new.days[-1].day}")
    return lines


def _positions(plan: PlanContent) -> dict[str, tuple[int, int, PlanTask]]:
    return {
        task.key: (day.day, index, task)
        for day in plan.days
        for index, task in enumerate(day.tasks)
    }


def _milestone_lines(old: PlanContent, new: PlanContent) -> list[str]:
    old_milestones = {m.key: m for m in old.milestones}
    new_milestones = {m.key: m for m in new.milestones}
    lines = []
    for key, milestone in new_milestones.items():
        previous = old_milestones.get(key)
        if previous is None:
            lines.append(f"+ веха {key} {milestone.title} ({milestone.version})")
        elif previous != milestone:
            lines.append(
                f"~ веха {key}: {previous.title} ({previous.version}) → "
                f"{milestone.title} ({milestone.version})"
            )
    for key, milestone in old_milestones.items():
        if key not in new_milestones:
            lines.append(f"− веха {key} {milestone.title}")
    return lines
