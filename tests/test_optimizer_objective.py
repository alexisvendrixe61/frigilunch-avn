from __future__ import annotations

from datetime import date, timedelta

from src.scheduler_ortools_v3 import assign_production_dates_feedback
from tests.conftest import make_order, make_product


MONDAY = date(2026, 9, 28)


def test_setup_efficiency_precedes_delivery_proximity(setup_matrix):
    products = {
        "A": make_product("A", snelheid=100, alleen_l1=True),
        "B": make_product("B", snelheid=100, alleen_l1=True),
    }
    products["A"].schaal = "UK-59"
    products["B"].schaal = "UK-57"  # same setup-format family
    orders = [
        make_order("A", 600, MONDAY, lever=MONDAY, order_nr="due-monday"),
        make_order(
            "B", 600, MONDAY, lever=MONDAY + timedelta(days=4), order_nr="due-friday"
        ),
    ]

    outcome = assign_production_dates_feedback(
        orders,
        products,
        MONDAY,
        weeks=1,
        setup_matrix=setup_matrix,
        toestellen={},
    )

    assert outcome.status == "complete"
    assert {order.productie_datum for order in outcome.scheduled} == {MONDAY}
    assert outcome.score.rejected_orders == 0


# ── G1 / G2: grouping in the assignment model ────────────────────────────────

THURSDAY = MONDAY + timedelta(days=3)


def _product(art_nr, schaal, kapvorm, dagen, identiek_groep=None):
    p = make_product(
        art_nr, snelheid=1000, alleen_l1=True, kapvorm=kapvorm, dagen=dagen,
        identiek_groep=identiek_groep,
    )
    p.schaal = schaal
    return p


def _solve(orders, products, setup_matrix):
    outcome = assign_production_dates_feedback(
        orders, products, MONDAY, weeks=1, setup_matrix=setup_matrix, toestellen={},
    )
    assert outcome.status == "complete"
    assert outcome.score.rejected_orders == 0
    return {o.order_nr: o.productie_datum for o in outcome.scheduled}


def _fillers():
    """One order pinned to Monday, one to Thursday: both days are active anyway."""
    return (
        {
            "F1": _product("F1", "C2208", "K1", ["ma"]),
            "F2": _product("F2", "C2176-1A", "K2", ["do"]),
        },
        [
            make_order("F1", 500, MONDAY, lever=MONDAY, order_nr="f1"),
            make_order("F2", 500, THURSDAY, lever=THURSDAY, order_nr="f2"),
        ],
    )


def test_a_kapvorm_joins_the_campaign_on_the_next_day(setup_matrix):
    """
    G2 over days (planner 15/10). K may run Tuesday or Thursday, both days are
    active anyway. Per (day, line) the two cost the same — one more H22 slot
    either way. Only on Tuesday does it continue Monday's H22 campaign.
    """
    tuesday = MONDAY + timedelta(days=1)
    products = {
        "F1": _product("F1", "", "H22", ["ma"]),
        "F2": _product("F2", "", "K2", ["di"]),
        "F3": _product("F3", "", "K3", ["do"]),
        "K": _product("K", "", "H22", ["di", "do"]),
    }
    orders = [
        make_order("F1", 500, MONDAY, lever=MONDAY, order_nr="f1"),
        make_order("F2", 500, tuesday, lever=tuesday, order_nr="f2"),
        make_order("F3", 500, THURSDAY, lever=THURSDAY, order_nr="f3"),
        make_order("K", 500, THURSDAY, lever=THURSDAY, order_nr="k"),
    ]

    days = _solve(orders, products, setup_matrix)

    assert days["k"] == tuesday


def test_identical_products_are_assigned_to_the_same_day(setup_matrix):
    """
    Without G1, B would join Thursday: it shares F2's scale format there. The
    identical-split penalty outweighs that and keeps it with its twin A.
    """
    products, orders = _fillers()
    products["A"] = _product("A", "C2325-1C", "KA", ["ma", "do"], identiek_groep="A")
    products["B"] = _product("B", "C2176-1A", "KB", ["ma", "do"], identiek_groep="A")
    orders += [
        make_order("A", 500, MONDAY, lever=MONDAY, order_nr="a"),
        make_order("B", 500, THURSDAY, lever=THURSDAY, order_nr="b"),
    ]

    days = _solve(orders, products, setup_matrix)

    assert days["a"] == days["b"] == MONDAY


def test_same_kapvorm_is_assigned_to_the_same_day(setup_matrix):
    """K shares F2's scale format on Thursday but F1's kapvorm on Monday;
    kapvorm (G2) outranks the scale-format cluster."""
    products, orders = _fillers()
    products["F1"].kapvorm = "H22"
    products["K"] = _product("K", "C2176-1A", "H22", ["ma", "do"])
    orders.append(make_order("K", 500, THURSDAY, lever=THURSDAY, order_nr="k"))

    days = _solve(orders, products, setup_matrix)

    assert days["k"] == MONDAY


def test_an_identical_pair_that_cannot_share_a_day_is_split_not_rejected(setup_matrix):
    products = {
        "A": _product("A", "UK-59", "KA", ["ma"], identiek_groep="A"),
        "B": _product("B", "UK-59", "KB", ["do"], identiek_groep="A"),
    }
    orders = [
        make_order("A", 500, MONDAY, lever=THURSDAY, order_nr="a"),
        make_order("B", 500, THURSDAY, lever=THURSDAY, order_nr="b"),
    ]

    days = _solve(orders, products, setup_matrix)

    assert days == {"a": MONDAY, "b": THURSDAY}


def test_an_order_due_inside_the_commit_window_beats_a_later_one(setup_matrix):
    """
    One Tuesday, room for one of two 10h orders. Without priority the smaller
    one (U) would go, since fewer rejected units wins a tie. U is due inside
    the commit window and L only later, so L — which a later run can still
    place — is the one left out.
    """
    tuesday = MONDAY + timedelta(days=1)
    products = {
        "U": make_product("U", snelheid=1000, alleen_l1=True, dagen=["di"]),
        "L": make_product("L", snelheid=1000, alleen_l1=True, dagen=["di"]),
    }
    orders = [
        make_order("U", 9_000, tuesday, lever=tuesday, order_nr="u"),
        make_order("L", 10_000, tuesday, lever=MONDAY + timedelta(days=15), order_nr="l"),
    ]
    outcome = assign_production_dates_feedback(
        orders, products, MONDAY, weeks=1, setup_matrix=setup_matrix, toestellen={},
        commit_end=MONDAY + timedelta(weeks=1), _mandatory_orders=False,
    )
    assert [o.order_nr for o in outcome.scheduled] == ["u"]
    assert [o.order_nr for o in outcome.rejected] == ["l"]


# ── O7: fill the committed days from the front ───────────────────────────────

def _solve_committed(orders, products, setup_matrix, monkeypatch):
    monkeypatch.setattr("src.scheduler_ortools_v3._FRONTLOAD_WEIGHT", 1)
    outcome = assign_production_dates_feedback(
        orders, products, MONDAY, weeks=1, setup_matrix=setup_matrix, toestellen={},
        commit_end=MONDAY + timedelta(days=7),
    )
    assert outcome.status == "complete"
    assert outcome.score.rejected_orders == 0
    return {o.order_nr: o.productie_datum for o in outcome.scheduled}


def test_an_order_with_no_grouping_preference_fills_the_earlier_day(setup_matrix, monkeypatch):
    """X shares nothing with either filler, so Monday and Thursday cost the
    same on every other term. O7 breaks the tie towards Monday: the idle end of
    shift moves to the back of the window."""
    products, orders = _fillers()
    products["X"] = _product("X", "", "KX", ["ma", "do"])
    orders.append(make_order("X", 500, THURSDAY, lever=THURSDAY, order_nr="x"))

    days = _solve_committed(orders, products, setup_matrix, monkeypatch)

    assert days["x"] == MONDAY


def test_filling_the_earlier_day_never_beats_grouping(setup_matrix, monkeypatch):
    """K shares F2's kapvorm on Thursday. Monday would fill the earlier day,
    but O7 sits below the grouping tier, so K stays with its kapvorm."""
    products, orders = _fillers()
    products["K"] = _product("K", "", "K2", ["ma", "do"])
    orders.append(make_order("K", 500, THURSDAY, lever=THURSDAY, order_nr="k"))

    days = _solve_committed(orders, products, setup_matrix, monkeypatch)

    assert days["k"] == THURSDAY
