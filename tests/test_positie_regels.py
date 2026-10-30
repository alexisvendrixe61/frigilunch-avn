"""
S2 — must / cannot be first or last of the production day.

Columns 28-31 of the product database. They were parsed into Product and then
read by nobody; these tests pin the enforcement now that the sequencer honours
them.
"""
from __future__ import annotations

from datetime import date

from frigilunch.domain.constraints.sequencing import apply_positie_regels

from conftest import make_order, make_product

DAG = date(2026, 3, 3)  # a Tuesday


def _seq(*products):
    return [(make_order(p.art_nr, 1000, DAG), p) for p in products]


def _names(seq):
    return [p.art_nr for _, p in seq]


def test_opstart_moet_is_pulled_to_the_front():
    a, b, c = make_product("A"), make_product("B"), make_product("C", opstart_moet=True)
    assert _names(apply_positie_regels(_seq(a, b, c)))[0] == "C"


def test_afsluit_moet_is_pushed_to_the_back():
    a, b, c = make_product("A", afsluit_moet=True), make_product("B"), make_product("C")
    assert _names(apply_positie_regels(_seq(a, b, c)))[-1] == "A"


def test_opstart_kan_niet_never_opens_the_day():
    a, b, c = make_product("A", opstart_kan_niet=True), make_product("B"), make_product("C")
    assert _names(apply_positie_regels(_seq(a, b, c)))[0] != "A"


def test_afsluit_kan_niet_never_closes_the_day():
    a, b, c = make_product("A"), make_product("B"), make_product("C", afsluit_kan_niet=True)
    assert _names(apply_positie_regels(_seq(a, b, c)))[-1] != "C"


def test_both_ends_are_repaired_at_once():
    a = make_product("A", opstart_kan_niet=True)
    b = make_product("B")
    c = make_product("C", afsluit_kan_niet=True)
    out = _names(apply_positie_regels(_seq(a, b, c)))
    assert out[0] != "A" and out[-1] != "C"


def test_two_order_day_satisfies_both_ends():
    a = make_product("A", opstart_kan_niet=True)
    b = make_product("B", afsluit_kan_niet=True)
    assert _names(apply_positie_regels(_seq(a, b))) == ["B", "A"]


def test_middle_keeps_its_incoming_order():
    ps = [make_product(x) for x in "ABCDE"]
    ps[4] = make_product("E", opstart_moet=True)
    assert _names(apply_positie_regels(_seq(*ps))) == ["E", "A", "B", "C", "D"]


def test_moet_beats_a_contradictory_kan_niet_on_the_same_product():
    # A few sheet rows set "opstart moet" and "opstart kan niet" together.
    a = make_product("A", opstart_moet=True, opstart_kan_niet=True)
    b = make_product("B")
    assert _names(apply_positie_regels(_seq(a, b)))[0] == "A"


def test_impossible_day_is_left_alone_for_the_validator():
    # Every product barred from opening: nothing to swap in, so the sequence
    # comes back unchanged rather than being silently mangled.
    a = make_product("A", opstart_kan_niet=True)
    b = make_product("B", opstart_kan_niet=True)
    assert _names(apply_positie_regels(_seq(a, b))) == ["A", "B"]


def test_single_order_day_is_untouched():
    a = make_product("A", opstart_kan_niet=True, afsluit_kan_niet=True)
    assert _names(apply_positie_regels(_seq(a))) == ["A"]


def test_head_does_not_steal_the_only_legal_closer():
    # Roughly a quarter of the catalogue is "afsluit kan niet", so a two-order
    # day with exactly one legal closer is common. The head used to take it and
    # leave the day unclosable.
    a = make_product("A")                          # may open, may close
    b = make_product("B", afsluit_kan_niet=True)   # may open, may NOT close
    out = _names(apply_positie_regels(_seq(a, b)))
    assert out == ["B", "A"]


def test_head_leaves_a_legal_closer_on_a_longer_day():
    a = make_product("A")
    b = make_product("B", afsluit_kan_niet=True)
    c = make_product("C", afsluit_kan_niet=True)
    out = _names(apply_positie_regels(_seq(a, b, c)))
    assert out[-1] == "A"
    assert out[0] != "A"


def test_head_yields_to_a_claimed_must_close():
    # Tail already settled by "afsluit moet", so the head is free to take the
    # first legal candidate even though it could also have closed.
    a = make_product("A")
    b = make_product("B", afsluit_moet=True)
    assert _names(apply_positie_regels(_seq(a, b))) == ["A", "B"]


def test_no_legal_closer_at_all_is_left_for_the_validator():
    a = make_product("A", afsluit_kan_niet=True)
    b = make_product("B", afsluit_kan_niet=True)
    assert _names(apply_positie_regels(_seq(a, b))) == ["A", "B"]


# ── the guarantee: a violating day is never handed to the floor ──────────────
#
# apply_positie_regels above returns the sequence untouched when no legal
# arrangement exists — that is the sequencer being honest, not the final word.
# schedule_day must then refuse to emit the day rather than plan it with the
# violation and leave a warning behind, which is what it used to do: a real
# wk36 plan shipped 13 line-days the floor cannot run as written.

def _day(setup_matrix, *products, aantal=1000):
    from frigilunch.application.scheduler import schedule_day
    orders = [make_order(p.art_nr, aantal, DAG) for p in products]
    return schedule_day(orders, {p.art_nr: p for p in products}, setup_matrix, DAG)


def test_a_day_that_cannot_be_closed_legally_is_emptied_not_shipped(setup_matrix):
    """No survivor may close, so no ordering is legal — the day must go."""
    a = make_product("A", afsluit_kan_niet=True)
    b = make_product("B", afsluit_kan_niet=True)
    ds = _day(setup_matrix, a, b)

    assert ds.lijn1 == [] and ds.lijn2 == []
    assert {o.art_nr for o in ds.dropped} == {"A", "B"}
    assert any("POSITIE_REGEL_DROP" in w for w in ds.warnings)


def test_a_legal_closer_is_moved_to_the_end_rather_than_dropped(setup_matrix):
    """Giving up an order is the last resort, not the first.

    Both pinned to L1: on separate lines each would be a single-order day and
    B alone still cannot close one.
    """
    a = make_product("A", alleen_l1=True)                        # may close
    b = make_product("B", alleen_l1=True, afsluit_kan_niet=True)
    ds = _day(setup_matrix, a, b)

    planned = ds.lijn1 + ds.lijn2
    assert len(planned) == 2, "neither order should have been given up"
    assert ds.dropped == []


def test_every_emitted_line_day_has_legal_ends(setup_matrix):
    """The invariant itself, over a mix the sequencer cannot fully satisfy."""
    from frigilunch.domain.constraints.sequencing import mag_eerst, mag_laatst
    products = [
        make_product("A", afsluit_kan_niet=True),
        make_product("B", afsluit_kan_niet=True),
        make_product("C", opstart_kan_niet=True),
        make_product("D"),
    ]
    ds = _day(setup_matrix, *products)
    for line in (ds.lijn1, ds.lijn2):
        if not line:
            continue
        assert mag_eerst(line[0].product), f"{line[0].product.art_nr} mag niet openen"
        assert mag_laatst(line[-1].product), f"{line[-1].product.art_nr} mag niet afsluiten"


def test_a_single_order_that_may_not_close_is_refused(setup_matrix):
    """It is both the opener and the closer, so the rule still binds."""
    a = make_product("A", afsluit_kan_niet=True)
    ds = _day(setup_matrix, a)
    assert ds.lijn1 == [] and ds.lijn2 == []
    assert [o.art_nr for o in ds.dropped] == ["A"]


# ── The rejection CAUSE, not just the removal ─────────────────────────────────
# An S2 removal used to reach the planner as ORACLE-DROPPED: a capacity story,
# with the scarce-device note appended. Both halves are wrong — such an order
# typically fits with the line idle most of the day, and the machines named
# were never contended for. These pin the separate cause.

def test_a_position_drop_is_recorded_separately_from_a_capacity_drop(setup_matrix):
    a = make_product("A", afsluit_kan_niet=True)
    ds = _day(setup_matrix, a)
    assert [o.art_nr for o in ds.dropped] == ["A"]
    assert [o.art_nr for o in ds.positie_dropped] == ["A"]


def test_a_capacity_drop_is_not_a_position_drop(setup_matrix):
    """A day that overflows on time must not be blamed on the ends."""
    products = [make_product("A"), make_product("B"), make_product("C")]
    ds = _day(setup_matrix, *products, aantal=900_000)   # far past any shift
    assert ds.dropped, "expected the day to overflow"
    assert ds.positie_dropped == []


def test_the_reason_names_the_end_the_product_is_barred_from():
    from frigilunch.application.date_optimizer import _positie_reason

    order = make_order("A", 1000, DAG)
    cannot_close = _positie_reason(order, make_product("A", afsluit_kan_niet=True))
    cannot_open = _positie_reason(order, make_product("A", opstart_kan_niet=True))
    neither = _positie_reason(
        order, make_product("A", opstart_kan_niet=True, afsluit_kan_niet=True)
    )

    assert "may not close the day" in cannot_close
    assert "may not open the day" in cannot_open
    assert "may not open or close the day" in neither
    for reason in (cannot_close, cannot_open, neither):
        assert reason.startswith("POSITIE-REGEL")


def test_the_reason_does_not_blame_capacity_or_devices():
    """The whole point: stop sending planners after capacity that was fine."""
    from frigilunch.application.date_optimizer import _positie_reason

    reason = _positie_reason(
        make_order("A", 1000, DAG),
        make_product("A", afsluit_kan_niet=True, devices=["gernal", "leonard"]),
    )
    assert "TOESTELLEN" not in reason
    assert "capacity" in reason  # only to say it FIT on capacity
    assert "must be repaired" in reason


def test_the_reason_does_not_claim_the_order_is_impossible():
    """
    Checked against a real case: other days in the window DID have a legal
    arrangement. The reason may only speak for the day actually assigned.
    """
    from frigilunch.application.date_optimizer import _positie_reason

    reason = _positie_reason(
        make_order("A", 1000, DAG), make_product("A", afsluit_kan_niet=True)
    )
    assert "Other unfrozen days" in reason
    assert "may still support a legal pair" in reason


def test_a_position_rejection_reaches_the_planner_action_list():
    """
    Unlike the capacity causes, this one never resolves by itself — every
    future run hits the identical dead end. It has to be on the list.
    """
    from frigilunch.adapters import exceptions_report
    from frigilunch.domain.state import order_identity

    order = make_order("A", 1000, DAG, order_nr="positie")
    products = {"A": make_product("A", afsluit_kan_niet=True)}
    rows = exceptions_report.build(
        [order], products, {order_identity(order): "POSITIE-REGEL: ..."}
    )
    assert len(rows) == 1


# ── Which order gets given up when the day cannot be made legal ───────────────

def test_the_only_order_able_to_close_is_not_the_one_given_up(setup_matrix):
    """
    Two equal-sized orders, one barred from both ends. Removing on size alone
    lets the tie fall on the only order that can close, which then strands the
    barred one and costs a second removal — both lost where one could have
    survived.
    """
    burden = make_product("BURDEN", opstart_kan_niet=True, afsluit_kan_niet=True)
    anchor = make_product("ANCHOR")
    ds = _day(setup_matrix, burden, anchor, aantal=2000)

    planned = [po.order.art_nr for po in ds.lijn1 + ds.lijn2]
    assert planned == ["ANCHOR"], "the order that can hold an end must survive"
    assert [o.art_nr for o in ds.dropped] == ["BURDEN"]


def test_a_small_sole_closer_outranks_a_large_burden(setup_matrix):
    """Size must not override being the only order able to take an end."""
    burden = make_product("BURDEN", afsluit_kan_niet=True)
    anchor = make_product("ANCHOR")
    orders = [
        make_order("BURDEN", 20_000, DAG),
        make_order("ANCHOR", 500, DAG),      # by far the smallest
    ]
    from frigilunch.application.scheduler import schedule_day
    ds = schedule_day(
        orders, {"BURDEN": burden, "ANCHOR": anchor}, setup_matrix, DAG,
    )
    planned = {po.order.art_nr for po in ds.lijn1 + ds.lijn2}
    assert "ANCHOR" in planned, "the sole closer must not be dropped for being small"


# ── a "moet" is hard ─────────────────────────────────────────────────────────
#
# A day has one first order and one last order. Two different products that
# both MUST open it can therefore not share it. Planner, 15/10: the product
# database's first/last rules are "set in stone" — so the day gives up one of
# the two orders (the feedback loop then places it elsewhere) rather than ship
# a day whose "moet opstarten" product runs second. Two orders of the SAME
# product open the day together, as one run, and that is honoured.

def test_two_must_open_products_never_share_a_line_day(setup_matrix):
    from frigilunch.application.scheduler import schedule_day
    from frigilunch.domain.validator import validate_schedule

    products = {
        "FIRST": make_product("FIRST", snelheid=1000, opstart_moet=True, alleen_l1=True),
        "ALSO": make_product("ALSO", snelheid=1000, opstart_moet=True, alleen_l1=True,
                             schaal="C2227-1H"),
    }
    orders = [
        make_order("FIRST", 2000, DAG, order_nr="first"),
        make_order("ALSO", 2000, DAG, order_nr="also"),
    ]

    ds = schedule_day(orders, products, setup_matrix, DAG)
    errors = validate_schedule(ds, products)

    assert len(ds.lijn1) == 1
    assert [o.order_nr for o in ds.dropped] in (["first"], ["also"])
    assert not [e for e in errors if "MOET" in e.code], [e.code for e in errors]


def test_two_orders_of_one_must_open_product_open_the_day_together(setup_matrix):
    from frigilunch.application.scheduler import schedule_day
    from frigilunch.domain.validator import validate_schedule

    products = {
        "M": make_product("M", snelheid=1000, opstart_moet=True, alleen_l1=True),
        "X": make_product("X", snelheid=1000, alleen_l1=True, schaal="C2227-1H"),
    }
    orders = [
        make_order("X", 1000, DAG, order_nr="x"),
        make_order("M", 1000, DAG, order_nr="m1"),
        make_order("M", 2000, DAG, order_nr="m2"),
    ]

    ds = schedule_day(orders, products, setup_matrix, DAG)

    assert [po.product.art_nr for po in ds.lijn1] == ["M", "M", "X"]
    assert not [e for e in validate_schedule(ds, products) if "MOET" in e.code]


def test_an_honoured_moet_is_not_reported(setup_matrix):
    from frigilunch.application.scheduler import schedule_day
    from frigilunch.domain.validator import validate_schedule

    products = {
        "FIRST": make_product("FIRST", snelheid=1000, opstart_moet=True, alleen_l1=True),
        "LAST": make_product("LAST", snelheid=1000, afsluit_moet=True, alleen_l1=True,
                             schaal="C2227-1H"),
    }
    orders = [
        make_order("FIRST", 2000, DAG, order_nr="first"),
        make_order("LAST", 2000, DAG, order_nr="last"),
    ]

    ds = schedule_day(orders, products, setup_matrix, DAG)

    assert [po.order.order_nr for po in ds.lijn1] == ["first", "last"]
    assert not [e for e in validate_schedule(ds, products) if "MOET" in e.code]


def test_the_solver_keeps_two_must_open_products_off_one_line_day(setup_matrix):
    """
    The requirement is decided at assignment, not at sequencing: by the time a
    day holds both, no sequence can satisfy them. Two days are free here.
    """
    from datetime import timedelta

    from frigilunch.application.date_optimizer import assign_production_dates_feedback

    start = date(2026, 7, 27)  # a Monday
    products = {
        "M1": make_product("M1", snelheid=1000, opstart_moet=True),
        "M2": make_product("M2", snelheid=1000, opstart_moet=True),
    }
    orders = [
        make_order("M1", 2000, start, lever=start + timedelta(days=9), order_nr="m1"),
        make_order("M2", 2000, start, lever=start + timedelta(days=9), order_nr="m2"),
    ]

    scheduled, rejected, _ = assign_production_dates_feedback(
        orders, products, start, weeks=2, setup_matrix=setup_matrix, toestellen={},
    )

    assert rejected == []
    slots = {o.order_nr: (o.productie_datum, o.lijn) for o in scheduled}
    assert slots["m1"] != slots["m2"], slots


def test_positie_ok_reads_a_run_of_one_article_as_the_opener():
    from frigilunch.domain.constraints.sequencing import positie_ok

    m = make_product("M", opstart_moet=True)
    n = make_product("N", opstart_moet=True)
    x = make_product("X")
    z = make_product("Z", afsluit_moet=True)
    assert positie_ok([m, m, x, z])
    assert not positie_ok([m, x, m, z]), "second M order is not in the opening run"
    assert not positie_ok([m, n, x]), "two articles cannot both open"
    assert not positie_ok([m, z, x]), "the closer must close"
