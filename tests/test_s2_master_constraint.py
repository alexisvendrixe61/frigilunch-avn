"""
S2 is a property of the SET of orders on a line-day, so it belongs in the
assignment model — not in the oracle that discovers it one failed day at a
time.

A non-empty line needs an order allowed to OPEN it and one allowed to CLOSE
it. That statement carries no sequencing information, and mag_eerst /
mag_laatst are static product attributes, so the master can enforce it up
front. Before it did, such an order was assigned a day where no legal
arrangement existed, removed by the day builder, and answered with a no-good
cut that says "this set is too big" — the opposite of the "this set is
missing something" that actually happened. Each iteration emptied the day
further and made the next failure more likely, so the loop never recovered.

These run the real CP-SAT solver on 2-3 orders, so they finish instantly.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.plan_state import order_identity
from src.scheduler_ortools_v3 import assign_production_dates_feedback
from tests.conftest import make_order, make_product

START = date(2026, 7, 27)          # a Monday
LEVER = START + timedelta(days=9)


def _solve(orders, products, setup_matrix):
    return assign_production_dates_feedback(
        orders, products, START, weeks=2,
        setup_matrix=setup_matrix, toestellen={},
    )


def _days(scheduled):
    return {o.order_nr: (o.productie_datum, o.lijn) for o in scheduled}


def test_an_order_that_cannot_close_is_placed_WITH_a_partner(setup_matrix):
    """The whole point: it is placeable, so it must be placed."""
    products = {
        "SOLO": make_product("SOLO", snelheid=1000, afsluit_kan_niet=True),
        "MATE": make_product("MATE", snelheid=1000),
    }
    solo = make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO")
    mate = make_order("MATE", 2000, START, lever=LEVER, order_nr="MATE")

    scheduled, rejected, _ = _solve([solo, mate], products, setup_matrix)

    assert rejected == [], "both orders fit; neither should be rejected"
    placed = _days(scheduled)
    assert placed["SOLO"] == placed["MATE"], (
        "the order that may not close has to share a line-day with one that can"
    )


def test_an_order_that_cannot_close_is_never_placed_alone(setup_matrix):
    """
    With no eligible closer anywhere, the master must not assign it a day at
    all — previously it did, and the day builder threw it away afterwards.
    """
    products = {"SOLO": make_product("SOLO", snelheid=1000, afsluit_kan_niet=True)}
    solo = make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO")

    scheduled, rejected, _ = _solve([solo], products, setup_matrix)

    assert scheduled == []
    assert [o.order_nr for o in rejected] == ["SOLO"]


def test_the_same_holds_for_an_order_that_may_not_open(setup_matrix):
    products = {
        "SOLO": make_product("SOLO", snelheid=1000, opstart_kan_niet=True),
        "MATE": make_product("MATE", snelheid=1000),
    }
    solo = make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO")
    mate = make_order("MATE", 2000, START, lever=LEVER, order_nr="MATE")

    scheduled, rejected, _ = _solve([solo, mate], products, setup_matrix)

    assert rejected == []
    placed = _days(scheduled)
    assert placed["SOLO"] == placed["MATE"]


def _both_ends_barred():
    return {
        "SOLO": make_product(
            "SOLO", snelheid=1000, opstart_kan_niet=True, afsluit_kan_niet=True
        ),
        "MATE": make_product("MATE", snelheid=1000),
    }


def test_a_product_barred_from_both_ends_runs_between_two_partners(setup_matrix):
    orders = [
        make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO"),
        make_order("MATE", 2000, START, lever=LEVER, order_nr="MATE1"),
        make_order("MATE", 2000, START, lever=LEVER, order_nr="MATE2"),
    ]
    scheduled, rejected, _ = _solve(orders, _both_ends_barred(), setup_matrix)

    assert rejected == []
    placed = _days(scheduled)
    assert placed["SOLO"] == placed["MATE1"] == placed["MATE2"]


def test_one_partner_is_not_enough_for_a_product_barred_from_both_ends(setup_matrix):
    """
    With only two orders on the line, SOLO has to take an end whatever the
    sequence — "someone may open and someone may close" is satisfied by MATE
    alone, and MATE cannot run both first and last.

    That used to be left to the oracle: the master was satisfied, assigned the
    pair a day, and the day builder threw SOLO away. The two ends of a day are
    two DIFFERENT orders, so a line-day carrying two or more of them needs two
    that can take an end — also a necessary condition, and now stated up front
    alongside the other one.
    """
    orders = [
        make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO"),
        make_order("MATE", 2000, START, lever=LEVER, order_nr="MATE"),
    ]
    scheduled, rejected, _ = _solve(orders, _both_ends_barred(), setup_matrix)

    assert [o.order_nr for o in rejected] == ["SOLO"]
    assert [o.order_nr for o in scheduled] == ["MATE"]


def test_an_order_that_takes_both_ends_itself_may_run_alone(setup_matrix):
    """The constraint must not ban ordinary single-order days."""
    products = {"A": make_product("A", snelheid=1000)}
    a = make_order("A", 2000, START, lever=LEVER, order_nr="A")

    scheduled, rejected, _ = _solve([a], products, setup_matrix)

    assert rejected == []
    assert len(scheduled) == 1


# ── what an EARLIER phase's orders already do for the ends ───────────────────
#
# Under the two-phase dispatch, line-restricted orders are solved first and
# flexible ones against what they left. The second solve sees the first's
# orders only as a minutes budget per (day, line) — they have no variables in
# its model. S2 used to be switched off entirely for every line-day carrying
# such a budget, which is every day phase 1 touched, and that is where the
# position-rule rejections came from. `_reserved_ends` carries what those
# orders do for the ends alongside the minutes, so the rule stays on.

def _phase1_and_phase2():
    return {
        # alleen_l1 -> phase 1, and able to take either end of its day.
        "HARD": make_product("HARD", snelheid=1000, alleen_l1=True),
        # flexible -> phase 2, and able to take neither.
        "SOLO": make_product(
            "SOLO", snelheid=1000, opstart_kan_niet=True, afsluit_kan_niet=True
        ),
        "MATE": make_product("MATE", snelheid=1000),
    }


def test_reserved_ends_reports_what_a_built_day_can_take(setup_matrix):
    from src.multi_day_scheduler import schedule_all_days
    from src.scheduler_ortools_v3 import _reserved_ends_from

    products = _phase1_and_phase2()
    orders = [
        make_order("HARD", 2000, START, lever=LEVER, order_nr="HARD", lijn=1),
        make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO", lijn=1),
        make_order("MATE", 2000, START, lever=LEVER, order_nr="MATE", lijn=1),
    ]
    schedules = schedule_all_days(orders, products, setup_matrix, {})

    ends = _reserved_ends_from(schedules)
    has_open, has_close, n, capable = ends[(START, 1)]
    assert (has_open, has_close, n) == (True, True, 3)
    assert capable == 2, "HARD and MATE can take an end; SOLO can take neither"
    assert (START, 2) not in ends, "an empty line contributes nothing"


def test_phase_two_counts_the_earlier_phase_s_orders_toward_the_ends(setup_matrix):
    """
    SOLO can take neither end, so it needs two day-mates that can. Only one of
    them (MATE) is in phase 2's own model; the other is the phase-1 order
    already on the day. Reading the reserved set's end capability is what lets
    phase 2 see that this day is legal — and the built day must then keep all
    three, with nothing removed for the position rule.
    """
    products = _phase1_and_phase2()
    orders = [
        make_order("HARD", 2000, START, lever=LEVER, order_nr="HARD"),
        make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO"),
        make_order("MATE", 2000, START, lever=LEVER, order_nr="MATE"),
    ]

    scheduled, rejected, _ = _solve(orders, products, setup_matrix)

    assert rejected == [], [o.order_nr for o in rejected]
    placed = _days(scheduled)
    assert placed["SOLO"] == placed["MATE"], "SOLO always needs a flexible mate"

    from src.multi_day_scheduler import schedule_all_days
    for ds in schedule_all_days(scheduled, products, setup_matrix, {}):
        assert not ds.positie_dropped, ds.warnings


def test_a_reserved_day_is_not_a_free_pass_for_the_position_rule(setup_matrix):
    """
    The other half. With no flexible mate at all, SOLO cannot legally join the
    phase-1 day either: two orders, only one of them able to take an end. It
    used to be assigned there anyway — S2 was off for that day — and thrown
    away by the day builder afterwards, which is a POSITIE-REGEL rejection for
    something the model could have known up front.
    """
    products = _phase1_and_phase2()
    orders = [
        make_order("HARD", 2000, START, lever=LEVER, order_nr="HARD"),
        make_order("SOLO", 2000, START, lever=LEVER, order_nr="SOLO"),
    ]

    scheduled, rejected, stats = _solve(orders, products, setup_matrix)

    assert [o.order_nr for o in rejected] == ["SOLO"]
    assert [o.order_nr for o in scheduled] == ["HARD"]
    reason = next(iter(stats["rejection_reasons"].values()))
    assert not reason.startswith("POSITIE-REGEL"), (
        f"the model should refuse the day, not the day builder: {reason}"
    )
