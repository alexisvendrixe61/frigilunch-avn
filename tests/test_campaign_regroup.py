"""
G2 / G3 over days — the local search that runs on the finished plan.
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason="depends on unported src module")

from datetime import date, timedelta

from src.campaign_regroup import regroup_campaigns
from tests.conftest import make_order, make_product

MONDAY = date(2026, 9, 28)
TUESDAY = MONDAY + timedelta(days=1)
THURSDAY = MONDAY + timedelta(days=3)


def _scenario():
    products = {
        "F1": make_product("F1", kapvorm="H22", alleen_l1=True, dagen=["ma"]),
        "F2": make_product("F2", kapvorm="K2", alleen_l1=True, dagen=["di"]),
        "F3": make_product("F3", kapvorm="K3", alleen_l1=True, dagen=["do"]),
        "K": make_product("K", kapvorm="H22", alleen_l1=True, dagen=["di", "do"]),
    }
    orders = [
        make_order("F1", 500, MONDAY, lever=MONDAY, order_nr="f1"),
        make_order("F2", 500, TUESDAY, lever=TUESDAY, order_nr="f2"),
        make_order("F3", 500, THURSDAY, lever=THURSDAY, order_nr="f3"),
        make_order("K", 500, THURSDAY, lever=THURSDAY, order_nr="k"),
    ]
    for o in orders:
        o.lijn = 1
    return products, orders


def test_an_order_moves_to_continue_yesterdays_kapvorm(setup_matrix):
    products, orders = _scenario()

    report = regroup_campaigns(
        orders, products, setup_matrix, {}, MONDAY, MONDAY + timedelta(weeks=1),
    )

    k = next(o for o in orders if o.order_nr == "k")
    assert k.productie_datum == TUESDAY
    assert report["moves"] >= 1
    assert report["score_after"] > report["score_before"]


def test_nothing_moves_past_its_delivery_date(setup_matrix):
    products, orders = _scenario()
    k = next(o for o in orders if o.order_nr == "k")
    k.lever_datum = MONDAY  # Tuesday is now too late

    regroup_campaigns(orders, products, setup_matrix, {}, MONDAY, MONDAY + timedelta(weeks=1))

    assert k.productie_datum == THURSDAY


def test_a_full_host_day_takes_the_order_by_swapping_one_back(setup_matrix):
    """
    Monday runs H22 and Thursday H44, but each holds one order of the other's
    kapvorm (X on Monday, K on Thursday), and both lines are too full to take
    an extra order. No single move fits; swapping K and X groups both days.
    Tuesday runs H50, so neither day borrows a seam from the other.
    """
    big = 13_000  # snelheid 1000/h: 780 of the 990 window minutes
    products = {
        "F1": make_product("F1", kapvorm="H22", alleen_l1=True, dagen=["ma"]),
        "G": make_product("G", kapvorm="H50", alleen_l1=True, dagen=["di"]),
        "F3": make_product("F3", kapvorm="H44", alleen_l1=True, dagen=["do"]),
        "X": make_product("X", kapvorm="H44", alleen_l1=True, dagen=["ma", "do"]),
        "K": make_product("K", kapvorm="H22", alleen_l1=True, dagen=["ma", "do"]),
    }
    orders = [
        make_order("F1", big, MONDAY, lever=MONDAY, order_nr="f1"),
        make_order("X", 2500, MONDAY, lever=THURSDAY, order_nr="x"),
        make_order("G", 500, TUESDAY, lever=TUESDAY, order_nr="g"),
        make_order("F3", big, THURSDAY, lever=THURSDAY, order_nr="f3"),
        make_order("K", 2500, THURSDAY, lever=THURSDAY, order_nr="k"),
    ]
    for o in orders:
        o.lijn = 1

    report = regroup_campaigns(
        orders, products, setup_matrix, {}, MONDAY, MONDAY + timedelta(weeks=1),
    )

    days = {o.order_nr: o.productie_datum for o in orders}
    assert days["k"] == MONDAY and days["x"] == THURSDAY, (days, report)
    assert report["swaps"] == 1 and report["moves"] == 0
