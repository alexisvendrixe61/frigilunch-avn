"""
tests/unit/test_adapters.py

Unit tests for the excel_orders and master_data adapters.
Excel loading is tested via mocking so real files are not required.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from frigilunch.adapters.excel_orders import _dedup, _plan_nr, _remark, _parse_row
from frigilunch.adapters.master_data import validate_master
from frigilunch.domain.models import Order, Product


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_order(order_nr: str = "ORD_1", art_nr: str = "P001") -> Order:
    return Order(
        order_nr=order_nr,
        art_nr=art_nr,
        klant="KLANT",
        aantal=100,
        lever_datum=date(2026, 10, 7),
        productie_datum=date(2026, 10, 6),
    )


def _make_product(art_nr: str = "P001", snelheid: int = 1500) -> Product:
    return Product(art_nr=art_nr, naam="Test", schaal="UK59", kapvorm="A", snelheid=snelheid)


# ── test _remark ──────────────────────────────────────────────────────────────

def test_remark_none_returns_empty():
    assert _remark(None) == ""


def test_remark_zero_sentinel_returns_empty():
    assert _remark("0") == ""
    assert _remark(0.0) == ""


def test_remark_real_text_is_preserved():
    assert _remark("let op: allergie") == "let op: allergie"


# ── test _plan_nr ─────────────────────────────────────────────────────────────

def test_plan_nr_float_strips_decimal():
    assert _plan_nr(40781.0) == "40781"


def test_plan_nr_none_returns_empty():
    assert _plan_nr(None) == ""


# ── test _dedup ───────────────────────────────────────────────────────────────

def test_dedup_removes_exact_duplicate():
    order = _make_order()
    result = _dedup([order, order])
    assert len(result) == 1


def test_dedup_preserves_distinct_orders():
    o1 = _make_order("ORD_1", "P001")
    o2 = _make_order("ORD_2", "P002")
    result = _dedup([o1, o2])
    assert len(result) == 2


# ── test validate_master ──────────────────────────────────────────────────────

def test_validate_master_no_warnings_for_valid_data():
    products = {"P001": _make_product("P001", snelheid=1500)}
    orders = [_make_order("ORD_1", "P001")]
    warnings = validate_master(products, orders)
    assert warnings == []


def test_validate_master_warns_unknown_product():
    products = {}
    orders = [_make_order("ORD_1", "P999")]
    warnings = validate_master(products, orders)
    assert any("P999" in w for w in warnings)


def test_validate_master_warns_missing_speed():
    products = {"P001": _make_product("P001", snelheid=1000)}
    orders = [_make_order("ORD_1", "P001")]
    warnings = validate_master(products, orders)
    assert any("P001" in w for w in warnings)
    assert any("speed" in w.lower() for w in warnings)


def test_validate_master_deduplicates_unknown_warnings():
    """Two orders with the same unknown art_nr → only one warning."""
    products = {}
    orders = [_make_order("ORD_1", "P999"), _make_order("ORD_2", "P999")]
    warnings = validate_master(products, orders)
    assert sum(1 for w in warnings if "P999" in w) == 1
