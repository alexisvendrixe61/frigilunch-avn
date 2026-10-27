"""
L3 — the SOFT half of the line rules: "producten prio L1 enkel op L2 inplannen
indien geen plaats op L1" (meeting 04/09, applied in both directions since the
product database states the preference both ways).

The preference was in the data and in `resolve_lijn` long before it was in the
solver, but `resolve_lijn` honours it only at step 3 — after "use the line the
optimizer picked" — and the optimizer picks a line for everything it places.
So the preference never decided anything: two week-36 runs without the term put
55 of 90 and 48 of 94 line-flexible placements on the non-preferred line, most
of them on days where the preferred line still had room.

Two things have to hold at once, and they pull in opposite directions:

  * with room on the preferred line, the preference WINS — including over the
    consolidation penalties, which were previously the only thing deciding;
  * with no room on the preferred line, the order still gets PLACED on the
    other one. The preference must never cost an order.

The solver tests run the real CP-SAT solver on 2 orders, so they finish
instantly.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from src.constraints.line_rules import preferred_line
from src.scheduler_ortools_v3 import assign_production_dates_feedback
from tests.conftest import make_order, make_product

START = date(2026, 7, 27)          # a Monday
LEVER = START + timedelta(days=9)


def prefers(art_nr: str, lijn: int, **kwargs):
    """A line-flexible product that prefers one line. `make_product` marks both
    prio flags for a flexible product, which is "no preference stated"."""
    product = make_product(art_nr, **kwargs)
    return replace(product, prio_l1=(lijn == 1), prio_l2=(lijn == 2))


def _solve(orders, products, setup_matrix):
    return assign_production_dates_feedback(
        orders, products, START, weeks=2,
        setup_matrix=setup_matrix, toestellen={},
    )


# ── preferred_line ───────────────────────────────────────────────────────────

def test_one_prio_flag_is_a_preference():
    assert preferred_line(prefers("A", 1)) == 1
    assert preferred_line(prefers("B", 2)) == 2


def test_both_prio_flags_is_no_preference():
    """4 real products carry both. Two preferences are none."""
    product = make_product("BOTH")
    assert (product.prio_l1, product.prio_l2) == (True, True)
    assert preferred_line(product) is None


def test_no_prio_flag_is_no_preference():
    product = replace(make_product("NONE"), prio_l1=False, prio_l2=False)
    assert preferred_line(product) is None


def test_a_line_locked_product_has_no_preference_to_express():
    """`permitted_lines` already decides it — a preference term would only add
    noise to the objective."""
    assert preferred_line(make_product("ONLY1", alleen_l1=True)) is None
    assert preferred_line(make_product("ONLY2", alleen_l2=True)) is None
    assert preferred_line(prefers("EXCL", 1, l1_uitgesloten=True)) is None


# ── the solver honours it when there is room ─────────────────────────────────

def test_each_order_lands_on_its_preferred_line(setup_matrix):
    products = {
        "WANTS1": prefers("WANTS1", 1, snelheid=1000),
        "WANTS2": prefers("WANTS2", 2, snelheid=1000),
    }
    orders = [
        make_order("WANTS1", 2000, START, lever=LEVER, order_nr="WANTS1"),
        make_order("WANTS2", 2000, START, lever=LEVER, order_nr="WANTS2"),
    ]

    scheduled, rejected, _ = _solve(orders, products, setup_matrix)

    assert rejected == []
    lijnen = {o.order_nr: o.lijn for o in scheduled}
    assert lijnen == {"WANTS1": 1, "WANTS2": 2}


def test_preference_outranks_line_consolidation(setup_matrix):
    """
    Two tiny orders with opposite preferences on a day with room for both.
    Consolidation (5 per activated line-day) would put them on ONE line; the
    preference (50) has to win, or the term is priced below the tie-break it
    exists to overrule.
    """
    products = {
        "P1": prefers("P1", 1, snelheid=1000),
        "P2": prefers("P2", 2, snelheid=1000),
    }
    orders = [
        make_order("P1", 500, START, lever=LEVER, order_nr="P1"),
        make_order("P2", 500, START, lever=LEVER, order_nr="P2"),
    ]

    scheduled, rejected, _ = _solve(orders, products, setup_matrix)

    assert rejected == []
    assert {o.lijn for o in scheduled} == {1, 2}


# ── but it never costs an order ──────────────────────────────────────────────

def test_the_overflow_goes_to_the_other_line_rather_than_being_dropped(setup_matrix):
    """
    Both orders prefer L1, both must run on the one day their window allows,
    and together they are far past L1's 990 minutes. One has to spill onto L2 —
    "enkel op L2 indien geen plaats op L1" is this case, and a rejection here
    would mean the preference is being priced as a constraint.
    """
    products = {
        "BIG1": prefers("BIG1", 1, snelheid=1000),
        "BIG2": prefers("BIG2", 1, snelheid=1000),
    }
    # 600 production minutes each; 2 x (600 + 60 proxy buffer) > 990.
    orders = [
        make_order("BIG1", 10_000, START, lever=START, order_nr="BIG1"),
        make_order("BIG2", 10_000, START, lever=START, order_nr="BIG2"),
    ]

    scheduled, rejected, _ = _solve(orders, products, setup_matrix)

    assert rejected == [], "the preference must never cost an order"
    assert len(scheduled) == 2
    assert {o.lijn for o in scheduled} == {1, 2}, (
        "one of the two has to spill onto L2 — they cannot share L1"
    )
