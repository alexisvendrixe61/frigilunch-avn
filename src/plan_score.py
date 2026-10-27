"""Canonical, side-effect-free production-plan scoring."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date

from .models import DaySchedule, Order, PlanScore
from .plan_state import order_identity

ForecastSlot = tuple[date, int]


def calculate_plan_score(
    schedules: Iterable[DaySchedule],
    rejected: Iterable[Order],
    *,
    proximity_grace_days: int = 0,
    previous_forecast: Mapping[str, ForecastSlot] | None = None,
) -> PlanScore:
    """Calculate the shared lexicographic score from exact rendered days.

    ``ombouw_voor`` is produced by the canonical changeover rules during day
    rendering, so this scorer never substitutes the assignment model's flat
    proxy buffer for actual transition time.
    """
    days = list(schedules)
    rejected_by_key: dict[str, Order] = {}
    for order in rejected:
        rejected_by_key.setdefault(order_identity(order), order)

    transition_minutes = 0
    scale_changes = 0
    early_days = 0
    current_forecast: dict[str, ForecastSlot] = {}

    for day in days:
        for line_number, line in ((1, day.lijn1), (2, day.lijn2)):
            transition_minutes += sum(run.ombouw_voor for run in line)
            scale_changes += sum(
                previous.product.schaal != current.product.schaal
                for previous, current in zip(line, line[1:])
            )
            for run in line:
                early = max(0, (run.order.lever_datum - day.datum).days)
                early_days += max(0, early - proximity_grace_days)
                current_forecast[order_identity(run.order)] = (day.datum, line_number)

    previous_forecast = previous_forecast or {}
    moved = sum(
        1
        for key, old_slot in previous_forecast.items()
        if key in current_forecast and current_forecast[key] != old_slot
    )

    return PlanScore(
        rejected_orders=len(rejected_by_key),
        rejected_units=sum(order.aantal for order in rejected_by_key.values()),
        transition_minutes=transition_minutes,
        scale_changes=scale_changes,
        early_days=early_days,
        moved_forecast_orders=moved,
    )
