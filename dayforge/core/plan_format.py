"""Формат плана из submit_plan и его проверка (docs/plan-format.md)."""

from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TaskKey = Annotated[str, StringConstraints(pattern=r"^T\d+$")]
MilestoneKey = Annotated[str, StringConstraints(pattern=r"^M\d+$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanMilestone(_Model):
    key: MilestoneKey
    title: NonEmpty
    version: Annotated[str, StringConstraints(pattern=r"^v\d+\.\d+\.\d+$")]


class PlanTask(_Model):
    key: TaskKey
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    description: NonEmpty
    acceptance_criteria: Annotated[list[NonEmpty], Field(min_length=1)]
    depends_on: list[TaskKey] = []
    milestone: MilestoneKey


class PlanDay(_Model):
    day: Annotated[int, Field(ge=1)]
    goal: NonEmpty
    tasks: Annotated[list[PlanTask], Field(min_length=1)]


class PlanContent(_Model):
    summary: NonEmpty
    change_summary: str | None = None
    estimate_comment: NonEmpty
    test_command: NonEmpty
    milestones: Annotated[list[PlanMilestone], Field(min_length=1)]
    days: Annotated[list[PlanDay], Field(min_length=1)]

    def iter_tasks(self) -> list[tuple[str, PlanTask]]:
        """Задачи в порядке выполнения вместе с путём для сообщений об ошибках."""
        return [
            (f"days[{d}].tasks[{t}]", task)
            for d, day in enumerate(self.days)
            for t, task in enumerate(day.tasks)
        ]


@dataclass(frozen=True, kw_only=True)
class PlanContext:
    """Что уже есть у проекта к моменту проверки плана."""

    planned_days: int
    first_day: int = 1
    # Выполненные задачи: в плане их быть не должно, ссылаться на них можно.
    done_keys: frozenset[str] = frozenset()
    # Задачи в работе (running, verifying, paused): удалять из плана нельзя.
    active_keys: frozenset[str] = frozenset()
    # Вехи с выполненными задачами или уже выпущенные: в новом плане им задачи не нужны.
    done_milestones: frozenset[str] = frozenset()

    @property
    def is_replan(self) -> bool:
        return self.first_day > 1 or bool(self.done_keys) or bool(self.active_keys)


@dataclass(frozen=True)
class CheckedPlan:
    plan: PlanContent
    warnings: list[str] = field(default_factory=list)


class PlanValidationError(Exception):
    def __init__(self, details: list[str]) -> None:
        super().__init__("; ".join(details))
        self.details = details


def check_plan(raw: dict[str, Any], context: PlanContext) -> CheckedPlan:
    """Проверяет план целиком. Ошибки собираются все сразу, чтобы агент исправил их за раз."""
    try:
        plan = PlanContent.model_validate(raw)
    except ValidationError as exc:
        raise PlanValidationError([_format_pydantic_error(e) for e in exc.errors()]) from None

    errors = [
        *_check_days(plan, context),
        *_check_keys(plan, context),
        *_check_dependencies(plan, context),
        *_check_milestones(plan, context),
    ]
    if context.is_replan and not plan.change_summary:
        errors.append("change_summary: обязателен при перепланировании")
    if errors:
        raise PlanValidationError(errors)

    warnings = []
    day_count = len(plan.days) + context.first_day - 1
    if day_count != context.planned_days:
        warnings.append(
            f"План на {day_count} дн., заказано {context.planned_days} — "
            "пользователь увидит предупреждение"
        )
    return CheckedPlan(plan, warnings)


def _format_pydantic_error(error: Any) -> str:
    path = ""
    for part in error["loc"]:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return f"{path.lstrip('.') or 'plan'}: {error['msg']}"


def _check_days(plan: PlanContent, context: PlanContext) -> list[str]:
    errors = []
    for index, day in enumerate(plan.days):
        expected = context.first_day + index
        if day.day != expected:
            errors.append(f"days[{index}].day: ожидается {expected}, получено {day.day}")
    return errors


def _check_keys(plan: PlanContent, context: PlanContext) -> list[str]:
    errors = []
    seen: set[str] = set()
    for path, task in plan.iter_tasks():
        if task.key in seen:
            errors.append(f"{path}.key: задача {task.key} встречается в плане повторно")
        elif task.key in context.done_keys:
            errors.append(
                f"{path}.key: задача {task.key} уже выполнена — "
                "выполненные задачи в план не входят, ключ нельзя переиспользовать"
            )
        seen.add(task.key)

    for key in sorted(context.active_keys - seen):
        errors.append(f"days: задача {key} в работе и не может быть удалена из плана")

    milestone_keys: set[str] = set()
    for index, milestone in enumerate(plan.milestones):
        if milestone.key in milestone_keys:
            errors.append(f"milestones[{index}].key: веха {milestone.key} повторяется")
        milestone_keys.add(milestone.key)
    return errors


def _check_dependencies(plan: PlanContent, context: PlanContext) -> list[str]:
    # Зависимость должна стоять строго раньше задачи — это заодно исключает циклы.
    position = {task.key: index for index, (_, task) in enumerate(plan.iter_tasks())}
    errors = []
    for index, (path, task) in enumerate(plan.iter_tasks()):
        for dep in task.depends_on:
            if dep in context.done_keys:
                continue
            if dep == task.key:
                errors.append(f"{path}.depends_on: задача не может зависеть от самой себя")
            elif dep not in position:
                errors.append(f"{path}.depends_on: задачи {dep} нет в плане")
            elif position[dep] > index:
                errors.append(
                    f"{path}.depends_on: задача {dep} выполняется позже {task.key} — "
                    "зависимость должна быть раньше"
                )
    return errors


def _check_milestones(plan: PlanContent, context: PlanContext) -> list[str]:
    defined = [m.key for m in plan.milestones]
    used: set[str] = set()
    errors = []
    for path, task in plan.iter_tasks():
        if task.milestone not in defined:
            errors.append(f"{path}.milestone: вехи {task.milestone} нет в milestones")
        used.add(task.milestone)

    for index, key in enumerate(defined):
        if key not in used and key not in context.done_milestones:
            errors.append(f"milestones[{index}]: у вехи {key} нет задач")

    last_path, last_task = plan.iter_tasks()[-1]
    if defined and last_task.milestone != defined[-1]:
        errors.append(
            f"{last_path}.milestone: последняя задача плана должна входить в последнюю веху "
            f"{defined[-1]} — к концу проекта всё должно быть выпущено в main"
        )
    return errors
