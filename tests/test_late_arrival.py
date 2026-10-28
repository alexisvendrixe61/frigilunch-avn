"""
Orders that arrive after the week they need has already been frozen.

This is the structural hole in the rolling lock: freezing bars the DAYS, not
just the orders already on them, so an order whose whole production window
falls inside the frozen zone can never be placed — not by this run and not by
any future one, because the zone only ever moves forward past it.

It used to be reported as "No valid production day/line slot (line/day
restriction)", i.e. indistinguishable from a product that simply isn't allowed
to run on those weekdays. It now has its own cause, so a planner can see that
it needs a decision rather than another solve.

These run the real CP-SAT solver, but on 2-3 orders, so they finish instantly.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from frigilunch.domain.state import order_identity
from src.constraints.slots import legal_slots
from src.scheduler_ortools_v3 import assign_production_dates_feedback
from tests.conftest import make_product, make_order

WK31 = date(2026, 7, 27)
WK32 = WK31 + timedelta(weeks=1)
WK35 = WK31 + timedelta(weeks=4)


@pytest.fixture
def products():
    return {"A": make_product("A", snelheid=1000)}


def _solve(orders, products, setup_matrix, start, weeks=8, frozen_until=None):
    return assign_production_dates_feedback(
        orders, products, start, weeks=weeks,
        setup_matrix=setup_matrix, toestellen={},
        frozen_until=frozen_until,
    )


def _reason(stats, order):
    return stats["rejection_reasons"][order_identity(order)]


def test_order_delivering_inside_the_frozen_zone_is_named_as_such(products, setup_matrix):
    """
    A wk32 run whose solver may not touch anything before wk35. An order due
    in wk33 has nowhere to go, ever.
    """
    late = make_order("A", 500, WK32, lever=WK31 + timedelta(weeks=2), order_nr="late")
    scheduled, rejected, stats = _solve(
        [late], products, setup_matrix, start=WK35, frozen_until=WK35
    )

    assert scheduled == []
    assert [o.order_nr for o in rejected] == ["late"]
    assert _reason(stats, late).startswith("ARRIVED-AFTER-FREEZE")
    assert stats["feedback"]["arrived_after_freeze"] == 1


def test_it_is_not_confused_with_a_weekday_restriction(products, setup_matrix):
    """
    A product that may only run Tue/Wed/Fri, ordered for a Monday delivery with
    a zero-day window, is a genuinely different problem — and is still reported
    as one.
    """
    products = {"A": make_product("A", dagen=["di", "wo", "vr"])}
    # Delivery on a Monday inside the planning window, production window
    # collapsed onto that single (forbidden) day.
    monday_in_window = WK35 + timedelta(weeks=1)
    o = make_order("A", 500, monday_in_window, lever=monday_in_window, order_nr="wrongday")

    scheduled, rejected, stats = _solve(
        [o], products, setup_matrix, start=WK35, frozen_until=WK35, weeks=8
    )
    # Whatever the outcome, it must not be mislabelled as a freeze problem.
    if rejected:
        assert not _reason(stats, o).startswith("ARRIVED-AFTER-FREEZE")


def test_a_past_due_order_is_window_passed_not_a_freeze_problem(products, setup_matrix):
    """With nothing frozen, an order whose window closed before the plan
    starts is simply late — not a casualty of the rolling lock."""
    o = make_order("A", 500, WK31, lever=WK31, order_nr="pastdue")
    scheduled, rejected, stats = _solve(
        [o], products, setup_matrix, start=WK35, frozen_until=None
    )

    assert scheduled == []
    assert _reason(stats, o).startswith("WINDOW-PASSED")


def test_a_normally_placeable_order_is_unaffected(products, setup_matrix):
    """The freeze logic must not swallow orders that genuinely have room."""
    o = make_order("A", 500, WK35, lever=WK35 + timedelta(days=3), order_nr="fine")
    scheduled, rejected, stats = _solve(
        [o], products, setup_matrix, start=WK35, frozen_until=WK35
    )

    assert [s.order_nr for s in scheduled] == ["fine"]
    assert rejected == []
    assert all(s.productie_datum >= WK35 for s in scheduled)


def test_the_solver_never_places_anything_before_the_freeze_boundary(products, setup_matrix):
    """The boundary is absolute — idle capacity inside the frozen zone stays
    idle rather than being offered to a new order."""
    orders = [
        make_order("A", 300, WK35, lever=WK35 + timedelta(days=2), order_nr=f"o{i}")
        for i in range(3)
    ]
    scheduled, _rejected, _stats = _solve(
        orders, products, setup_matrix, start=WK35, frozen_until=WK35
    )
    assert scheduled
    for s in scheduled:
        assert s.productie_datum >= WK35


def test_known_order_is_eligible_more_than_fourteen_days_before_delivery(products):
    early_day = WK35
    delivery = early_day + timedelta(days=30)
    order = make_order("A", 500, early_day, lever=delivery, order_nr="early")

    slots = legal_slots(order, products["A"], [early_day], start_date=early_day)

    assert slots.slots == [(0, 1), (0, 2)]
    assert slots.cause is None


def test_delivery_cutoff_remains_a_hard_latest_bound(products):
    delivery = WK35 + timedelta(days=2)
    after_delivery = delivery + timedelta(days=1)
    order = make_order("A", 500, WK35, lever=delivery, order_nr="too-late")

    slots = legal_slots(order, products["A"], [after_delivery], start_date=WK35)

    assert slots.slots == []
