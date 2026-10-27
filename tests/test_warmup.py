"""
Warm-up week selection.

A cold run may not place anything before plan_start, so an order delivering
early in the target week loses most of its 14-day production window and that
week reads as far fuller than it really is. `--warmup` re-runs the preceding
weeks first so the rolling lock is built the way a real weekly cadence builds
it. These tests cover the week arithmetic, which is where the edge cases are —
the ISO calendar, not the scheduler.
"""
from __future__ import annotations

from datetime import date, timedelta

from create_plan import iso_weeks_before
from src.plan_state import COMMIT_WEEKS


def test_the_three_weeks_before_a_normal_week():
    assert iso_weeks_before(2026, 36, 3) == [(2026, 33), (2026, 34), (2026, 35)]


def test_oldest_first_so_the_lock_chain_builds_forward():
    """Each warm-up run must inherit what the previous one committed."""
    weeks = iso_weeks_before(2026, 36, 3)
    assert weeks == sorted(weeks)


def test_crossing_the_year_boundary():
    """2026 is a 53-week ISO year — plain subtraction on week numbers breaks here."""
    assert iso_weeks_before(2027, 2, 3) == [(2026, 52), (2026, 53), (2027, 1)]
    assert iso_weeks_before(2027, 1, 3) == [(2026, 51), (2026, 52), (2026, 53)]


def test_zero_warmup_is_no_weeks():
    assert iso_weeks_before(2026, 36, 0) == []


def test_every_returned_week_is_exactly_one_week_apart():
    weeks = iso_weeks_before(2027, 3, 5)
    mondays = [date.fromisocalendar(y, w, 1) for y, w in weeks]
    assert all(b - a == timedelta(weeks=1) for a, b in zip(mondays, mondays[1:]))


def test_the_default_keeps_the_target_week_out_of_the_cold_run():
    """
    A run for week W newly solves week W + COMMIT_WEEKS - 1 — everything before
    that is already frozen by the previous run. So the TARGET week is decided by
    the run COMMIT_WEEKS - 1 weeks before it, and that run must not be the first
    (cold) one. That needs COMMIT_WEEKS warm-up weeks.
    """
    target = 36
    weeks = iso_weeks_before(2026, target, COMMIT_WEEKS)
    first_run = weeks[0][1]
    decider = target - (COMMIT_WEEKS - 1)
    assert decider > first_run, "the run deciding the target week is the cold one"


def test_one_warmup_week_less_would_leave_the_target_week_cold_decided():
    """Why the default is COMMIT_WEEKS and not COMMIT_WEEKS - 1."""
    target = 36
    too_few = iso_weeks_before(2026, target, COMMIT_WEEKS - 1)
    assert too_few[0][1] == target - (COMMIT_WEEKS - 1)


def test_warmup_weeks_land_on_real_mondays():
    for y, w in iso_weeks_before(2026, 36, COMMIT_WEEKS):
        assert date.fromisocalendar(y, w, 1).weekday() == 0
