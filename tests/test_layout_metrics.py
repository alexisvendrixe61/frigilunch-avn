"""How the plan looks to the floor — idle holes, unused shift end, grouping."""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason="depends on unported src module")

from datetime import time

from src.layout_metrics import line_day_metrics, plan_layout_summary
from frigilunch.domain.models import DaySchedule, PlannedOrder
from tests.conftest import make_order, make_product


def _po(product, aantal, start, eind, ombouw, day, lijn=2):
    return PlannedOrder(
        order=make_order(product.art_nr, aantal, day), product=product, lijn=lijn,
        start=start, eind=eind, ombouw_voor=ombouw,
    )


def test_line_day_metrics_measures_holes_and_the_unused_end(monday):
    a, b = make_product("A"), make_product("B")
    planned = [
        _po(a, 100, time(6, 30), time(8, 0), 0, monday),
        _po(b, 100, time(10, 20), time(12, 0), 20, monday),  # idle 08:00-10:00
    ]
    m = line_day_metrics(planned, monday, 2)
    assert m["idle_min"] == 120
    assert m["end_gap_min"] == 22 * 60 + 30 - 12 * 60


def test_summary_counts_split_groups_and_kapvorm_breaks(monday):
    tuesday = monday.replace(day=monday.day + 1)
    twin_a = make_product("TA", identiek_groep="T", kapvorm="NVT")
    twin_b = make_product("TB", identiek_groep="T", kapvorm="NVT")
    h22, h25, h22b = (make_product(n, kapvorm=k) for n, k in (("X", "H22"), ("Y", "H25"), ("Z", "H22")))
    days = [
        DaySchedule(datum=monday, lijn1=[
            _po(h22, 100, time(6, 0), time(7, 0), 0, monday, 1),
            _po(h25, 100, time(7, 20), time(8, 0), 20, monday, 1),
            _po(h22b, 100, time(8, 20), time(9, 0), 20, monday, 1),
        ], lijn2=[_po(twin_a, 100, time(6, 30), time(7, 0), 0, monday)]),
        DaySchedule(datum=tuesday, lijn2=[_po(twin_b, 100, time(6, 30), time(7, 0), 0, tuesday)]),
    ]
    s = plan_layout_summary(days)
    assert s["identiek_groups_split"] == 1
    assert s["kapvorm_breaks"] == 1
    assert s["active_line_days"] == 3
    assert plan_layout_summary(days, start=tuesday)["active_line_days"] == 1
