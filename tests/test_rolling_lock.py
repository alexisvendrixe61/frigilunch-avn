"""
The rolling commit — the behaviour the whole system exists for.

  run wk31 -> locks wk31..34
  run wk32 -> wk31 has passed and is dropped; wk32..34 arrive already locked
              and untouched; exactly one new week (35) gets committed

Nothing guarded this before. These tests drive src/plan_state.py directly
rather than through the solver, so they are fast and deterministic — the
solver's own non-determinism is irrelevant to whether the lock rolls.

The committed unit is the DAY (see plan_state's module docstring). Several
tests below exist because the previous order-keyed format let real leaks
through: two chunks of one order sharing a day and line silently destroyed
one of them, an order the day builder dropped got committed anyway, and a
frozen day's warnings were reset to empty on replay.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, time, timedelta

import pytest

from src.models import DaySchedule, PlannedOrder
from frigilunch.adapters.json_state import load_plan_state, save_plan_state
from frigilunch.domain.state import (
    COMMIT_WEEKS,
    block_end,
    build_new_state,
    commit_window,
    explain_from_history,
    merge_rejection_history,
    order_identity,
    partition_locked_days,
    roll_completed_orders,
)
from tests.conftest import make_order, make_product

WK31 = date(2026, 7, 27)          # Monday of ISO week 31
WK32 = WK31 + timedelta(weeks=1)
WK33 = WK31 + timedelta(weeks=2)
WK34 = WK31 + timedelta(weeks=3)
WK35 = WK31 + timedelta(weeks=4)
WK36 = WK31 + timedelta(weeks=5)


def _po(order, lijn=None, start=time(6, 0), eind=time(8, 0), ombouw=20) -> PlannedOrder:
    return PlannedOrder(
        order=order, product=make_product(order.art_nr), lijn=lijn or order.lijn or 1,
        start=start, eind=eind, ombouw_voor=ombouw,
    )


def _day(datum, orders, *, warnings=None, dropped=None) -> DaySchedule:
    """A built day: `orders` land on their own `lijn`, in the order given."""
    day = DaySchedule(datum=datum, warnings=list(warnings or []), dropped=list(dropped or []))
    for i, o in enumerate(orders):
        po = _po(o, start=time(6 + 2 * i, 0), eind=time(8 + 2 * i, 0))
        (day.lijn1 if po.lijn == 1 else day.lijn2).append(po)
    return day


def _commit(schedules, plan_start):
    """One run's commit step."""
    return build_new_state(schedules, *commit_window(plan_start))


# ── the window itself ────────────────────────────────────────────────────────

def test_commit_window_spans_four_weeks_from_this_runs_monday():
    cs, ce = commit_window(WK31)
    assert cs == WK31
    assert ce == WK35                      # exclusive end == wk35 Monday
    assert (ce - cs).days == COMMIT_WEEKS * 7


def test_freeze_boundary_blocks_the_solver_from_committed_days():
    """`frozen_until` bars the DAYS, not just the orders already on them."""
    assert block_end(WK32, frozen_until=WK35.isoformat()) == WK35
    assert block_end(WK31, frozen_until=None) == WK31       # first ever run
    # A frozen_until in the past never pushes the start backwards.
    assert block_end(WK36, frozen_until=WK35.isoformat()) == WK36


def test_only_days_inside_the_window_are_committed():
    inside = _day(WK33, [make_order("A", 100, WK33, lijn=1, order_nr="in")])
    outside = _day(WK35, [make_order("B", 100, WK35, lijn=1, order_nr="out")])

    state = _commit([inside, outside], WK31)
    assert list(state) == [WK33.isoformat()]


# ── the roll ─────────────────────────────────────────────────────────────────

def test_running_the_next_week_commits_exactly_one_more_week():
    """The headline behaviour: wk31 locks 31-34, wk32 adds only 35."""
    wk31_orders = [
        make_order("A", 100, WK31, lijn=1, order_nr="o31"),
        make_order("B", 100, WK33, lijn=1, order_nr="o33"),
        make_order("C", 100, WK34, lijn=2, order_nr="o34"),
    ]
    state31 = _commit([_day(o.productie_datum, [o]) for o in wk31_orders], WK31)
    assert len(state31) == 3

    # ── next week ──
    all_orders = wk31_orders + [make_order("D", 100, WK35, lijn=1, order_nr="o35")]
    locked_days, free, warnings = partition_locked_days(all_orders, state31, today=WK32)

    # wk31's own day is in the past on the wk32 run: produced, dropped.
    assert [d.datum for d in locked_days] == [WK33, WK34]
    assert {r.order.order_nr for d in locked_days for r in d.runs} == {"o33", "o34"}
    assert not warnings
    # The wk35 order is the only thing left to plan.
    assert {o.order_nr for o in free} == {"o35"}

    newly = _day(WK35, [make_order("D", 100, WK35, lijn=1, order_nr="o35")])
    replayed = [_day(d.datum, [r.order for r in d.runs]) for d in locked_days]
    state32 = _commit(replayed + [newly], WK32)

    assert sorted(state32) == [d.isoformat() for d in (WK33, WK34, WK35)]
    assert block_end(WK32, commit_window(WK32)[1].isoformat()) == WK36


def test_three_week_roll_replays_survivors_and_only_adds_the_new_fourth_week():
    """Across consecutive runs, published days survive byte-for-byte.

    The state stores populated days rather than empty calendar dates, so the
    four-week publication boundary is asserted through ``commit_window`` and
    ``frozen_until`` while the exact persisted payload proves immutability.
    """
    original_orders = [
        make_order("A", 100, WK32, lijn=1, order_nr="o32"),
        make_order("B", 100, WK33, lijn=2, order_nr="o33"),
        make_order("C", 100, WK34, lijn=1, order_nr="o34"),
    ]
    state31 = _commit([_day(o.productie_datum, [o]) for o in original_orders], WK31)
    frozen31 = commit_window(WK31)[1]
    assert frozen31 == WK35

    payload33 = json.loads(json.dumps(state31[WK33.isoformat()]))
    payload34 = json.loads(json.dumps(state31[WK34.isoformat()]))

    wk35_order = make_order("D", 100, WK35, lijn=2, order_nr="o35")
    all_orders = original_orders + [wk35_order]
    locked32, free32, warnings32 = partition_locked_days(all_orders, state31, today=WK32)
    assert not warnings32
    assert {o.order_nr for o in free32} == {"o35"}

    state32 = _commit(
        [_day_from_locked(day) for day in locked32] + [_day(WK35, [wk35_order])],
        WK32,
    )
    assert commit_window(WK32)[1] == WK36
    assert state32[WK33.isoformat()] == payload33
    assert state32[WK34.isoformat()] == payload34

    wk36_order = make_order("E", 100, WK36, lijn=1, order_nr="o36")
    all_orders.append(wk36_order)
    locked33, free33, warnings33 = partition_locked_days(all_orders, state32, today=WK33)
    assert not warnings33
    assert {o.order_nr for o in free33} == {"o36"}

    state33 = _commit(
        [_day_from_locked(day) for day in locked33] + [_day(WK36, [wk36_order])],
        WK33,
    )
    assert commit_window(WK33)[1] == WK31 + timedelta(weeks=6)
    assert state33[WK33.isoformat()] == payload33
    assert state33[WK34.isoformat()] == payload34
    assert WK35.isoformat() in state33
    assert WK36.isoformat() in state33


def test_carried_forward_days_keep_their_exact_time_slots():
    """
    A frozen day is replayed, not recomputed — so its persisted slots must
    survive every carry-forward untouched. If they didn't, a committed day
    could silently re-render differently next week.
    """
    orders = [make_order("A", 100, WK33, lijn=1, order_nr="keep")]
    day = _day(WK33, orders)
    day.lijn1[0].start, day.lijn1[0].eind = time(9, 30), time(11, 30)
    state = _commit([day], WK31)
    original = state[WK33.isoformat()]["lijn1"][0]

    for run_start in (WK32, WK33):
        locked_days, _free, _w = partition_locked_days(orders, state, today=run_start)
        assert len(locked_days) == 1
        run = locked_days[0].lijn1[0]
        assert (run.start, run.eind, run.ombouw_voor) == ("09:30", "11:30", 20)
        state = _commit([_day_from_locked(locked_days[0])], run_start)

    assert state[WK33.isoformat()]["lijn1"][0] == original


def _day_from_locked(locked) -> DaySchedule:
    """Re-render a LockedDay the way replay_locked_days does, for round-tripping."""
    day = DaySchedule(datum=locked.datum, warnings=list(locked.warnings))
    for lijn, runs in ((1, locked.lijn1), (2, locked.lijn2)):
        for r in runs:
            target = day.lijn1 if lijn == 1 else day.lijn2
            target.append(PlannedOrder(
                order=r.order, product=make_product(r.order.art_nr), lijn=lijn,
                start=time.fromisoformat(r.start), eind=time.fromisoformat(r.eind),
                ombouw_voor=r.ombouw_voor,
            ))
    return day


def test_produced_days_are_dropped_not_replanned():
    """Once its production date has passed, a day is done — its orders must
    not come back as free work for a later run."""
    orders = [make_order("A", 100, WK31, lijn=1, order_nr="done")]
    state = _commit([_day(WK31, orders)], WK31)

    locked_days, free, _w = partition_locked_days(orders, state, today=WK33)
    assert locked_days == [] and free == []


def test_produced_order_stays_completed_after_its_day_is_rewritten_out_of_state():
    """Regression for the two-run resurrection seen for article 64224100."""
    produced = make_order(
        "64224100", 6000, WK31, lever=WK34, lijn=1, order_nr="2026-08-03"
    )
    state31_days = _commit([_day(WK31, [produced])], WK31)

    completed32 = roll_completed_orders([produced], state31_days, {}, today=WK32)
    locked32, free32, _ = partition_locked_days(
        [produced], state31_days, today=WK32, completed_state=completed32
    )
    assert locked32 == [] and free32 == []
    assert completed32[order_identity(produced)]["production_date"] == WK31.isoformat()

    # The elapsed WK31 day is no longer present in the rewritten rolling
    # state. One more run must still suppress the source order.
    state32_days: dict[str, dict] = {}
    completed33 = roll_completed_orders(
        [produced], state32_days, completed32, today=WK33
    )
    locked33, free33, _ = partition_locked_days(
        [produced], state32_days, today=WK33, completed_state=completed33
    )
    assert locked33 == [] and free33 == []
    assert order_identity(produced) in completed33


def test_a_cancelled_order_is_warned_about_not_silently_kept():
    orders = [make_order("A", 100, WK33, lijn=1, order_nr="gone")]
    state = _commit([_day(WK33, orders)], WK31)

    locked_days, free, warnings = partition_locked_days([], state, today=WK32)
    assert locked_days == [] and free == []
    assert len(warnings) == 1 and "niet meer in orders.json" in warnings[0]


# ── the day is the stored unit ───────────────────────────────────────────────

def test_two_chunks_on_the_same_day_and_line_both_survive():
    """
    Regression: the order-keyed format keyed on identity@day@lijn, so two
    chunks of ONE split order sharing a day AND line collided and one was
    silently destroyed at commit time — 77,984 units in one observed run.
    """
    chunks = [make_order("A", 2857, WK33, lever=WK34, lijn=2, order_nr="split"),
              make_order("A", 2143, WK33, lever=WK34, lijn=2, order_nr="split")]
    assert order_identity(chunks[0]) == order_identity(chunks[1])

    state = _commit([_day(WK33, chunks)], WK31)
    stored = state[WK33.isoformat()]["lijn2"]
    assert [e["aantal"] for e in stored] == [2857, 2143]

    whole = make_order("A", 5000, WK33, lever=WK34, lijn=2, order_nr="split")
    locked_days, free, _w = partition_locked_days([whole], state, today=WK33)
    assert [r.order.aantal for r in locked_days[0].lijn2] == [2857, 2143]
    assert free == []


def test_an_order_the_day_builder_dropped_is_never_committed():
    """
    Regression: the commit read the solver's order list, so a DAY-BUILD-DROP
    order was reported rejected AND written into plan_state — reappearing the
    next run on a day that had been declared frozen.
    """
    placed = make_order("A", 100, WK33, lijn=1, order_nr="placed")
    phantom = make_order("B", 100, WK33, lijn=1, order_nr="phantom")

    state = _commit([_day(WK33, [placed], dropped=[phantom])], WK31)
    identities = {e["identity"] for e in state[WK33.isoformat()]["lijn1"]}
    assert identities == {order_identity(placed)}

    _locked, free, _w = partition_locked_days([placed, phantom], state, today=WK33)
    assert [o.order_nr for o in free] == ["phantom"]


def test_a_frozen_days_warnings_survive_the_commit():
    """
    Regression: warnings were not stored, so a replayed frozen day reported
    zero hard errors while the schedule the floor runs still had them.
    """
    order = make_order("A", 100, WK33, lijn=1, order_nr="x")
    err = "[ERROR] AFSLUIT_VERBODEN: L1: PRODUCT A staat als laatste order van de dag"

    state = _commit([_day(WK33, [order], warnings=[err])], WK31)
    assert state[WK33.isoformat()]["warnings"] == [err]

    locked_days, _free, _w = partition_locked_days([order], state, today=WK33)
    assert locked_days[0].warnings == [err]


def test_the_committed_running_sequence_is_stored_not_re_derived():
    first = make_order("A", 100, WK33, lijn=1, order_nr="first")
    second = make_order("B", 100, WK33, lijn=1, order_nr="second")

    state = _commit([_day(WK33, [first, second])], WK31)
    assert [e["identity"] for e in state[WK33.isoformat()]["lijn1"]] == [
        order_identity(first), order_identity(second)
    ]

    locked_days, _free, _w = partition_locked_days([first, second], state, today=WK33)
    assert [r.order.order_nr for r in locked_days[0].lijn1] == ["first", "second"]


# ── split orders across the commit boundary ──────────────────────────────────

def test_a_split_order_straddling_the_boundary_reports_its_shortfall():
    """
    The in-window chunk is frozen (that guarantee comes first); the rest is
    neither locked nor free, so the shortfall is reported per order rather
    than absorbed silently. Forbidding the solver from splitting across
    commit_end removes the case entirely — until then this is the contract.
    """
    inside = make_order("A", 1742, WK33, lever=WK36, lijn=2, order_nr="split")
    outside = make_order("A", 2058, WK35, lever=WK36, lijn=2, order_nr="split")
    assert order_identity(inside) == order_identity(outside)

    state = _commit([_day(WK33, [inside]), _day(WK35, [outside])], WK31)
    assert list(state) == [WK33.isoformat()]           # only the in-window day

    whole = make_order("A", 3800, WK33, lever=WK36, lijn=2, order_nr="split")
    locked_days, free, warnings = partition_locked_days([whole], state, today=WK33)

    assert [r.order.aantal for r in locked_days[0].lijn2] == [1742]
    assert free == []
    assert len(warnings) == 1
    assert "1,742 van 3,800 stuks" in warnings[0]
    assert "2,058" in warnings[0]


# ── persistence ──────────────────────────────────────────────────────────────

def test_state_survives_a_save_load_round_trip(tmp_path):
    orders = [make_order("A", 100, WK33, lijn=1, order_nr="x")]
    state = {"frozen_until": WK35.isoformat(), "days": _commit([_day(WK33, orders)], WK31)}
    path = tmp_path / "plan_state.json"
    save_plan_state(state, path)
    assert load_plan_state(path) == {
        **state,
        "schema_version": 2,
        "completed_orders": {},
        "rejections": {},
        "warnings": [],
    }


def test_missing_state_file_means_nothing_is_frozen(tmp_path):
    state = load_plan_state(tmp_path / "does_not_exist.json")
    assert state == {
        "schema_version": 2,
        "frozen_until": None,
        "days": {},
        "completed_orders": {},
        "rejections": {},
        "warnings": [],
    }
    assert block_end(WK31, state["frozen_until"]) == WK31


# ── migration off the order-keyed format ─────────────────────────────────────

def _legacy_entry(order, lijn, start, eind, ombouw=20):
    return {
        "identity": order_identity(order),
        "productie_datum": order.productie_datum.isoformat(),
        "lijn": lijn, "aantal": order.aantal,
        "render": {"start": start, "eind": eind, "ombouw_voor": ombouw},
    }


def test_legacy_order_keyed_state_migrates_to_days(tmp_path):
    """The old {"frozen_until", "locked"} shape regroups into days on read,
    each line ordered by its committed start time."""
    late = make_order("A", 100, WK33, lijn=1, order_nr="late")
    early = make_order("B", 100, WK33, lijn=1, order_nr="early")
    other = make_order("C", 100, WK34, lijn=2, order_nr="other")
    path = tmp_path / "plan_state.json"
    path.write_text(json.dumps({
        "frozen_until": WK35.isoformat(),
        "locked": {
            "k1": _legacy_entry(late, 1, "14:00", "16:00"),
            "k2": _legacy_entry(early, 1, "06:00", "08:00"),
            "k3": _legacy_entry(other, 2, "06:00", "08:00"),
        },
    }), encoding="utf-8")

    state = load_plan_state(path)
    assert state["frozen_until"] == WK35.isoformat()
    assert sorted(state["days"]) == [WK33.isoformat(), WK34.isoformat()]
    assert [e["start"] for e in state["days"][WK33.isoformat()]["lijn1"]] == ["06:00", "14:00"]
    assert state["days"][WK33.isoformat()]["warnings"] == []

    locked_days, free, _w = partition_locked_days([late, early, other], state["days"], today=WK33)
    assert [r.order.order_nr for r in locked_days[0].lijn1] == ["early", "late"]
    assert free == []


def test_legacy_flat_state_file_is_still_readable(tmp_path):
    """Older files still were a bare {key: entry} dict with no frozen_until."""
    order = make_order("A", 100, WK33, lijn=1, order_nr="x")
    path = tmp_path / "plan_state.json"
    path.write_text(json.dumps({"k1": _legacy_entry(order, 1, "06:00", "08:00")}), encoding="utf-8")

    state = load_plan_state(path)
    assert state["frozen_until"] is None
    assert len(state["days"]) == 1


def test_a_legacy_entry_without_a_render_is_reported_not_guessed(tmp_path):
    """It cannot be replayed, so it becomes free work again — loudly."""
    order = make_order("A", 100, WK33, lijn=1, order_nr="x")
    entry = _legacy_entry(order, 1, "06:00", "08:00")
    del entry["render"]
    path = tmp_path / "plan_state.json"
    path.write_text(json.dumps({"frozen_until": None, "locked": {"k1": entry}}), encoding="utf-8")

    state = load_plan_state(path)
    assert state["days"] == {}
    assert len(state["warnings"]) == 1
    assert "geen opgeslagen rendering" in state["warnings"][0]


# ── why a frozen-out order was never made ────────────────────────────────────

AAF = "ARRIVED-AFTER-FREEZE: every day up to X is frozen."
DROP = "COMBINED-SCHEDULE-DROP: still not placeable after 3 reconciliation rounds."


def test_the_run_that_actually_tried_is_the_one_remembered():
    """
    Once the freeze passes an order's delivery date every later run reports
    ARRIVED-AFTER-FREEZE, which says only that it is now unreachable. The run
    that had a real go at it is the one that knows why.
    """
    keep = {"o"}
    h = merge_rejection_history({}, {"o": DROP}, week=32, keep=keep)
    h = merge_rejection_history(h, {"o": DROP}, week=33, keep=keep)
    assert h["o"] == {"eerste_week": 32, "laatste_week": 33, "pogingen": 2, "reason": DROP}

    # From here the order is frozen out — that must not overwrite the history.
    h = merge_rejection_history(h, {"o": AAF}, week=34, keep=keep)
    assert h["o"]["pogingen"] == 2 and h["o"]["reason"] == DROP


def test_history_is_pruned_to_orders_still_worth_remembering():
    h = merge_rejection_history({}, {"gone": DROP, "here": DROP}, week=32, keep={"gone", "here"})
    assert set(h) == {"gone", "here"}
    h = merge_rejection_history(h, {}, week=33, keep={"here"})
    assert set(h) == {"here"}


def test_the_explanation_names_the_span_and_the_last_reason():
    entry = {"eerste_week": 32, "laatste_week": 33, "pogingen": 2, "reason": DROP}
    text = explain_from_history(entry)
    assert "2 eerdere run(s)" in text
    assert "week 32 t/m 33" in text
    assert DROP in text

    one = explain_from_history({"eerste_week": 34, "laatste_week": 34, "pogingen": 1, "reason": DROP})
    assert "week 34" in one and "t/m" not in one


def test_no_history_adds_nothing():
    """A genuinely new order booked into a frozen week has nothing to explain."""
    assert explain_from_history(None) == ""
    assert explain_from_history({}) == ""


# ── identity ─────────────────────────────────────────────────────────────────

def test_identity_ignores_quantity_so_split_chunks_still_match():
    """A chunk carries a partial `aantal`; orders.json only ever has the whole
    one. Including quantity in the identity would orphan every split lock."""
    whole = make_order("A", 100, WK33, order_nr="x")
    chunk = make_order("A", 40, WK33, order_nr="x")
    assert order_identity(whole) == order_identity(chunk)


def test_identity_is_stable_when_the_production_date_moves():
    """Locking overwrites productie_datum, so identity must not depend on it."""
    a = make_order("A", 100, WK33, lever=WK34, order_nr="x")
    b = make_order("A", 100, WK35, lever=WK34, order_nr="x")
    assert order_identity(a) == order_identity(b)


def test_identity_prefers_unique_plan_number_for_sibling_lines():
    a = replace(make_order("A", 100, WK33, lever=WK34, order_nr="2026-01-01"), plan_nr="45001")
    b = replace(make_order("A", 100, WK33, lever=WK34, order_nr="2026-01-01"), plan_nr="45002")

    assert order_identity(a) == "plan:45001"
    assert order_identity(b) == "plan:45002"


def test_legacy_frozen_identity_still_resolves_after_plan_number_upgrade():
    order = replace(make_order("A", 100, WK33, lever=WK34, order_nr="old"), plan_nr="45001")
    legacy = f"{order.order_nr}#{order.art_nr}#{order.klant}#{order.lever_datum.isoformat()}"
    state = {
        WK33.isoformat(): {
            "lijn1": [{
                "identity": legacy, "aantal": 100, "start": "06:00",
                "eind": "06:10", "ombouw_voor": 0,
            }],
            "lijn2": [], "warnings": [],
        }
    }

    locked, free, warnings = partition_locked_days([order], state, WK33)

    assert not warnings
    assert len(locked) == 1
    assert free == []
