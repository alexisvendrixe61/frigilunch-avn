"""
The VERIFY backstop.

`validate_schedule` never fires during a normal run — the builder prevents
every violation it looks for. That is exactly why it needs its own tests: the
only way it gets exercised is when something upstream has gone wrong, which is
the moment you need it to be right.

Schedules here are constructed directly rather than through `schedule_day`,
because `schedule_day` refuses to produce the violations being tested.
"""
from __future__ import annotations

from datetime import date, time

import pytest

from src.models import DaySchedule, PlannedOrder
from src.validator import validate_schedule
from tests.conftest import make_order, make_product

MONDAY = date(2026, 7, 27)


def _po(product, lijn, start=(6, 0), eind=(8, 0), ombouw=0, warnings=None):
    return PlannedOrder(
        order=make_order(product.art_nr, 500, MONDAY, lijn=lijn),
        product=product, lijn=lijn,
        start=time(*start), eind=time(*eind),
        ombouw_voor=ombouw, warnings=list(warnings or []), man=1,
    )


def _validate(lijn1=(), lijn2=(), datum=MONDAY, devices=None):
    schedule = DaySchedule(datum=datum, lijn1=list(lijn1), lijn2=list(lijn2))
    return validate_schedule(schedule, {}, devices)


def _codes(errors):
    return [e.code for e in errors]


# ── line assignment ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("flags,lijn", [
    ({"alleen_l1": True}, 2),
    ({"alleen_l2": True}, 1),
    ({"l1_uitgesloten": True}, 1),
    ({"l2_uitgesloten": True}, 2),
])
def test_an_order_on_a_forbidden_line_is_an_error(flags, lijn):
    product = make_product("X", **flags)
    po = _po(product, lijn)
    errors = _validate(lijn1=[po] if lijn == 1 else [], lijn2=[po] if lijn == 2 else [])
    assert "LIJN_ASSIGNMENT" in _codes(errors)


@pytest.mark.parametrize("flags,lijn", [
    ({"alleen_l1": True}, 1),
    ({"alleen_l2": True}, 2),
    ({"l1_uitgesloten": True}, 2),
    ({"l2_uitgesloten": True}, 1),
    ({}, 1),
    ({}, 2),
])
def test_an_order_on_a_permitted_line_is_not(flags, lijn):
    product = make_product("X", **flags)
    po = _po(product, lijn)
    errors = _validate(lijn1=[po] if lijn == 1 else [], lijn2=[po] if lijn == 2 else [])
    assert "LIJN_ASSIGNMENT" not in _codes(errors)


def test_the_validator_agrees_with_the_builder_on_every_flag_combination():
    """
    Both ask constraints.line_rules.permitted_lines, so they cannot disagree.
    They used to: the validator re-derived the answer from the four flags
    inline and flagged a product excluded from BOTH lines on either line,
    while permitted_lines allows it on L1.
    """
    from src.constraints import permitted_lines

    flag_names = ("alleen_l1", "alleen_l2", "l1_uitgesloten", "l2_uitgesloten")
    for bits in range(16):
        flags = {n: bool(bits >> i & 1) for i, n in enumerate(flag_names)}
        product = make_product("X", **flags)
        allowed = permitted_lines(product)
        for lijn in (1, 2):
            po = _po(product, lijn)
            errors = _validate(lijn1=[po] if lijn == 1 else [], lijn2=[po] if lijn == 2 else [])
            flagged = "LIJN_ASSIGNMENT" in _codes(errors)
            assert flagged == (lijn not in allowed), (flags, lijn, allowed)


# ── allergens ────────────────────────────────────────────────────────────────

def test_an_allergen_drop_without_cleaning_is_an_error():
    hi = make_product("HI", allergie_niveau=3)
    lo = make_product("LO", allergie_niveau=0)
    errors = _validate(lijn1=[_po(hi, 1), _po(lo, 1, start=(8, 0), eind=(10, 0))])
    assert "ALLERGIE_DALING" in _codes(errors)


def test_an_allergen_drop_the_scheduler_cleaned_for_is_not_an_error():
    """The builder marks the order it planned cleaning for; the validator
    trusts that mark and downgrades to INFO."""
    hi = make_product("HI", allergie_niveau=3)
    lo = make_product("LO", allergie_niveau=0)
    cleaned = _po(lo, 1, start=(8, 30), eind=(10, 0), ombouw=30,
                  warnings=["Allergie-daling niv.3->0: reiniging 30 min"])
    errors = _validate(lijn1=[_po(hi, 1), cleaned])
    codes = _codes(errors)
    assert "ALLERGIE_DALING" not in codes
    assert "ALLERGIE_DALING_MET_REINIGING" in codes


def test_a_rising_allergen_level_is_never_flagged():
    lo = make_product("LO", allergie_niveau=0)
    hi = make_product("HI", allergie_niveau=3)
    errors = _validate(lijn1=[_po(lo, 1), _po(hi, 1, start=(8, 0), eind=(10, 0))])
    assert "ALLERGIE_DALING" not in _codes(errors)


def test_each_line_is_checked_independently():
    """Allergen levels rise per LINE — L2 starting low after L1 ended high is
    not a drop."""
    hi = make_product("HI", allergie_niveau=3)
    lo = make_product("LO", allergie_niveau=0)
    errors = _validate(lijn1=[_po(hi, 1)], lijn2=[_po(lo, 2)])
    assert "ALLERGIE_DALING" not in _codes(errors)


# ── the other backstops ──────────────────────────────────────────────────────

def test_running_past_the_shift_end_is_an_error():
    po = _po(make_product("X"), 1, start=(20, 0), eind=(23, 0))
    assert "SHIFT_OVERSCHRIJDING" in _codes(_validate(lijn1=[po]))


def test_friday_uses_the_earlier_shift_end():
    product = make_product("X")
    late = _po(product, 1, start=(19, 0), eind=(21, 0))
    assert "SHIFT_OVERSCHRIJDING" in _codes(_validate(lijn1=[late], datum=date(2026, 7, 31)))
    assert "SHIFT_OVERSCHRIJDING" not in _codes(_validate(lijn1=[late]))   # fine on a Monday


def test_a_forbidden_weekday_is_an_error():
    po = _po(make_product("X", dagen=["di", "wo"]), 1)
    assert "DAG_RESTRICTIE" in _codes(_validate(lijn1=[po]))


def test_a_shared_single_unit_machine_used_by_both_lines_is_an_error():
    a = make_product("A", devices=["leonard"])
    b = make_product("B", devices=["leonard"])
    errors = _validate(lijn1=[_po(a, 1)], lijn2=[_po(b, 2)], devices={"leonard": 1})
    assert "TOESTEL_CONFLICT" in _codes(errors)


def test_a_plentiful_machine_used_by_both_lines_is_fine():
    a = make_product("A", devices=["turbo"])
    b = make_product("B", devices=["turbo"])
    errors = _validate(lijn1=[_po(a, 1)], lijn2=[_po(b, 2)], devices={"turbo": 8})
    assert "TOESTEL_CONFLICT" not in _codes(errors)


def test_a_clean_schedule_produces_no_errors():
    a = make_product("A", allergie_niveau=0)
    b = make_product("B", allergie_niveau=1, kapvorm=a.kapvorm)
    errors = _validate(lijn1=[_po(a, 1), _po(b, 1, start=(8, 0), eind=(10, 0))])
    assert [e for e in errors if e.severity == "ERROR"] == []
