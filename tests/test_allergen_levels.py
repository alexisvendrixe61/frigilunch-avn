"""
The allergen classification column and the scale it actually uses.

"legende database" row 10 spells it out: "-  laagst  naar  ! ! ! ! !  Hoogst
/  X = sate productie". Seven levels, and the sate marker sits on top — these
tests pin that reading, because the parser used to drop X through to the
cross-contamination marker count and land the peanut products mid-scale.
"""
from __future__ import annotations

from src.constraints.allergen_rules import allergie_niveau_from_allergenen
from src.product_setup_loader import (
    _allergy_level_from_label,
    _allergy_level_from_x_count,
)

from conftest import NO_ALLERGENS


def test_sate_label_is_the_top_level():
    assert _allergy_level_from_label("X") == 6
    assert _allergy_level_from_label("x") == 6


def test_dash_is_the_clean_floor():
    assert _allergy_level_from_label("-") == 0


def test_bang_count_is_the_level_and_five_is_not_clipped_to_four():
    assert _allergy_level_from_label("!") == 1
    assert _allergy_level_from_label("! !") == 2
    assert _allergy_level_from_label("! ! !") == 3
    assert _allergy_level_from_label("! ! ! !") == 4
    assert _allergy_level_from_label("! ! ! ! !") == 5


def test_sate_outranks_every_bang_level():
    assert _allergy_level_from_label("X") > _allergy_level_from_label("! ! ! ! !")


def test_blank_falls_through_to_the_marker_count():
    assert _allergy_level_from_label("") is None
    assert _allergy_level_from_label("0") is None


def test_marker_count_never_claims_the_sate_level():
    all_x = {a: "X" for a in NO_ALLERGENS}
    assert _allergy_level_from_x_count(all_x) == 5
    assert allergie_niveau_from_allergenen(all_x) == 5


def test_marker_count_fallback_matches_its_duplicate_in_allergen_rules():
    for n in range(len(NO_ALLERGENS) + 1):
        markers = dict(NO_ALLERGENS)
        for a in list(markers)[:n]:
            markers[a] = "X"
        assert _allergy_level_from_x_count(markers) == allergie_niveau_from_allergenen(markers)
