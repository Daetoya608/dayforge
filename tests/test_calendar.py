from datetime import date

import pytest

from dayforge.core.calendar import Forecast, ProjectCalendar, forecast

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# Пример из docs/calendar.md: проект A — Пн, Ср, Пт; 4 ноября — общий выходной;
# план на 5 дней утверждён в воскресенье 1 ноября 2026 года.
EXAMPLE = ProjectCalendar(
    starts_on=date(2026, 11, 1),
    weekdays=frozenset({MON, WED, FRI}),
    days_off=frozenset({date(2026, 11, 4)}),
)


def test_slots_follow_template_and_days_off() -> None:
    slots = list(EXAMPLE.slots(date(2026, 11, 1), horizon_days=14))

    assert slots == [
        date(2026, 11, 2),
        date(2026, 11, 6),
        date(2026, 11, 9),
        date(2026, 11, 11),
        date(2026, 11, 13),
    ]


def test_no_slots_before_plan_approval() -> None:
    assert not EXAMPLE.is_slot(date(2026, 10, 30))  # пятница, но до утверждения


def test_include_beats_template_and_exclude_beats_template() -> None:
    calendar = ProjectCalendar(
        starts_on=date(2026, 11, 1),
        weekdays=frozenset({MON}),
        include=frozenset({date(2026, 11, 7)}),  # суббота
        exclude=frozenset({date(2026, 11, 9)}),  # понедельник
    )

    assert calendar.is_slot(date(2026, 11, 7))
    assert not calendar.is_slot(date(2026, 11, 9))
    assert calendar.is_slot(date(2026, 11, 2))


def test_day_off_beats_include() -> None:
    calendar = ProjectCalendar(
        starts_on=date(2026, 11, 1),
        include=frozenset({date(2026, 12, 31)}),
        days_off=frozenset({date(2026, 12, 31)}),
    )

    assert not calendar.is_slot(date(2026, 12, 31))


def test_forecast_from_docs_example_without_early_start() -> None:
    result = forecast(
        EXAMPLE,
        today=date(2026, 11, 1),
        today_slot_used=False,
        planned_days=5,
        current_day=0,
        has_unfinished=False,
    )

    assert result == Forecast(date(2026, 11, 13))


def test_forecast_after_early_start_moves_end_earlier() -> None:
    # Вторник 3 ноября: день 1 выполнен в понедельник, день 2 — досрочно через /next.
    result = forecast(
        EXAMPLE,
        today=date(2026, 11, 3),
        today_slot_used=False,
        planned_days=5,
        current_day=2,
        has_unfinished=False,
    )

    assert result == Forecast(date(2026, 11, 11))


@pytest.mark.parametrize(
    ("today_slot_used", "expected"),
    [(False, date(2026, 11, 9)), (True, date(2026, 11, 11))],
)
def test_todays_slot_counts_only_if_unused(today_slot_used: bool, expected: date) -> None:
    result = forecast(
        EXAMPLE,
        today=date(2026, 11, 6),  # пятница — слот
        today_slot_used=today_slot_used,
        planned_days=5,
        current_day=3,
        has_unfinished=False,
    )

    assert result == Forecast(expected)


def test_unfinished_tasks_after_last_day_need_one_more_slot() -> None:
    result = forecast(
        EXAMPLE,
        today=date(2026, 11, 11),
        today_slot_used=True,
        planned_days=5,
        current_day=5,
        has_unfinished=True,
    )

    assert result == Forecast(date(2026, 11, 13))


def test_finished_project_has_no_forecast() -> None:
    result = forecast(
        EXAMPLE,
        today=date(2026, 11, 13),
        today_slot_used=True,
        planned_days=5,
        current_day=5,
        has_unfinished=False,
    )

    assert result == Forecast(None, missing_days=0)


def test_shortage_when_only_explicit_dates_are_marked() -> None:
    calendar = ProjectCalendar(
        starts_on=date(2026, 11, 1),
        include=frozenset({date(2026, 11, 2), date(2026, 11, 3)}),
    )

    result = forecast(
        calendar,
        today=date(2026, 11, 1),
        today_slot_used=False,
        planned_days=5,
        current_day=0,
        has_unfinished=False,
    )

    assert result == Forecast(None, missing_days=3)
