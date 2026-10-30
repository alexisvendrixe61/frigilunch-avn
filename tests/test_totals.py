"""
The accounting invariant: every order that goes in comes out exactly once,
either scheduled or rejected.

This did not hold. A real wk31 payload reported orders_in_window=174,
scheduled=166, rejected=6 — 172, with two orders in no bucket at all, because
three components counted three different things (input rows / unique
identities / chunk rows) and were then subtracted from each other.
"""
from __future__ import annotations

from datetime import date, timedelta

from frigilunch.application import totals
from tests.conftest import make_order

D1 = date(2026, 7, 27)
D2 = D1 + timedelta(days=1)
LEVER = D1 + timedelta(days=7)


def test_everything_in_comes_out_exactly_once():
    orders = [make_order(f"A{i}", 100, D1, lever=LEVER, order_nr=f"o{i}") for i in range(5)]
    t = totals.compute(orders, orders[:3], orders[3:])

    assert t.orders_in == 5
    assert t.scheduled_orders == 3
    assert t.rejected_orders == 2
    assert t.scheduled_orders + t.rejected_orders == t.orders_in
    assert t.balances and not totals.report_imbalance(t)


def test_a_split_order_is_one_order_and_several_production_runs():
    """The Excel used to count chunk rows and the dashboard unique identities,
    so the same plan reported two different order counts."""
    chunks = [
        make_order("A", 60, D1, lever=LEVER, order_nr="split"),
        make_order("A", 40, D2, lever=LEVER, order_nr="split"),
    ]
    t = totals.compute(chunks, chunks, [])

    assert t.orders_in == 1
    assert t.scheduled_orders == 1        # one order, one customer commitment
    assert t.scheduled_chunks == 2        # two runs on the floor
    assert t.orders_split == 1
    assert t.scheduled_units == 100       # quantity is conserved
    assert t.balances


def test_a_vanished_order_is_reported_not_swallowed():
    orders = [make_order(f"A{i}", 100, D1, lever=LEVER, order_nr=f"o{i}") for i in range(3)]
    t = totals.compute(orders, orders[:1], [])       # o1 and o2 went nowhere

    assert not t.balances
    assert len(t.unaccounted) == 2
    msgs = totals.report_imbalance(t)
    assert msgs and "neither scheduled nor rejected" in msgs[0]


def test_an_order_in_both_buckets_is_reported():
    o = make_order("A", 100, D1, lever=LEVER, order_nr="dup")
    t = totals.compute([o], [o], [o])

    assert not t.balances
    assert t.double_counted == [totals.order_identity(o)]
    assert any("BOTH scheduled and rejected" in m for m in totals.report_imbalance(t))


def test_percentages_are_over_orders_not_rows():
    chunks = [make_order("A", 60, D1, lever=LEVER, order_nr="split"),
              make_order("A", 40, D2, lever=LEVER, order_nr="split")]
    rejected = [make_order("B", 100, D1, lever=LEVER, order_nr="b")]
    t = totals.compute(chunks + rejected, chunks, rejected)

    assert t.scheduled_orders == 1 and t.rejected_orders == 1
    assert t.fulfillment_pct == 50.0          # not 66.7 from 2 chunks of 3 rows
    assert t.units_pct == 50.0


def test_empty_plan_does_not_divide_by_zero():
    t = totals.compute([], [], [])
    assert t.fulfillment_pct == 0.0 and t.units_pct == 0.0 and t.balances
