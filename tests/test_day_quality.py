"""
O6, G1, G2 in the built day — no idle holes, groups back to back.

The 2026-09-28 plan ran an L2 gernal order at 21:50 because L1 held the gernal
from 10:30 and the sequencer only counted changeover minutes: waiting until
the evening cost it nothing. These tests pin that the day's shape is scored.
"""
from __future__ import annotations

from datetime import time

from frigilunch.application.day_quality import adjacency_counts, idle_minutes, line_score
from frigilunch.application.day_sequencer import solve_optimal_sequence
from frigilunch.domain.models import PlannedOrder
from tests.conftest import make_order, make_product


def _po(product, aantal, start, eind, ombouw, monday, lijn=2):
    return PlannedOrder(
        order=make_order(product.art_nr, aantal, monday), product=product, lijn=lijn,
        start=start, eind=eind, ombouw_voor=ombouw,
    )


def test_idle_minutes_counts_the_hole_and_not_the_changeover(monday):
    a, b = make_product("A"), make_product("B")
    planned = [
        _po(a, 100, time(6, 30), time(7, 30), 0, monday),
        # 20 min changeover from 08:10, so the line idles 07:30-08:10 = 40 min
        _po(b, 100, time(8, 30), time(9, 0), 20, monday),
    ]
    assert idle_minutes(planned, 2) == 40


def test_a_late_line_start_is_idle_too(monday):
    planned = [_po(make_product("A"), 100, time(8, 40), time(9, 0), 0, monday)]
    assert idle_minutes(planned, 2) == 130


def test_a_blocked_order_runs_before_the_block_not_after_it(setup_matrix, monday):
    """The 2026-09-28 case: run the gernal order before L1 takes the gernal."""
    low = make_product("LOW", allergie_niveau=0, alleen_l2=True)
    mid = make_product("MID", allergie_niveau=1, alleen_l2=True)
    gernal = make_product("GER", allergie_niveau=5, alleen_l2=True, devices=["gernal"])
    # LOW and MID take two hours each: in allergen order the gernal order would
    # only get its turn after L1's block, at 21:50.
    seq = [
        (make_order(p.art_nr, n, monday, order_nr=p.art_nr), p)
        for p, n in ((low, 2000), (mid, 2000), (gernal, 500))
    ]
    proposal = solve_optimal_sequence(
        seq, setup_matrix, start_time_min=6 * 60 + 30, max_end_min=22 * 60 + 30,
        lijn_nr=2, blocking={"gernal": [(10 * 60 + 30, 21 * 60 + 50)]},
        toestellen_avail={"gernal": 1},
    )
    assert proposal is not None
    # Before the block means anywhere but last: GER-LOW-MID and MID-GER-LOW
    # both finish the gernal by 10:30 with the same 50 changeover minutes and
    # no idle time, so which of the two comes back is a tie-break.
    assert [p.art_nr for _, p in proposal][-1] != "GER"


def test_the_sequencer_puts_identical_products_back_to_back(setup_matrix, monday):
    big = make_product("BIG", identiek_groep="G", kapvorm="H22", alleen_l1=True)
    small = make_product("SMALL", identiek_groep="G", kapvorm="H44", alleen_l1=True)
    other = make_product("OTHER", kapvorm="H44", alleen_l1=True)
    seq = [
        (make_order("BIG", 3000, monday, order_nr="big"), big),
        (make_order("OTHER", 1000, monday, order_nr="other"), other),
        (make_order("SMALL", 1000, monday, order_nr="small"), small),
    ]
    proposal = solve_optimal_sequence(
        seq, setup_matrix, start_time_min=6 * 60, max_end_min=22 * 60 + 30, lijn_nr=1,
    )
    names = [o.order_nr for o, _ in proposal]
    assert names.index("big") == names.index("small") + 1


def test_line_score_prefers_the_grouped_build(monday):
    p = make_product("P", identiek_groep="G")
    q = make_product("Q", identiek_groep="G")
    r = make_product("R", kapvorm="NVT")
    grouped = [
        _po(p, 100, time(6, 30), time(7, 0), 0, monday),
        _po(q, 200, time(7, 20), time(7, 50), 20, monday),
        _po(r, 100, time(8, 20), time(8, 50), 30, monday),
    ]
    split = [
        _po(p, 100, time(6, 30), time(7, 0), 0, monday),
        _po(r, 100, time(7, 20), time(7, 50), 20, monday),
        _po(q, 200, time(8, 10), time(8, 40), 20, monday),
    ]
    assert adjacency_counts(grouped)[0] == 1
    assert line_score(grouped, 2) > line_score(split, 2)


def test_the_day_opens_on_the_kapvorm_the_line_closed_on_yesterday(setup_matrix, monday):
    """G2 carry: two otherwise identical orders, yesterday ended on H22."""
    h44 = make_product("H44P", kapvorm="H44")
    h22 = make_product("H22P", kapvorm="H22")
    seq = [
        (make_order(p.art_nr, 1000, monday, order_nr=p.art_nr), p)
        for p in (h44, h22)
    ]
    for vorige, expected in ((make_product("Y22", kapvorm="H22"), "H22P"),
                             (make_product("Y44", kapvorm="H44"), "H44P")):
        proposal = solve_optimal_sequence(
            seq, setup_matrix, start_time_min=6 * 60, max_end_min=22 * 60,
            lijn_nr=1, vorige=vorige,
        )
        assert proposal is not None
        assert proposal[0][1].art_nr == expected


def test_the_day_closes_on_a_kapvorm_the_line_runs_tomorrow(setup_matrix, monday):
    """G2 look-ahead: two otherwise identical orders, tomorrow runs H44."""
    from frigilunch.domain.constraints.grouping_rules import volgende_van
    from frigilunch.domain.constraints.sequencing import sort_orders_for_lijn

    h44 = make_product("H44P", kapvorm="H44")
    h22 = make_product("H22P", kapvorm="H22")
    seq = [
        (make_order(p.art_nr, 1000, monday, order_nr=p.art_nr), p)
        for p in (h44, h22)
    ]
    for tomorrow, expected in (("H44", "H44P"), ("H22", "H22P")):
        volgende = volgende_van([make_product("T", kapvorm=tomorrow)])
        proposal = solve_optimal_sequence(
            seq, setup_matrix, start_time_min=6 * 60, max_end_min=22 * 60,
            lijn_nr=1, volgende=volgende,
        )
        assert proposal is not None
        assert proposal[-1][1].art_nr == expected
        assert sort_orders_for_lijn(seq, volgende=volgende)[-1][1].art_nr == expected
