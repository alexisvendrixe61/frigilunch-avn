"""
What the loader derives from the spreadsheet.

These run against the REAL products.json, because the value of the derivation
is entirely in whether it matches the source data.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.loader import load_products

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def products():
    prods, _ = load_products(ROOT / "data" / "generated" / "products.json")
    return prods


def test_the_two_exclusion_flags_are_never_both_set(products):
    """
    A 0/0 pair in the sheet means the columns were not filled in, not that the
    product is unmakeable — so _derive_line_info sets at most one exclusion.
    permitted_lines relies on this: given both, it would arbitrarily answer L1.
    """
    both = [p.art_nr for p in products.values() if p.l1_uitgesloten and p.l2_uitgesloten]
    assert both == [], f"products excluded from BOTH lines: {both[:10]}"


def test_alleen_and_uitgesloten_never_contradict(products):
    for p in products.values():
        assert not (p.alleen_l1 and p.l1_uitgesloten), p.art_nr
        assert not (p.alleen_l2 and p.l2_uitgesloten), p.art_nr


def test_every_product_can_run_somewhere(products):
    from src.constraints import permitted_lines

    for p in products.values():
        assert permitted_lines(p), f"{p.art_nr} has no permitted line"


def test_every_product_has_at_least_one_production_day(products):
    empty = [p.art_nr for p in products.values() if not p.toegelaten_dagen]
    assert empty == [], f"products with no allowed weekday: {empty[:10]}"


def test_allowed_days_are_weekdays_only(products):
    valid = {"ma", "di", "wo", "do", "vr"}
    for p in products.values():
        assert set(p.toegelaten_dagen) <= valid, (p.art_nr, p.toegelaten_dagen)


def test_speed_is_always_positive(products):
    """calculate_duration divides by it."""
    bad = [p.art_nr for p in products.values() if p.snelheid <= 0]
    assert bad == [], f"products with non-positive speed: {bad[:10]}"


def test_allergen_level_is_in_range(products):
    for p in products.values():
        assert 0 <= p.allergie_niveau <= 6, (p.art_nr, p.allergie_niveau)


def test_constraint_provenance_is_available_for_generated_products(products):
    with_provenance = [p for p in products.values() if p.constraint_provenance]
    assert with_provenance
    for product in with_provenance:
        assert set(product.constraint_provenance) >= {
            "line", "weekdays", "speed", "position", "allergens", "devices"
        }
        assert set(product.constraint_provenance.values()) <= {"explicit", "inferred"}
