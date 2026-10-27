"""
tests/unit/test_oracle.py

Three invariants the oracle must hold on every input:
  1. Same input → same output (determinism)
  2. No planned order ends after shift_end
  3. Empty order list → empty DaySchedule with no errors
"""
from __future__ import annotations

from datetime import date, time

import pytest

from frigilunch.domain.models import Order, Product
from frigilunch.domain.oracle import schedule_day


# ── shared fixtures ───────────────────────────────────────────────────────────

def _make_product(art_nr: str, allergie: int = 0, snelheid: int = 100) -> Product:
    return Product(
        art_nr=art_nr,
        naam=f"Product {art_nr}",
        schaal="UK59",
        kapvorm="A",
        snelheid=snelheid,
    )


def _make_order(
    order_nr: str,
    art_nr: str,
    aantal: int,
    productie_datum: date,
    lijn: int = 0,
) -> Order:
    return Order(
        order_nr=order_nr,
        art_nr=art_nr,
        klant="TestKlant",
        aantal=aantal,
        lever_datum=productie_datum,
        productie_datum=productie_datum,
        lijn=lijn,
    )


def _base_config() -> dict:
    return {
        "setup_matrix": {
            "ombouw_standaard": {"zelfde_formaat": 20, "ander_formaat": 30},
            "kapvorm_wissel_extra": {"andere_kapvorm": 15},
            "reiniging": {"allergie_daling": 30},
            "zelfde_machine_wissel": {},
            "daesang_uitzondering": {"art_nr_combinaties": []},
        },
        "line_start": {1: time(6, 0), 2: time(6, 30)},
        "shift_end": time(22, 30),
    }


def _three_order_input() -> tuple[list[Order], dict[str, Product], date]:
    """Three small orders for a Wednesday — all fit within the shift."""
    datum = date(2026, 10, 7)  # Wednesday
    products = {
        "P001": _make_product("P001", allergie=0),
        "P002": _make_product("P002", allergie=1),
    }
    orders = [
        _make_order("O001", "P001", 500, datum, lijn=1),
        _make_order("O002", "P002", 500, datum, lijn=1),
        _make_order("O003", "P001", 500, datum, lijn=2),
    ]
    return orders, products, datum


# ── test 1: determinism ───────────────────────────────────────────────────────

def test_oracle_is_deterministic():
    """Calling schedule_day twice with identical input yields identical output."""
    orders, products, datum = _three_order_input()
    config = _base_config()

    result_a = schedule_day(orders, products, datum, config)
    result_b = schedule_day(orders, products, datum, config)

    assert len(result_a.lijn1) == len(result_b.lijn1)
    assert len(result_a.lijn2) == len(result_b.lijn2)
    assert len(result_a.dropped) == len(result_b.dropped)

    for po_a, po_b in zip(result_a.lijn1, result_b.lijn1):
        assert po_a.start == po_b.start
        assert po_a.eind == po_b.eind
        assert po_a.order.order_nr == po_b.order.order_nr


# ── test 2: shift window ──────────────────────────────────────────────────────

def test_oracle_respects_shift_window():
    """No planned order ends after shift_end (22:30 on a normal day)."""
    datum = date(2026, 10, 7)  # Wednesday
    config = _base_config()
    shift_end = config["shift_end"]

    # Fill with enough orders to approach shift_end
    products = {"P001": _make_product("P001", snelheid=10)}  # slow → long runs
    orders = [
        _make_order(f"O{i:03d}", "P001", 500, datum, lijn=1)
        for i in range(20)
    ]

    result = schedule_day(orders, products, datum, config)

    for po in result.lijn1 + result.lijn2:
        end_min = po.eind.hour * 60 + po.eind.minute
        limit_min = shift_end.hour * 60 + shift_end.minute
        assert end_min <= limit_min, (
            f"Order {po.order.order_nr} ends at {po.eind} > shift_end {shift_end}"
        )


# ── test 3: empty day ─────────────────────────────────────────────────────────

def test_oracle_empty_day():
    """Empty order list → empty DaySchedule, no exceptions, no warnings."""
    datum = date(2026, 10, 7)
    config = _base_config()

    result = schedule_day([], {}, datum, config)

    assert result.lijn1 == []
    assert result.lijn2 == []
    assert result.dropped == []
    assert result.datum == datum
    # No errors from an empty day
    assert not any("[ERROR]" in w for w in result.warnings)
