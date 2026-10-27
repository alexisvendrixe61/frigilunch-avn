"""
tests/unit/test_constraints.py — one test per constraint rule.

Covers: changeover, allergen, device, sequencing, cuts.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from frigilunch.domain.models import Order, Product
from frigilunch.domain.constraints.changeover import (
    changeover_minutes,
    production_minutes,
    _schaal_formaat,
    _allergen_drop,
    _cross_contamination,
)
from frigilunch.domain.constraints.allergen import allergen_level, is_sate, mep_lead_days
from frigilunch.domain.constraints.device import scarce_devices_needed
from frigilunch.domain.constraints.sequencing import (
    mag_eerst,
    mag_laatst,
    moet_eerst,
    moet_laatst,
    positie_ok,
    apply_positie_regels,
)
from frigilunch.domain.cuts import build_no_good_cut, cuts_for_device_pair


# ── shared helpers ─────────────────────────────────────────────────────────────

def _product(
    art_nr: str = "P001",
    kapvorm: str = "A",
    schaal: str = "UK59",
    allergie: int = 0,
    allergenen: dict | None = None,
    snelheid: int = 100,
    opstart_moet: bool = False,
    opstart_kan_niet: bool = False,
    afsluit_moet: bool = False,
    afsluit_kan_niet: bool = False,
    toestellen: dict | None = None,
) -> Product:
    return Product(
        art_nr=art_nr, naam=f"P{art_nr}", schaal=schaal, kapvorm=kapvorm,
        snelheid=snelheid, allergie_niveau=allergie,
        allergenen=allergenen or {},
        opstart_moet=opstart_moet, opstart_kan_niet=opstart_kan_niet,
        afsluit_moet=afsluit_moet, afsluit_kan_niet=afsluit_kan_niet,
        toestellen=toestellen or {},
    )


def _order(art_nr: str = "P001", aantal: int = 100) -> Order:
    d = date(2026, 10, 7)
    return Order(order_nr="O001", art_nr=art_nr, klant="K", aantal=aantal, lever_datum=d, productie_datum=d)


_SETUP = {
    "ombouw_standaard": {"zelfde_formaat": 20, "ander_formaat": 30},
    "kapvorm_wissel_extra": {"andere_kapvorm": 15},
    "reiniging": {"allergie_daling": 30},
    "zelfde_machine_wissel": {"leonard": 40},
    "daesang_uitzondering": {"art_nr_combinaties": [{"van": "A", "naar": "B", "tijd": 10}]},
}


# ── changeover (O1) ────────────────────────────────────────────────────────────

def test_same_format_no_kapvorm_change():
    """Same scale family, same kapvorm → 20 min."""
    p1 = _product("P1", schaal="UK59A", kapvorm="A")
    p2 = _product("P2", schaal="UK59B", kapvorm="A")
    assert changeover_minutes(p1, p2, _SETUP) == 20


def test_different_format_costs_30():
    """Different scale families → 30 min."""
    p1 = _product("P1", schaal="UK59")
    p2 = _product("P2", schaal="C2325")
    assert changeover_minutes(p1, p2, _SETUP) == 30


def test_kapvorm_change_adds_cost():
    """Different kapvorm on same format → max(20, 15) = 20, but different format + kapvorm → 30."""
    p1 = _product("P1", schaal="UK59", kapvorm="A")
    p2 = _product("P2", schaal="C2325", kapvorm="B")
    # different format (30) + kapvorm (15) → max = 30
    assert changeover_minutes(p1, p2, _SETUP) == 30


def test_allergen_drop_costs_cleaning():
    """Allergen drop (level 2 → 1) → cleaning cost 30."""
    p1 = _product("P1", allergie=2)
    p2 = _product("P2", allergie=1)
    cost = changeover_minutes(p1, p2, _SETUP)
    assert cost >= 30


def test_daesang_exception_short_circuit():
    """Named article pair 'A'→'B' uses the override time (10)."""
    p1 = _product("A")
    p2 = _product("B")
    assert changeover_minutes(p1, p2, _SETUP) == 10


def test_same_machine_floor():
    """Both products use the leonard (scarce machine) → max(30, 40)=40."""
    p1 = _product("P1", schaal="UK59", kapvorm="A", toestellen={"leonard": True})
    p2 = _product("P2", schaal="C2325", kapvorm="B", toestellen={"leonard": True})
    assert changeover_minutes(p1, p2, _SETUP) == 40


def test_production_minutes_rounded_up():
    """V1: 50 units at 100/h = 30 min. 55 units → ceil to 35 min."""
    p = _product(snelheid=100)
    o55 = Order(order_nr="O", art_nr="P001", klant="K", aantal=55,
                lever_datum=date(2026, 10, 7), productie_datum=date(2026, 10, 7))
    assert production_minutes(o55, p) == 35


# ── allergen rules (A1–A5) ────────────────────────────────────────────────────

def test_allergen_level_returns_field():
    p = _product(allergie=3)
    assert allergen_level(p) == 3


def test_is_sate_level_6():
    p_sate = _product(allergie=6)
    p_normal = _product(allergie=5)
    assert is_sate(p_sate) is True
    assert is_sate(p_normal) is False


def test_mep_lead_days_returns_field():
    p = _product()
    p.mep_lead_days = 2
    assert mep_lead_days(p) == 2


def test_cross_contamination_detected():
    p1 = _product("P1", allergenen={"pinda": "-"})
    p2 = _product("P2", allergenen={"pinda": "X"})
    cross = _cross_contamination(p1, p2)
    assert "pinda" in cross


def test_no_cross_contamination_when_already_present():
    p1 = _product("P1", allergenen={"pinda": "X"})
    p2 = _product("P2", allergenen={"pinda": "X"})
    assert _cross_contamination(p1, p2) == []


# ── device rules (M1) ─────────────────────────────────────────────────────────

def test_scarce_device_detected():
    p = _product(toestellen={"leonard": True, "multifill": True})
    availability = {"leonard": 1, "multifill": 2}
    result = scarce_devices_needed(p, availability)
    assert "leonard" in result
    assert "multifill" not in result


def test_no_scarce_device_without_availability():
    p = _product(toestellen={"leonard": True})
    assert scarce_devices_needed(p, None) == set()


def test_scarce_device_no_toestel_needed():
    p = _product(toestellen={"leonard": False})
    availability = {"leonard": 1}
    assert scarce_devices_needed(p, availability) == set()


# ── sequencing (S2) ────────────────────────────────────────────────────────────

def test_mag_eerst_default_true():
    p = _product()
    assert mag_eerst(p) is True


def test_mag_eerst_false_when_opstart_kan_niet():
    p = _product(opstart_kan_niet=True)
    assert mag_eerst(p) is False


def test_mag_eerst_true_when_moet_overrides_kan_niet():
    p = _product(opstart_moet=True, opstart_kan_niet=True)
    assert mag_eerst(p) is True


def test_mag_laatst_default_true():
    p = _product()
    assert mag_laatst(p) is True


def test_mag_laatst_false_when_afsluit_kan_niet():
    p = _product(afsluit_kan_niet=True)
    assert mag_laatst(p) is False


def test_positie_ok_simple_sequence():
    p1 = _product("P1")
    p2 = _product("P2")
    assert positie_ok([p1, p2]) is True


def test_positie_ok_fails_when_opener_banned():
    p1 = _product("P1", opstart_kan_niet=True)
    p2 = _product("P2")
    assert positie_ok([p1, p2]) is False


def test_positie_ok_fails_when_closer_banned():
    p1 = _product("P1")
    p2 = _product("P2", afsluit_kan_niet=True)
    assert positie_ok([p1, p2]) is False


def test_apply_positie_regels_pins_moet_eerst():
    """A 'moet eerst' product must move to the front."""
    p_must_first = _product("P1", opstart_moet=True)
    p_normal = _product("P2")
    o1 = _order("P1")
    o2 = _order("P2")
    # Put the must-first product second
    result = apply_positie_regels([(o2, p_normal), (o1, p_must_first)])
    assert result[0][1].art_nr == "P1"


# ── cuts ──────────────────────────────────────────────────────────────────────

def test_build_no_good_cut_schema():
    """build_no_good_cut returns the expected dict shape."""
    d = date(2026, 10, 7)
    cut = build_no_good_cut(d, frozenset(["O001", "O002"]), {})
    assert cut["type"] == "no_good"
    assert cut["day_date"] == "2026-10-07"
    assert "O001" in cut["order_ids"]
    assert "O002" in cut["order_ids"]


def test_build_no_good_cut_empty_raises():
    d = date(2026, 10, 7)
    with pytest.raises(ValueError, match="empty"):
        build_no_good_cut(d, frozenset(), {})


def test_device_pair_cut_schema():
    """cuts_for_device_pair returns the expected dict shape."""
    d = date(2026, 10, 7)
    cut = cuts_for_device_pair(d, ("O001", "O002"), "leonard", {})
    assert cut["type"] == "device_pair"
    assert cut["device"] == "leonard"
    assert sorted(cut["order_ids"]) == ["O001", "O002"]
