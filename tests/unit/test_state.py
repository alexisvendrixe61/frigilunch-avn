"""
tests/unit/test_state.py

Tests for json_state.py (adapters) and domain/state.py (pure functions):
  1. write→read round-trip preserves the state
  2. crash simulation (orphaned .tmp) → old state survives intact
  3. missing file → empty baseline state returned (no exception)
  4. wrong schema version → StateSchemaError raised
  5-8. domain state: freeze_window, is_frozen, load_frozen_orders
"""
from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path

import pytest

from frigilunch.adapters.json_state import (
    SCHEMA_VERSION,
    StateSchemaError,
    read_state,
    write_state,
)
from frigilunch.domain.models import DaySchedule, Order, PlannedOrder, Product
from frigilunch.domain.state import freeze_window, is_frozen, load_frozen_orders


# ── test 1: round-trip ────────────────────────────────────────────────────────

def test_write_read_round_trip(tmp_path: Path):
    """Written state can be read back without loss."""
    path = tmp_path / "plan_state.json"
    state = {
        "schema_version": SCHEMA_VERSION,
        "frozen_until": "2026-11-01",
        "committed_days": ["2026-10-07", "2026-10-08"],
        "completed_orders": [],
    }

    write_state(path, state)
    restored = read_state(path)

    assert restored["frozen_until"] == "2026-11-01"
    assert restored["committed_days"] == ["2026-10-07", "2026-10-08"]
    assert restored["schema_version"] == SCHEMA_VERSION


# ── test 2: crash safety ──────────────────────────────────────────────────────

def test_crash_leaves_old_state_intact(tmp_path: Path):
    """An orphaned .tmp file must not corrupt the good state file."""
    path = tmp_path / "plan_state.json"
    good_state = {
        "schema_version": SCHEMA_VERSION,
        "frozen_until": "2026-10-28",
        "committed_days": [],
        "completed_orders": [],
    }
    write_state(path, good_state)

    # Simulate a crash: write a bad .tmp without renaming it
    tmp = path.with_suffix(".tmp")
    tmp.write_text('{"corrupted": true}', encoding="utf-8")

    # The good file must still be readable and correct
    restored = read_state(path)
    assert restored["frozen_until"] == "2026-10-28"
    assert "corrupted" not in restored


# ── test 3: missing file returns empty state ──────────────────────────────────

def test_missing_file_returns_empty_state(tmp_path: Path):
    """read_state on a non-existent path returns the empty baseline, not an error."""
    path = tmp_path / "nonexistent.json"

    state = read_state(path)

    assert state["schema_version"] == SCHEMA_VERSION
    assert state["frozen_until"] is None
    assert state["committed_days"] == []


# ── test 4: schema version mismatch raises ────────────────────────────────────

def test_wrong_schema_version_raises(tmp_path: Path):
    """A file with a different schema_version must raise StateSchemaError."""
    path = tmp_path / "plan_state.json"
    bad_state = {"schema_version": 99, "committed_days": []}
    path.write_text(json.dumps(bad_state), encoding="utf-8")

    with pytest.raises(StateSchemaError, match="schema_version=99"):
        read_state(path)


# ── domain/state.py tests ─────────────────────────────────────────────────────

def _make_planned_order(plan_nr: str = "40781") -> PlannedOrder:
    order = Order(
        order_nr="2026-10-01 00:00:00",
        art_nr="P001",
        klant="KLANT",
        aantal=100,
        lever_datum=date(2026, 10, 9),
        productie_datum=date(2026, 10, 7),
        plan_nr=plan_nr,
    )
    product = Product(art_nr="P001", naam="Test", schaal="UK59", kapvorm="A", snelheid=1500)
    return PlannedOrder(order=order, product=product, lijn=1,
                        start=time(6, 0), eind=time(8, 0), ombouw_voor=0)


def test_freeze_window_adds_order_key():
    """freeze_window must record the planned order's key in frozen_keys."""
    po = _make_planned_order("40781")
    day = DaySchedule(datum=date(2026, 10, 7), lijn1=[po])
    new_state = freeze_window({}, week=40, year=2026, schedule=[day])
    assert "plan:40781" in new_state["frozen_keys"]


def test_is_frozen_true_after_freeze():
    """is_frozen returns True for an order that was committed."""
    po = _make_planned_order("40781")
    day = DaySchedule(datum=date(2026, 10, 7), lijn1=[po])
    state = freeze_window({}, week=40, year=2026, schedule=[day])
    assert is_frozen(state, "plan:40781") is True


def test_is_frozen_false_for_unknown_key():
    """is_frozen returns False for an order not in the state."""
    assert is_frozen({}, "plan:99999") is False


def test_load_frozen_orders_returns_future_orders():
    """load_frozen_orders returns orders whose productie_datum >= current week's Monday."""
    po = _make_planned_order("40781")
    # productie_datum is 2026-10-07 (wk41); current_week=40 → Monday=2026-09-28
    day = DaySchedule(datum=date(2026, 10, 7), lijn1=[po])
    state = freeze_window({}, week=40, year=2026, schedule=[day])
    orders = load_frozen_orders(state, current_week=40, year=2026)
    assert len(orders) == 1
    assert orders[0].plan_nr == "40781"


def test_load_frozen_orders_empty_when_nothing_frozen():
    """load_frozen_orders returns empty list when state has no frozen_keys."""
    orders = load_frozen_orders({}, current_week=40, year=2026)
    assert orders == []
