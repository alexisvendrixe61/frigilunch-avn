"""
A5 — sate ('X', allergen level 6) stops the other line.

Peanut travels through the hall, so while sate runs the other line may not be
producing anything else — except its own sate, side by side, since the second
Gernal (planner 22/10). And after sate nothing else runs on its own line. These are the properties that follow from that: the
block starts when the other line is done (at the latest against closing
time), the other line works up to it and no further, and what it costs is
reported as its own kind of drop rather than as capacity.
"""
from __future__ import annotations

from datetime import time

import pytest

from frigilunch.adapters import config
from frigilunch.domain.constraints.allergen import (
    SATE_EXCLUSIVE, check_sate_conflict, check_sate_sync, sate_window,
)
from frigilunch.application.scheduler import schedule_day
from tests.conftest import make_order, make_product


pytestmark = pytest.mark.skipif(
    not SATE_EXCLUSIVE,
    reason="A5 is switched off (FRIGI_SATE_EXCLUSIVE=0) — this file describes A5",
)


def _mins(t: time) -> int:
    return t.hour * 60 + t.minute


@pytest.fixture
def sate_and_filler():
    """
    Sate on L1, an ordinary product on L2. Both slow enough that a handful of
    orders is a real day.
    """
    return {
        "SATE": make_product(
            "SATE", naam="KIP SATE", allergie_niveau=6, snelheid=200, alleen_l1=True
        ),
        "PLAIN": make_product("PLAIN", snelheid=200, alleen_l2=True),
    }


# ── the window ───────────────────────────────────────────────────────────────

def test_sate_starts_when_the_other_line_stops(setup_matrix, monday, sate_and_filler):
    """
    The handover (planner, 02/10): with the other line done early, sliding the
    block to closing time only leaves both lines standing still.
    """
    orders = [
        make_order("SATE", 400, monday, order_nr="sate"),
        make_order("PLAIN", 400, monday, order_nr="plain"),
    ]

    ds = schedule_day(orders, sate_and_filler, setup_matrix, monday)

    assert [po.order.order_nr for po in ds.lijn1] == ["sate"]
    start, _ = sate_window(ds.lijn1)
    assert start == max(_mins(po.eind) for po in ds.lijn2)
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []


def test_sate_still_closes_the_day_when_the_other_line_runs_long(
    setup_matrix, monday, sate_and_filler
):
    """Placed any earlier, the rule would cost the other line everything it
    would have run afterwards — a stopped line does not start again."""
    # 2800 at 200/h is 14h: L2 runs 06:30-20:30, exactly up to where a
    # two-hour sate block closing the day has to open.
    orders = [
        make_order("SATE", 400, monday, order_nr="sate"),
        make_order("PLAIN", 2800, monday, order_nr="long"),
    ]

    ds = schedule_day(orders, sate_and_filler, setup_matrix, monday)

    assert [po.order.order_nr for po in ds.lijn2] == ["long"]
    assert ds.lijn1[-1].eind == config.shift_end(monday)
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []


def test_handover_never_moves_sate_before_its_own_predecessor(setup_matrix, monday):
    products = {
        "SATE": make_product("SATE", allergie_niveau=6, snelheid=200, alleen_l1=True),
        "BODY": make_product("BODY", snelheid=200, alleen_l1=True),
        "PLAIN": make_product("PLAIN", snelheid=200, alleen_l2=True),
    }
    orders = [
        make_order("BODY", 1200, monday, order_nr="body"),
        make_order("SATE", 200, monday, order_nr="sate"),
        make_order("PLAIN", 100, monday, order_nr="plain"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert [po.order.order_nr for po in ds.lijn1] == ["body", "sate"]
    start, _ = sate_window(ds.lijn1)
    assert start == _mins(ds.lijn1[0].eind)


def test_the_other_line_is_stopped_before_the_sate_starts(
    setup_matrix, monday, sate_and_filler
):
    orders = [make_order("SATE", 400, monday, order_nr="sate")] + [
        make_order("PLAIN", 300, monday, order_nr=f"p{i}") for i in range(6)
    ]

    ds = schedule_day(orders, sate_and_filler, setup_matrix, monday)

    window = sate_window(ds.lijn1)
    assert window is not None
    assert ds.lijn2, "L2 should still get most of the day"
    for po in ds.lijn2:
        assert _mins(po.eind) <= window[0], (
            f"{po.order.order_nr} runs to {po.eind} into the sate window "
            f"opening at {window[0] // 60:02d}:{window[0] % 60:02d}"
        )
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []


def test_the_other_line_still_gets_the_whole_morning(
    setup_matrix, monday, sate_and_filler
):
    """
    The point of sliding the block to the end: the loss is the last hours, not
    the day. A day whose sate is short must leave L2 nearly its full shift.
    """
    orders = [make_order("SATE", 100, monday, order_nr="sate")] + [
        make_order("PLAIN", 300, monday, order_nr=f"p{i}") for i in range(8)
    ]

    ds = schedule_day(orders, sate_and_filler, setup_matrix, monday)

    window = sate_window(ds.lijn1)
    lost = _mins(config.shift_end(monday)) - window[0]
    assert lost <= 120, f"a 30-minute sate run cost L2 {lost} minutes"
    assert len(ds.lijn2) >= 4


def test_the_window_covers_the_changeover_in_front_of_the_sate(
    setup_matrix, monday, sate_and_filler
):
    """The peanut arrives at the line while it is being changed over, not
    when the first pack comes off it."""
    orders = [
        make_order("SATE", 400, monday, order_nr="sate"),
        make_order("PLAIN", 400, monday, order_nr="plain"),
    ]

    ds = schedule_day(orders, sate_and_filler, setup_matrix, monday)

    sate_po = ds.lijn1[-1]
    start, _ = sate_window(ds.lijn1)
    assert start == _mins(sate_po.start) - sate_po.ombouw_voor


# ── what it costs, and how that is reported ──────────────────────────────────

def test_an_order_pushed_out_by_sate_is_reported_as_such(
    setup_matrix, monday, sate_and_filler
):
    """
    Not a CAPACITY_DROP: the day had the minutes. More line time will never
    place this order — only a day without sate on the other line will.
    """
    orders = [make_order("SATE", 1800, monday, order_nr="sate")] + [
        make_order("PLAIN", 400, monday, order_nr=f"p{i}") for i in range(6)
    ]

    ds = schedule_day(orders, sate_and_filler, setup_matrix, monday)

    assert ds.lijn1, "the sate itself must still be planned"
    assert ds.dropped, "a 9-hour sate run has to cost L2 something"
    assert any("SATE_DROP" in w for w in ds.warnings)
    for order, lijn in ds.sate_dropped:
        assert lijn == 2
        assert order in ds.dropped


def test_no_sate_no_change(setup_matrix, monday):
    """
    The rule may not touch a day that runs no sate — every other day in the
    plan has to come out exactly as it did before.
    """
    products = {
        "A": make_product("A", snelheid=200, alleen_l1=True, allergie_niveau=5),
        "B": make_product("B", snelheid=200, alleen_l2=True),
    }
    orders = [make_order("A", 300, monday, order_nr=f"a{i}") for i in range(4)]
    orders += [make_order("B", 300, monday, order_nr=f"b{i}") for i in range(4)]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert ds.sate_dropped == []
    assert ds.lijn1[0].start == config.line_start(1)
    assert ds.lijn2[0].start == config.line_start(2)
    # No idle gap anywhere: level 5 is still an ordinary product.
    for planned in (ds.lijn1, ds.lijn2):
        for prev, nxt in zip(planned, planned[1:]):
            assert _mins(nxt.start) - nxt.ombouw_voor == _mins(prev.eind)


# ── sate on both lines ───────────────────────────────────────────────────────

def test_sate_on_both_lines_runs_side_by_side_and_starts_together(
    setup_matrix, monday
):
    """
    Planner 22/10: with the second Gernal, sate on L1 and L2 can run at the
    same time, and both blocks should start at about the same moment. The
    builder opens both windows on the same minute.
    """
    products = {
        "S1": make_product("S1", allergie_niveau=6, snelheid=200, alleen_l1=True),
        "S2": make_product("S2", allergie_niveau=6, snelheid=200, alleen_l2=True),
        "B1": make_product("B1", snelheid=200, alleen_l1=True),
        "B2": make_product("B2", snelheid=200, alleen_l2=True),
    }
    orders = [
        make_order("B1", 1200, monday, order_nr="b1"),     # L1 busy to 12:00
        make_order("S1", 400, monday, order_nr="s1"),
        make_order("B2", 400, monday, order_nr="b2"),      # L2 done at 08:30
        make_order("S2", 800, monday, order_nr="s2"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert ds.dropped == [], ds.warnings
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []
    assert check_sate_sync(ds.lijn1, ds.lijn2) is None
    w1, w2 = sate_window(ds.lijn1), sate_window(ds.lijn2)
    assert w1[0] == w2[0], f"windows open apart: L1 {w1}, L2 {w2}"
    # As soon as both lines are done with ordinary work, not later.
    assert w1[0] == _mins(ds.lijn1[0].eind)


def test_parallel_sate_opens_late_enough_for_the_longer_block(setup_matrix, monday):
    """Both blocks open together, so the longer one decides how late that can be."""
    products = {
        "S1": make_product("S1", allergie_niveau=6, snelheid=200, alleen_l1=True),
        "S2": make_product("S2", allergie_niveau=6, snelheid=200, alleen_l2=True),
        "B1": make_product("B1", snelheid=200, alleen_l1=True),
    }
    orders = [
        make_order("B1", 2400, monday, order_nr="b1"),     # wants L1 06:00-18:00
        make_order("S1", 200, monday, order_nr="s1"),      # 1 h
        make_order("S2", 1000, monday, order_nr="s2"),     # 5 h
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    w1, w2 = sate_window(ds.lijn1), sate_window(ds.lijn2)
    assert w1[0] == w2[0]
    assert w2[1] <= _mins(config.shift_end(monday))
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []


def test_sate_on_both_lines_queues_when_parallel_is_off(
    setup_matrix, monday, monkeypatch
):
    """
    FRIGI_SATE_PARALLEL=0 — the fallback for a day without the second Gernal:
    the two blocks go one after the other, one closing the shift.
    """
    monkeypatch.setattr("frigilunch.domain.constraints.allergen.SATE_PARALLEL", False)
    monkeypatch.setattr("frigilunch.application.scheduler._SATE_PARALLEL", False)
    products = {
        "S1": make_product("S1", allergie_niveau=6, snelheid=200, alleen_l1=True),
        "S2": make_product("S2", allergie_niveau=6, snelheid=200, alleen_l2=True),
    }
    orders = [
        make_order("S1", 400, monday, order_nr="s1"),
        make_order("S2", 400, monday, order_nr="s2"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert len(ds.lijn1) == 1 and len(ds.lijn2) == 1, ds.warnings
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []
    w1, w2 = sate_window(ds.lijn1), sate_window(ds.lijn2)
    assert w1[0] >= w2[1] or w2[0] >= w1[1], (
        f"the two sate windows overlap: L1 {w1}, L2 {w2}"
    )
    assert max(w1[1], w2[1]) == _mins(config.shift_end(monday))


def test_sate_on_one_line_never_empties_the_other(setup_matrix, monday):
    """
    Regression. The probe used to charge the other line from where the sate's
    window happened to open to closing time; with nothing in front of the sate
    that is 06:30, and L1 lost all six orders while the sate ran 20:30-22:30.
    """
    products = {
        "SATE": make_product("SATE", allergie_niveau=6, snelheid=200, alleen_l2=True),
        "PLAIN": make_product("PLAIN", snelheid=200, alleen_l1=True),
    }
    orders = [make_order("SATE", 400, monday, order_nr="sate")] + [
        make_order("PLAIN", 300, monday, order_nr=f"p{i}") for i in range(6)
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert len(ds.lijn1) == 6, [o.order_nr for o in ds.dropped]
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []


def test_two_sate_orders_on_one_line_stay_one_block(setup_matrix, monday):
    products = {
        "SATE": make_product("SATE", allergie_niveau=6, snelheid=200, alleen_l1=True),
        "PLAIN": make_product("PLAIN", snelheid=200, alleen_l2=True),
    }
    orders = [
        make_order("SATE", 300, monday, order_nr="s1"),
        make_order("SATE", 300, monday, order_nr="s2"),
        make_order("PLAIN", 300, monday, order_nr="p1"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert len(ds.lijn1) == 2
    # Back to back, and the window covers both.
    assert _mins(ds.lijn1[1].start) - ds.lijn1[1].ombouw_voor == _mins(ds.lijn1[0].eind)
    start, eind = sate_window(ds.lijn1)
    assert eind == _mins(ds.lijn1[-1].eind)
    assert start <= _mins(ds.lijn1[0].start)
    # Handed over: the block opens when L2 is done, not at closing time.
    assert start == max(_mins(po.eind) for po in ds.lijn2)


# ── the backstop ─────────────────────────────────────────────────────────────

def test_the_validator_catches_an_overlap_the_builder_would_never_make(
    setup_matrix, monday, sate_and_filler
):
    """
    `check_sate_conflict` exists because "the builder never does this" is not
    a guarantee anyone can eat. Hand it a day the builder would not produce.
    """
    from dataclasses import replace

    from frigilunch.domain.models import PlannedOrder

    sate = PlannedOrder(
        order=make_order("SATE", 400, monday),
        product=sate_and_filler["SATE"],
        lijn=1, start=time(18, 0), eind=time(22, 30), ombouw_voor=30,
    )
    plain = PlannedOrder(
        order=make_order("PLAIN", 400, monday),
        product=sate_and_filler["PLAIN"],
        lijn=2, start=time(16, 0), eind=time(20, 0), ombouw_voor=0,
    )

    assert check_sate_conflict([sate], [plain])
    # Sate beside sate is allowed since the second Gernal (22/10)...
    other_sate = replace(sate, lijn=2, eind=time(22, 0))
    assert check_sate_conflict([sate], [other_sate]) == []
    # ...but nothing may run after the sate on its own line.
    early_sate = replace(sate, start=time(14, 0), eind=time(15, 40))
    after = PlannedOrder(
        order=make_order("PLAIN", 100, monday),
        product=sate_and_filler["PLAIN"],
        lijn=1, start=time(16, 0), eind=time(17, 0), ombouw_voor=20,
    )
    assert check_sate_conflict([early_sate, after], [])


# ── sate closes the day, whatever route the sequence came in by ──────────────

def test_sate_never_runs_before_an_ordinary_product(setup_matrix, monday):
    """
    The failure this was found by: the CP-SAT day sequencer put a nine-hour
    sate run first thing in the morning — legal on its own line, A1 paid the
    cleaning on the way back down — and shut the other line down from 06:30.
    Sate closes the day, so there is nothing behind it to shut down for.
    """
    products = {
        "SATE": make_product("SATE", allergie_niveau=6, snelheid=400, alleen_l1=True),
        "LOW": make_product("LOW", allergie_niveau=1, snelheid=400, alleen_l1=True),
    }
    orders = [
        make_order("SATE", 400, monday, order_nr="sate"),
        make_order("LOW", 400, monday, order_nr="low"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert [po.order.order_nr for po in ds.lijn1] == ["low", "sate"]


def test_sate_keeps_the_end_of_the_day_from_a_must_close_product(
    setup_matrix, monday
):
    """
    Planner 22/10: after sate nothing else can run on the line. So a product
    the sheet says MUST close the day cannot share a line-day with sate — the
    assignment model keeps them apart, and a day that arrives anyway loses the
    closer, not the sate.
    """
    products = {
        "SATE": make_product("SATE", allergie_niveau=6, snelheid=400, alleen_l1=True),
        "CLOSER": make_product(
            "CLOSER", allergie_niveau=1, snelheid=400, alleen_l1=True, afsluit_moet=True
        ),
    }
    orders = [
        make_order("SATE", 300, monday, order_nr="sate"),
        make_order("CLOSER", 300, monday, order_nr="closer"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert [po.order.order_nr for po in ds.lijn1] == ["sate"]
    assert [o.order_nr for o in ds.dropped] == ["closer"]
    assert check_sate_conflict(ds.lijn1, ds.lijn2) == []


def test_two_different_sate_products_close_the_day_together(setup_matrix, monday):
    """
    Two sate articles that both "must close" are one closing block: after
    sate the line runs only more sate, which breaks neither requirement.
    """
    products = {
        "SA": make_product("SA", allergie_niveau=6, snelheid=400, alleen_l1=True,
                           afsluit_moet=True, opstart_kan_niet=True),
        "SB": make_product("SB", allergie_niveau=6, snelheid=400, alleen_l1=True,
                           afsluit_moet=True, opstart_kan_niet=True),
        "X": make_product("X", allergie_niveau=1, snelheid=400, alleen_l1=True),
    }
    orders = [
        make_order("SA", 300, monday, order_nr="sa"),
        make_order("SB", 300, monday, order_nr="sb"),
        make_order("X", 300, monday, order_nr="x"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday, force_optimal_sequence=True)

    assert ds.dropped == []
    assert ds.lijn1[0].order.order_nr == "x"
    assert {po.order.order_nr for po in ds.lijn1[1:]} == {"sa", "sb"}


def test_the_walker_reorders_a_sate_first_sequence_it_is_handed(setup_matrix, monday):
    """
    `sate_last` sits inside the walker, not in the sort, so a proposal from
    any source — the fixed sort, the CP-SAT sequencer, a position repair —
    lands the same way round.
    """
    from frigilunch.application.scheduler import _schedule_lijn

    sate = make_product("SATE", allergie_niveau=6, snelheid=400)
    low = make_product("LOW", allergie_niveau=1, snelheid=400)
    handed = [
        (make_order("SATE", 300, monday, order_nr="sate"), sate),
        (make_order("LOW", 300, monday, order_nr="low"), low),
    ]

    planned = _schedule_lijn(handed, 1, monday, setup_matrix, config.shift_end(monday))

    assert [po.order.order_nr for po in planned] == ["low", "sate"]


# ── A6: what the assignment model reserves ───────────────────────────────────

def test_the_model_never_puts_sate_beside_a_product_that_must_close(setup_matrix):
    """
    Sate closes its line (22/10), so a line-day carrying it has no last slot
    left for a product the sheet says must close. Stated on the set: plenty of
    room, both L1-only, still never the same day.
    """
    from datetime import date, timedelta

    from frigilunch.application.date_optimizer import assign_production_dates_feedback

    start = date(2026, 7, 27)
    lever = start + timedelta(days=9)
    products = {
        "SATE": make_product("SATE", allergie_niveau=6, snelheid=1000, alleen_l1=True),
        "CLOSER": make_product(
            "CLOSER", snelheid=1000, alleen_l1=True, afsluit_moet=True
        ),
    }
    orders = [
        make_order("SATE", 2000, start, lever=lever, order_nr="sate"),
        make_order("CLOSER", 2000, start, lever=lever, order_nr="closer"),
    ]

    scheduled, rejected, _ = assign_production_dates_feedback(
        orders, products, start, weeks=2, setup_matrix=setup_matrix, toestellen={},
    )

    assert rejected == []
    days = {o.order_nr: o.productie_datum for o in scheduled}
    assert days["sate"] != days["closer"]


def test_sate_on_both_lines_may_share_a_day_in_the_model(setup_matrix):
    """
    Side by side, each line's sate only has to fit its own line: two long
    sate runs, one per line, on the one day both can run.
    """
    from datetime import date, timedelta

    from frigilunch.application.date_optimizer import assign_production_dates_feedback

    start = date(2026, 7, 27)
    products = {
        "S1": make_product("S1", allergie_niveau=6, snelheid=1000, alleen_l1=True, dagen=["ma"]),
        "S2": make_product("S2", allergie_niveau=6, snelheid=1000, alleen_l2=True, dagen=["ma"]),
    }
    # 9 hours each: queued, the two would need 18 hours on one day.
    orders = [
        make_order("S1", 9000, start, lever=start, order_nr="s1"),
        make_order("S2", 9000, start, lever=start, order_nr="s2"),
    ]

    scheduled, rejected, _ = assign_production_dates_feedback(
        orders, products, start, weeks=1, setup_matrix=setup_matrix, toestellen={},
    )

    assert rejected == [], [r for r in rejected]
    assert {o.productie_datum for o in scheduled} == {start}


def test_a_reserved_sate_block_never_makes_the_model_infeasible(setup_matrix):
    """
    Regression. A5 reserves the other line's minutes in the assignment model,
    and an earlier phase's sate is subtracted as a constant. Where that
    constant was bigger than what the line had left, the constraint read
    `sum(non-negative terms) <= negative` — not "this line-day is full" but
    UNSATISFIABLE, and one such day takes the whole phase down with it.

    Measured before the clamp: phase 2 came back INFEASIBLE and 0 of 168
    flexible orders were scheduled — 119 orders lost to a day nobody was even
    trying to use.
    """
    from datetime import date, timedelta

    from frigilunch.application.date_optimizer import assign_production_dates_feedback

    start = date(2026, 7, 27)                 # a Monday
    lever = start + timedelta(days=9)
    products = {
        "SATE": make_product("SATE", allergie_niveau=6, snelheid=200),
        "PLAIN": make_product("PLAIN", snelheid=1000),
    }
    # 25 hours of sate on L2 that Monday — more than L1's whole 990-minute
    # shift, which is what drives the subtraction below zero. (An impossible
    # day on purpose: the point is that a nonsense number in the reserved set
    # must not take every other day down with it.)
    hogging = make_order("SATE", 5000, start, lever=lever, order_nr="HOG", lijn=2)
    free = [
        make_order("PLAIN", 2000, start, lever=lever, order_nr=f"f{i}")
        for i in range(3)
    ]

    scheduled, rejected, _ = assign_production_dates_feedback(
        free, products, start, weeks=2, setup_matrix=setup_matrix, toestellen={},
        _reserved_capacity={(start, 2): 900},
        _reserved_orders={(start, 2): [hogging]},
    )

    assert scheduled, "the model went infeasible: nothing was placed at all"
    assert len(scheduled) == len(free), (
        f"{len(rejected)} order(s) rejected; other days in the window are free"
    )
    for o in scheduled:
        assert not (o.productie_datum == start and o.lijn == 1), (
            "L1 on the sate day was blocked out, so nothing may be booked there"
        )
