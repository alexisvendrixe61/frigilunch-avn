"""
G2 on L1 — the automatic packing line's kapvorm rule outranks every other
preference (planner, 22/10): "producties met dezelfde kapvorm zo veel
mogelijk aansluitend — deze regel krijgt voorrang op alle andere instellingen".
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason="depends on unported src module")

from datetime import date, time, timedelta

from src.campaign_regroup import regroup_campaigns
from frigilunch.application.day_quality import line_score
from frigilunch.application.day_sequencer import IDENTIEK_BONUS, L1_KAPVORM_BONUS, kapvorm_bonus
from src.layout_metrics import l1_kapvorm_wissels
from frigilunch.domain.models import PlannedOrder
from tests.conftest import make_order, make_product

MONDAY = date(2026, 9, 28)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)


def _po(product, start, eind, ombouw=0, lijn=1):
    return PlannedOrder(
        order=make_order(product.art_nr, 100, MONDAY), product=product,
        lijn=lijn, start=start, eind=eind, ombouw_voor=ombouw,
    )


# ── the measure ──────────────────────────────────────────────────────────────

def test_l1_changes_count_day_seams_and_nvt():
    """NVT -> H25 is a changeover on L1 too, and so is a change overnight."""
    h22 = make_product("A", kapvorm="H22")
    h25 = make_product("B", kapvorm="H25")
    nvt = make_product("C", kapvorm="NVT")
    monday = [_po(h22, time(6, 0), time(8, 0)), _po(h25, time(8, 30), time(10, 0))]
    tuesday = [_po(nvt, time(6, 0), time(8, 0)), _po(h25, time(8, 30), time(10, 0))]

    assert l1_kapvorm_wissels([monday, tuesday]) == 3      # H22>H25 | >NVT >H25
    assert l1_kapvorm_wissels([tuesday], vorige=h25) == 2  # H25 | >NVT >H25


# ── the sequencer ────────────────────────────────────────────────────────────

def test_one_l1_kapvorm_arc_outweighs_every_other_day_shape_term():
    """An identical pair plus a whole shift of idle minutes is still less."""
    assert L1_KAPVORM_BONUS > IDENTIEK_BONUS + 990
    assert kapvorm_bonus(1) == L1_KAPVORM_BONUS
    assert kapvorm_bonus(2) < kapvorm_bonus(1)


def test_on_l1_a_grouped_day_scores_higher_even_with_more_changeover():
    """
    Same three orders: H22 H25 H22 with short changeovers against H22 H22 H25
    with an hour more of cleaning. L1 takes the grouped day.
    """
    a, b, c = (make_product(n, kapvorm=k) for n, k in (("A", "H22"), ("B", "H25"), ("C", "H22")))
    broken = [
        _po(a, time(6, 0), time(8, 0)),
        _po(b, time(8, 20), time(10, 0), 20),
        _po(c, time(10, 20), time(12, 0), 20),
    ]
    grouped = [
        _po(a, time(6, 0), time(8, 0)),
        _po(c, time(8, 50), time(10, 30), 50),
        _po(b, time(11, 20), time(13, 0), 50),
    ]
    assert line_score(grouped, 1) > line_score(broken, 1)


# ── the regroup pass ─────────────────────────────────────────────────────────

def test_regroup_reports_and_reduces_l1_kapvorm_changes(setup_matrix):
    """
    H25 on Monday, H22 on Tuesday, H25 again on Wednesday: moving the
    Wednesday H25 order onto Monday removes two changes on L1.
    """
    products = {
        "M": make_product("M", kapvorm="H25", alleen_l1=True, dagen=["ma"]),
        "T": make_product("T", kapvorm="H22", alleen_l1=True, dagen=["di"]),
        "W": make_product("W", kapvorm="H25", alleen_l1=True, dagen=["ma", "wo"]),
        "V": make_product("V", kapvorm="H31", alleen_l1=True, dagen=["wo"]),
    }
    orders = [
        make_order("M", 500, MONDAY, lever=MONDAY, order_nr="m"),
        make_order("T", 500, TUESDAY, lever=TUESDAY, order_nr="t"),
        make_order("V", 500, WEDNESDAY, lever=WEDNESDAY, order_nr="v"),
        make_order("W", 500, WEDNESDAY, lever=WEDNESDAY, order_nr="w"),
    ]
    for o in orders:
        o.lijn = 1

    report = regroup_campaigns(
        orders, products, setup_matrix, {}, MONDAY, MONDAY + timedelta(weeks=1),
    )

    w = next(o for o in orders if o.order_nr == "w")
    assert w.productie_datum == MONDAY, report
    assert report["l1_kapvorm_after"] < report["l1_kapvorm_before"]


# ── the assignment model ─────────────────────────────────────────────────────

def test_an_l1_preference_is_not_lost_to_the_kapvorm_premium(setup_matrix):
    """
    The premium prices kapvorm CHANGES on L1, not running L1 at all: an order
    that prefers L1 and is the only one there still goes to L1.
    """
    from dataclasses import replace

    from frigilunch.application.date_optimizer import assign_production_dates_feedback

    start = date(2026, 7, 27)
    lever = start + timedelta(days=9)
    products = {
        "P1": replace(make_product("P1", kapvorm="H22", snelheid=1000), prio_l1=True, prio_l2=False),
    }
    orders = [make_order("P1", 2000, start, lever=lever, order_nr="p1")]

    scheduled, rejected, _ = assign_production_dates_feedback(
        orders, products, start, weeks=2, setup_matrix=setup_matrix, toestellen={},
    )

    assert rejected == []
    assert scheduled[0].lijn == 1
