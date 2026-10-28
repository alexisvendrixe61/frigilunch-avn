"""
Invariants of one built production day.

These are the properties a plan must never violate. `schedule_day` is the only
thing that decides them, and until now nothing checked that it does.
"""
from __future__ import annotations

from datetime import time

import pytest

from src import config
from src.constraints import transition_cost
from src.scheduler import schedule_day, span_minuten, werk_minuten
from tests.conftest import make_order, make_product


def _all(ds):
    return ds.lijn1 + ds.lijn2


def test_canonical_transition_cost_distinguishes_scale_families(setup_matrix):
    same_a = make_product("A")
    same_b = make_product("B")
    different = make_product("C")
    same_a.schaal = "UK-59"
    same_b.schaal = "UK-57"
    different.schaal = "C2325-1C"

    assert transition_cost(same_a, same_b, setup_matrix) == 20
    assert transition_cost(same_a, different, setup_matrix) == 30


def test_canonical_transition_cost_is_directional_for_allergen_cleaning(setup_matrix):
    low = make_product("LOW")
    high = make_product("HIGH")
    low.schaal = high.schaal = "UK-59"
    low.allergie_niveau = 1
    high.allergie_niveau = 5

    assert transition_cost(low, high, setup_matrix) == 20
    assert transition_cost(high, low, setup_matrix) == 30


def test_first_run_has_no_transition_and_following_run_uses_canonical_cost(
    setup_matrix, monday
):
    first_product = make_product("A", alleen_l1=True)
    second_product = make_product("B", alleen_l1=True)
    first_product.schaal = "UK-59"
    second_product.schaal = "C2325-1C"
    products = {"A": first_product, "B": second_product}
    orders = [
        make_order("A", 100, monday, lijn=1, order_nr="first"),
        make_order("B", 100, monday, lijn=1, order_nr="second"),
    ]

    day = schedule_day(orders, products, setup_matrix, monday)

    assert day.lijn1[0].ombouw_voor == 0
    assert day.lijn1[1].ombouw_voor == transition_cost(
        day.lijn1[0].product, day.lijn1[1].product, setup_matrix
    )


# ── capacity ─────────────────────────────────────────────────────────────────

def test_nothing_is_scheduled_past_the_shift_end(setup_matrix, monday):
    products = {"A": make_product("A", snelheid=100)}
    # 12 x 500 units at 100/hr = 5h each; nowhere near all of these can fit.
    orders = [make_order("A", 500, monday, order_nr=f"o{i}") for i in range(12)]

    ds = schedule_day(orders, products, setup_matrix, monday)

    assert _all(ds), "expected at least one order to be placed"
    for po in _all(ds):
        assert po.eind <= config.shift_end(monday)
        assert po.start >= config.line_start(po.lijn)


def test_friday_is_shorter_than_a_normal_day(setup_matrix, monday, friday):
    products = {"A": make_product("A", snelheid=100)}
    mk = lambda d: [make_order("A", 500, d, order_nr=f"o{i}") for i in range(12)]

    mon = schedule_day(mk(monday), products, setup_matrix, monday)
    fri = schedule_day(mk(friday), products, setup_matrix, friday)

    for po in _all(fri):
        assert po.eind <= time(20, 30)
    assert werk_minuten(fri.lijn1) <= werk_minuten(mon.lijn1)


def test_orders_that_do_not_fit_are_reported_not_dropped_silently(setup_matrix, monday):
    """
    `_schedule_lijn` returns only what fits — it never raises and never returns
    the rest. Without the CAPACITY_DROP warning those orders would be invisible:
    not scheduled, not rejected, just gone.
    """
    products = {"A": make_product("A", snelheid=60, alleen_l1=True)}
    orders = [make_order("A", 600, monday, order_nr=f"o{i}") for i in range(8)]

    ds = schedule_day(orders, products, setup_matrix, monday)

    placed = {id(po.order) for po in _all(ds)}
    dropped = [o for o in orders if id(o) not in placed]
    assert dropped, "test needs an over-full day to be meaningful"
    assert sum(1 for w in ds.warnings if "CAPACITY_DROP" in w) == len(dropped)


# ── line assignment ──────────────────────────────────────────────────────────

def test_alleen_l1_never_lands_on_l2(setup_matrix, monday):
    products = {"A": make_product("A", alleen_l1=True)}
    # lijn=2 is what the optimizer asked for; the product rule must still win.
    ds = schedule_day([make_order("A", 500, monday, lijn=2)], products, setup_matrix, monday)
    assert len(ds.lijn1) == 1 and not ds.lijn2


def test_explicit_kan_niet_exclusion_overrides_the_optimizer(setup_matrix, monday):
    """A product marked 'kan_niet' on L2 must never run there, even if the
    solver's capacity balancing picked L2 for it."""
    products = {"A": make_product("A", l2_uitgesloten=True)}
    ds = schedule_day([make_order("A", 500, monday, lijn=2)], products, setup_matrix, monday)
    assert len(ds.lijn1) == 1 and not ds.lijn2

    products = {"B": make_product("B", l1_uitgesloten=True)}
    ds = schedule_day([make_order("B", 500, monday, lijn=1)], products, setup_matrix, monday)
    assert len(ds.lijn2) == 1 and not ds.lijn1


# ── day restrictions (PIPE-4) ────────────────────────────────────────────────

def test_a_forbidden_weekday_is_refused_not_warned_about(setup_matrix, monday):
    """
    D1 is a HARD rule and now has a BUILD-point owner. It used to only append a
    warning here while the validator raised an ERROR nobody acted on — so the
    only thing actually preventing an illegal day was the solver's slot filter,
    and anything that bypassed the solver could render a plan that violates it.
    """
    products = {"A": make_product("A", dagen=["di", "wo", "vr"])}   # not Monday
    ds = schedule_day([make_order("A", 300, monday)], products, setup_matrix, monday)

    assert _all(ds) == [], "an order on a forbidden weekday must not be scheduled"
    assert any("DAG_RESTRICTIE_DROP" in w for w in ds.warnings)


def test_an_allowed_weekday_still_runs(setup_matrix, monday):
    products = {"A": make_product("A", dagen=["ma", "di"])}
    ds = schedule_day([make_order("A", 300, monday)], products, setup_matrix, monday)
    assert len(_all(ds)) == 1


def test_the_refusal_holds_even_when_the_optimizer_picked_the_day(setup_matrix, friday):
    """A hand-set or carried-over production date does not get a pass."""
    products = {"A": make_product("A", dagen=["ma"])}
    order = make_order("A", 300, friday, lijn=1)
    ds = schedule_day([order], products, setup_matrix, friday)
    assert _all(ds) == []


# ── allergens ────────────────────────────────────────────────────────────────

def test_allergen_drop_always_gets_cleaning_time(setup_matrix, monday):
    """
    Going from a high allergen level to a lower one requires cleaning. Whatever
    sequence the scheduler picks, a drop must carry a changeover >= the
    configured cleaning time.
    """
    hi = make_product("HI", allergie_niveau=3,
                      allergenen={**{"gluten": "X", "melk": "X", "soja": "X", "eieren": "X"},
                                  **{k: "-" for k in ("aardnoten", "lupine", "mosterd", "noten",
                                                      "schaaldieren", "selderij", "sesamzaad",
                                                      "vis", "weekdieren")}})
    lo = make_product("LO", allergie_niveau=0)
    products = {"HI": hi, "LO": lo}
    orders = [make_order("HI", 400, monday, order_nr="a"),
              make_order("LO", 400, monday, order_nr="b")]

    ds = schedule_day(orders, products, setup_matrix, monday)
    cleaning = setup_matrix["reiniging"]["allergie_daling"]

    for line in (ds.lijn1, ds.lijn2):
        for prev, curr in zip(line, line[1:]):
            if curr.product.allergie_niveau < prev.product.allergie_niveau:
                assert curr.ombouw_voor >= cleaning, (
                    f"allergen drop {prev.product.allergie_niveau}->"
                    f"{curr.product.allergie_niveau} got only {curr.ombouw_voor}min"
                )


def test_no_hard_validation_errors_on_a_clean_day(setup_matrix, monday):
    products = {"A": make_product("A"), "B": make_product("B", allergie_niveau=1)}
    orders = [make_order("A", 300, monday, order_nr="a"),
              make_order("B", 300, monday, order_nr="b")]
    ds = schedule_day(orders, products, setup_matrix, monday)
    assert [w for w in ds.warnings if w.startswith("[ERROR]")] == []


# ── shared equipment ─────────────────────────────────────────────────────────

def test_scarce_device_is_never_double_booked_across_lines(setup_matrix, monday):
    """
    L1 and L2 both need the single Leonard. schedule_day staggers L2 around
    L1's usage; the two must never overlap in time.
    """
    products = {
        "A": make_product("A", devices=["leonard"], alleen_l1=True),
        "B": make_product("B", devices=["leonard"], alleen_l2=True, schaal="C2227-1H",
                          kapvorm="I-T"),
    }
    orders = [make_order("A", 400, monday, order_nr="a"),
              make_order("B", 400, monday, order_nr="b")]

    ds = schedule_day(orders, products, setup_matrix, monday,
                      toestellen_beschikbaarheid={"leonard": 1})

    conflicts = [w for w in ds.warnings if "TOESTEL_CONFLICT" in w]
    assert conflicts == [], conflicts
    for a in ds.lijn1:
        for b in ds.lijn2:
            if a.product.toestellen["leonard"] and b.product.toestellen["leonard"]:
                assert not (a.start < b.eind and b.start < a.eind), "Leonard double-booked"


def test_plentiful_device_may_run_on_both_lines_at_once(setup_matrix, monday):
    """8 turbos exist, so simultaneous use is not a conflict and must not be
    staggered — staggering it would waste real capacity."""
    products = {
        "A": make_product("A", devices=["turbo"], alleen_l1=True),
        "B": make_product("B", devices=["turbo"], alleen_l2=True),
    }
    orders = [make_order("A", 400, monday, order_nr="a"),
              make_order("B", 400, monday, order_nr="b")]
    ds = schedule_day(orders, products, setup_matrix, monday,
                      toestellen_beschikbaarheid={"turbo": 8})
    assert ds.lijn1[0].start == config.line_start(1)
    assert ds.lijn2[0].start == config.line_start(2)


# ── the KPI fix ──────────────────────────────────────────────────────────────

def test_work_minutes_exclude_idle_gaps(setup_matrix, monday):
    """
    werk_minuten counts production + changeover. span_minuten counts wall-clock
    from first changeover to last end. When a line is staggered around a shared
    machine the two diverge, and only the first is a capacity figure.
    """
    products = {
        "A": make_product("A", devices=["leonard"], alleen_l1=True, snelheid=100),
        "B": make_product("B", devices=["leonard"], alleen_l2=True, snelheid=100),
    }
    orders = [make_order("A", 400, monday, order_nr="a"),
              make_order("B", 200, monday, order_nr="b")]
    ds = schedule_day(orders, products, setup_matrix, monday,
                      toestellen_beschikbaarheid={"leonard": 1})

    assert ds.lijn2, "L2 order should still be placed, just later"
    assert werk_minuten(ds.lijn2) <= span_minuten(ds.lijn2)
    assert werk_minuten(ds.lijn2) < config.window_minutes(monday, 2)


def test_utilization_never_exceeds_one_hundred_percent(setup_matrix, monday):
    products = {"A": make_product("A", snelheid=100)}
    orders = [make_order("A", 500, monday, order_nr=f"o{i}") for i in range(10)]
    ds = schedule_day(orders, products, setup_matrix, monday)
    for lijn in (1, 2):
        assert 0 <= ds.kpis[f"lijn{lijn}_bezetting_pct"] <= 100


def test_fresh_and_replayed_days_are_measured_identically(setup_matrix, monday):
    """
    replay_locked_days used to divide by 16h while schedule_day divided by
    16.5h, so a frozen day read ~3% busier than an identical fresh one in the
    same workbook.
    """
    from frigilunch.domain.state import build_new_state, commit_window, partition_locked_days
    from src.scheduler import replay_locked_days

    products = {"A": make_product("A"), "B": make_product("B")}
    orders = [make_order("A", 300, monday, lijn=1, order_nr="a"),
              make_order("B", 300, monday, lijn=2, order_nr="b")]
    fresh = schedule_day(orders, products, setup_matrix, monday)

    # Round-trip the built day through the committed format, as a real run does.
    state = build_new_state([fresh], *commit_window(monday))
    locked_days, _free, warnings = partition_locked_days(orders, state, today=monday)
    replayed, replay_warnings = replay_locked_days(locked_days, products)

    assert not warnings and not replay_warnings and len(replayed) == 1
    for key in ("lijn1_werk_min", "lijn2_werk_min",
                "lijn1_window_min", "lijn2_window_min",
                "lijn1_bezetting_pct", "lijn2_bezetting_pct"):
        assert replayed[0].kpis[key] == fresh.kpis[key], key


def test_sequencer_knows_about_the_other_line_s_device_block(setup_matrix, monday):
    """
    The CP-SAT recovery sequencer used to optimize L2 against a window it
    believed was entirely free, because `blocking` was passed to the verifying
    walker but never to the solver. It would therefore hand back a sequence
    that put a device-bound order underneath L1's claim on the one machine,
    the walker would stagger it past close, and the order was dropped as
    "ORACLE-DROPPED" — capacity looked available in the approximate model but
    was not once the real device block applied.

    Here L2's two device-bound orders only both fit if the solver puts them in
    the gap BEFORE L1 takes the Leonard. A device-blind solver picks the
    cheap-changeover order first and loses the other.
    """
    from src.day_sequencer import solve_optimal_sequence
    from src.constraints.device_rules import busy_intervals
    from src.scheduler import _schedule_lijn, _time_to_minutes

    products = {
        "L1BIG": make_product("L1BIG", devices=["leonard"], alleen_l1=True),
        "EARLY": make_product("EARLY", devices=["leonard"], alleen_l2=True),
        "LATE": make_product("LATE", devices=["leonard"], alleen_l2=True,
                             schaal="C2227-1H", kapvorm="I-T"),
    }
    max_end = config.shift_end(monday)
    l1 = [(make_order("L1BIG", 2000, monday, order_nr="l1"), products["L1BIG"])]
    planned1 = _schedule_lijn(l1, 1, monday, setup_matrix, max_end)
    blocking = busy_intervals(planned1, {"leonard": 1})
    assert blocking.get("leonard"), "L1 must actually hold the Leonard"

    l2 = [(make_order("EARLY", 300, monday, order_nr="e"), products["EARLY"]),
          (make_order("LATE", 300, monday, order_nr="l"), products["LATE"])]

    seq = solve_optimal_sequence(
        l2, setup_matrix,
        _time_to_minutes(config.line_start(2)), _time_to_minutes(max_end), 2,
        blocking=blocking, toestellen_avail={"leonard": 1},
    )
    assert seq is not None
    got = _schedule_lijn(list(seq), 2, monday, setup_matrix, max_end,
                         blocking=blocking, toestellen_avail={"leonard": 1})
    assert len(got) == 2, (
        "solver proposed a sequence the walker could not realize: "
        f"{[po.order.order_nr for po in got]}"
    )


def test_the_line_that_claims_a_shared_machine_first_is_not_fixed(setup_matrix, monday):
    """
    M1 staggering is one-directional: the line built first takes the shared
    single-unit machine whenever it wants it, and the other line is pushed
    around that claim until its orders run past closing time and are dropped.
    L1 was always the line built first, which is not a fact about the floor —
    it decided which line pays the whole cost of sharing.

    Here one long L1 order holds the Leonard almost all day and buries three
    short L2 orders that need it. Building L2 first costs the one L1 order and
    saves all three, so the day the floor gets should be the mirror one.
    """
    products = {
        "BIG": make_product("BIG", devices=["leonard"], alleen_l1=True, snelheid=100),
        "SMALL": make_product("SMALL", devices=["leonard"], alleen_l2=True, snelheid=100),
    }
    orders = [make_order("BIG", 1500, monday, order_nr="big")] + [
        make_order("SMALL", 100, monday, order_nr=f"s{i}") for i in range(3)
    ]

    ds = schedule_day(orders, products, setup_matrix, monday, {"leonard": 1})

    assert len(_all(ds)) == 3, [po.order.order_nr for po in _all(ds)]
    assert {po.order.order_nr for po in ds.lijn2} == {"s0", "s1", "s2"}
    assert any("voorrang" in w for w in ds.warnings)


def test_the_mirror_build_only_replaces_a_strictly_better_day(setup_matrix, monday):
    """
    A tie keeps L1 first. The mirror is a fallback for days the one-directional
    staggering actually loses orders on, not a new default line order.
    """
    products = {
        "A": make_product("A", devices=["leonard"], alleen_l1=True, snelheid=100),
        "B": make_product("B", devices=["leonard"], alleen_l2=True, snelheid=100),
    }
    orders = [
        make_order("A", 200, monday, order_nr="a"),
        make_order("B", 200, monday, order_nr="b"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday, {"leonard": 1})

    assert len(_all(ds)) == 2
    assert ds.lijn1[0].start == config.line_start(1), "L1 should still open the day"
    assert not any("voorrang" in w for w in ds.warnings)


def test_the_line_built_first_carries_no_blocking(setup_matrix, monday):
    """
    Only the SECOND line staggers. Later passes (the S2 repair, the sequencer
    recovery) re-walk a line and need the same blocking it was built with, so
    `_build_line_pair` reports it per line — and for the first line that has to
    be nothing. Handing a line its own busy intervals makes `stagger_around`
    push each of its orders past its own earlier claim on the machine, and a
    line that fits perfectly well then looks impossible and gives up orders to
    a conflict with itself.
    """
    from src.scheduler import _build_line_pair

    products = {
        "A": make_product("A", devices=["leonard"], alleen_l1=True, snelheid=100),
        "B": make_product("B", devices=["leonard"], alleen_l2=True, snelheid=100),
    }
    l1 = [(make_order("A", 200, monday, order_nr="a"), products["A"])]
    l2 = [(make_order("B", 200, monday, order_nr="b"), products["B"])]

    for first in (1, 2):
        built = _build_line_pair(
            first, {1: l1, 2: l2}, {1: l1, 2: l2}, monday, setup_matrix,
            config.shift_end(monday), {"leonard": 1}, False,
        )
        second = 2 if first == 1 else 1
        assert built[first][1] is None, f"line {first} was built first"
        assert built[second][1], f"line {second} must stagger around line {first}"


# ── M1: telling a device-caused drop apart from a full day ───────────────────
#
# The day builder knew why it dropped an order and threw the reason away, so
# the solver learned "this set is too big" when the truth was "these two cannot
# share a day". See docs/toestel-cut-gap.md.

def test_a_drop_caused_by_the_other_line_s_machine_says_so(setup_matrix, monday):
    """
    One long L1 order holds the Leonard almost all day against three short L2
    orders that need it. The mirror build saves the three and gives up the one,
    and that one is not a capacity casualty: on a line of its own it fits with
    hours to spare.
    """
    products = {
        "BIG": make_product("BIG", devices=["leonard"], alleen_l1=True, snelheid=100),
        "SMALL": make_product("SMALL", devices=["leonard"], alleen_l2=True, snelheid=100),
    }
    orders = [make_order("BIG", 1500, monday, order_nr="big")] + [
        make_order("SMALL", 100, monday, order_nr=f"s{i}") for i in range(3)
    ]

    ds = schedule_day(orders, products, setup_matrix, monday, {"leonard": 1})

    assert [o.order_nr for o in ds.dropped] == ["big"]
    assert len(ds.device_dropped) == 1
    order, lijn, devs = ds.device_dropped[0]
    assert (order.order_nr, lijn, devs) == ("big", 1, ["leonard"])
    assert any("TOESTEL_DROP" in w for w in ds.warnings)
    assert not any("CAPACITY_DROP" in w for w in ds.warnings)


def test_an_order_that_would_not_have_fitted_anyway_is_not_blamed_on_the_machine(
    setup_matrix, monday
):
    """
    The failure mode this whole idea has to avoid. An order pushed by a device
    block may simply not have fit; recording that as a device conflict forbids
    a pairing that is in fact perfectly schedulable, and then the change costs
    orders instead of winning them.

    Here the L2 order needs the blocked machine and is dropped on a day where
    the machine IS contended — but it is 20 hours long and no shift could ever
    have held it. It must be reported as capacity.
    """
    products = {
        "SHORT": make_product("SHORT", devices=["leonard"], alleen_l1=True, snelheid=100),
        "HUGE": make_product("HUGE", devices=["leonard"], alleen_l2=True, snelheid=100),
    }
    orders = [
        make_order("SHORT", 100, monday, order_nr="short"),
        make_order("HUGE", 2000, monday, order_nr="huge"),
    ]

    ds = schedule_day(orders, products, setup_matrix, monday, {"leonard": 1})

    assert [o.order_nr for o in ds.dropped] == ["huge"]
    assert ds.device_dropped == [], "it dies with or without the block: capacity"
    assert any("CAPACITY_DROP" in w for w in ds.warnings)
    assert not any("TOESTEL_DROP" in w for w in ds.warnings)


def test_the_oracle_turns_a_device_drop_into_a_pair(setup_matrix, monday):
    """
    What the cut is built from: the order that was lost, paired with the order
    on the other line that was holding the machine. The overlap detector next
    to it can essentially never fire — M1's staggering is what removes the
    overlap — so this is where the solver's evidence about machines comes from.
    """
    from src.scheduler_ortools_v3 import _run_oracle_day

    products = {
        "BIG": make_product("BIG", devices=["leonard"], alleen_l1=True, snelheid=100),
        "SMALL": make_product("SMALL", devices=["leonard"], alleen_l2=True, snelheid=100),
    }
    unit_order_obj = {
        0: make_order("BIG", 1500, monday, order_nr="big"),
        1: make_order("SMALL", 100, monday, order_nr="s0"),
        2: make_order("SMALL", 100, monday, order_nr="s1"),
        3: make_order("SMALL", 100, monday, order_nr="s2"),
    }

    fit, conflicts, s2, device_pairs, sate = _run_oracle_day(
        list(unit_order_obj), unit_order_obj, products, setup_matrix,
        {"leonard": 1}, monday,
    )

    assert 0 not in fit, "the L1 order is the one given up"
    assert conflicts == set(), "staggering means the two never overlap in time"
    assert s2 == set()
    assert sate == set()
    assert device_pairs, "but the loss is still the machine's doing"
    assert all(0 in pair for pair in device_pairs)
    assert {b for pair in device_pairs for b in pair} - {0} <= {1, 2, 3}
