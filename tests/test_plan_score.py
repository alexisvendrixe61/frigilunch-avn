from __future__ import annotations

from datetime import date, time, timedelta

from frigilunch.domain.models import DaySchedule, PlannedOrder
from frigilunch.application.plan_score import calculate_plan_score
from frigilunch.domain.state import order_identity
from tests.conftest import make_order, make_product


MONDAY = date(2026, 9, 28)


def _run(article: str, *, scale: str, delivery_days: int, setup: int) -> PlannedOrder:
    order = make_order(
        article,
        100,
        MONDAY,
        lever=MONDAY + timedelta(days=delivery_days),
        lijn=1,
        order_nr=article,
    )
    product = make_product(article)
    product.schaal = scale
    return PlannedOrder(
        order=order,
        product=product,
        lijn=1,
        start=time(6, 0),
        eind=time(7, 0),
        ombouw_voor=setup,
    )


def _day(*runs: PlannedOrder) -> DaySchedule:
    return DaySchedule(datum=MONDAY, lijn1=list(runs))


def test_complete_plan_always_beats_an_incomplete_plan():
    expensive = _run("A", scale="large", delivery_days=1, setup=500)
    rejected = make_order("B", 1, MONDAY, lever=MONDAY + timedelta(days=1), order_nr="B")

    complete = calculate_plan_score([_day(expensive)], [])
    incomplete = calculate_plan_score([], [rejected])

    assert complete < incomplete


def test_exact_transition_efficiency_precedes_delivery_proximity():
    efficient_early = _run("A", scale="small", delivery_days=20, setup=10)
    inefficient_close = _run("A", scale="small", delivery_days=1, setup=40)

    efficient = calculate_plan_score([_day(efficient_early)], [])
    close = calculate_plan_score([_day(inefficient_close)], [])

    assert efficient < close


def test_scale_changes_and_soft_earliness_are_counted():
    first = _run("A", scale="small", delivery_days=10, setup=5)
    second = _run("B", scale="large", delivery_days=4, setup=20)

    score = calculate_plan_score([_day(first, second)], [], proximity_grace_days=3)

    assert score.transition_minutes == 25
    assert score.scale_changes == 1
    assert score.early_days == 8


def test_forecast_movement_is_the_last_tie_breaker():
    run = _run("A", scale="small", delivery_days=2, setup=10)
    key = order_identity(run.order)

    stable = calculate_plan_score(
        [_day(run)], [], previous_forecast={key: (MONDAY, 1)}
    )
    moved = calculate_plan_score(
        [_day(run)], [], previous_forecast={key: (MONDAY + timedelta(days=1), 1)}
    )

    assert stable < moved
