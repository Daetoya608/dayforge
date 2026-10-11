"""Слоты проекта и прогноз окончания (docs/calendar.md).

Чистые функции: правила календаря собирает CalendarService из БД и передаёт сюда.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

FORECAST_HORIZON_DAYS = 365


@dataclass(frozen=True, kw_only=True)
class ProjectCalendar:
    """Правила календаря одного проекта."""

    # Дата утверждения плана: раньше неё слотов нет.
    starts_on: date
    weekdays: frozenset[int] = frozenset()  # 0 = понедельник
    include: frozenset[date] = frozenset()
    exclude: frozenset[date] = frozenset()
    days_off: frozenset[date] = frozenset()  # общие выходные для всех проектов

    def is_slot(self, day: date) -> bool:
        if day < self.starts_on or day in self.days_off:
            return False
        if day in self.include:
            return True
        return day.weekday() in self.weekdays and day not in self.exclude

    def slots(self, start: date, *, horizon_days: int = FORECAST_HORIZON_DAYS) -> Iterator[date]:
        """Слоты начиная со `start` включительно, на горизонте `horizon_days` дней."""
        for offset in range(horizon_days):
            day = start + timedelta(days=offset)
            if self.is_slot(day):
                yield day


@dataclass(frozen=True)
class Forecast:
    # None — проект завершён или в календаре не хватает дней (тогда missing_days > 0).
    end_date: date | None
    missing_days: int = 0


def forecast(
    calendar: ProjectCalendar,
    *,
    today: date,
    today_slot_used: bool,
    planned_days: int,
    current_day: int,
    has_unfinished: bool,
    horizon_days: int = FORECAST_HORIZON_DAYS,
) -> Forecast:
    """Дата слота, на который придётся последний день плана.

    Каждый слот выполняет следующий день плана; незавершённые задачи доделываются в начале
    ближайшего слота и отдельного слота не занимают.
    """
    to_start = planned_days - current_day
    needed = to_start if to_start > 0 else int(has_unfinished)
    if needed == 0:
        return Forecast(None)

    first = today + timedelta(days=1) if today_slot_used else today
    found = 0
    for slot in calendar.slots(first, horizon_days=horizon_days):
        found += 1
        if found == needed:
            return Forecast(slot)
    return Forecast(None, missing_days=needed - found)
