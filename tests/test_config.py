"""
The shift window and the planning cap are two different numbers, and the
whole point of src/config.py is that they can no longer drift apart.
"""
from __future__ import annotations

from datetime import date, time

from frigilunch.adapters import config


def test_line_start_times():
    assert config.line_start(1) == time(6, 0)
    assert config.line_start(2) == time(6, 30)


def test_friday_shift_ends_earlier(monday, friday):
    assert config.shift_end(monday) == time(22, 30)
    assert config.shift_end(friday) == time(20, 30)
    assert config.day_type(monday) == "norm"
    assert config.day_type(friday) == "vr"


def test_window_minutes(monday, friday):
    assert config.window_minutes(monday, 1) == 990   # 06:00-22:30
    assert config.window_minutes(monday, 2) == 960   # 06:30-22:30
    assert config.window_minutes(friday, 1) == 870   # 06:00-20:30
    assert config.window_minutes(friday, 2) == 840   # 06:30-20:30


def test_planning_cap_matches_capacity_json(monday, friday):
    """
    The caps must be whatever capacity.json says, exactly.

    These were 900/870/750/720 — window-minus-a-90/120-minute-reserve — from
    before config.py existed, and this test hardcoded them. The reserve has
    since been measured and removed (see below), so pinning the old numbers
    would now fail for a deliberate change rather than catch a drift. Read
    them from the config instead: the drift this guards against is between
    `planning_cap` and `capacity_minuten`, not against a remembered value.
    """
    caps = config.planning_caps()
    assert config.planning_cap(monday, 1) == caps["norm"][1]
    assert config.planning_cap(monday, 2) == caps["norm"][2]
    assert config.planning_cap(friday, 1) == caps["vr"][1]
    assert config.planning_cap(friday, 2) == caps["vr"][2]


def test_planning_cap_never_exceeds_the_window(monday, friday):
    """
    A cap may equal its window but must never exceed it.

    This asserted STRICTLY below, to keep a reserve the assignment solver
    could not book — protection against work that legally fits the day reading
    as over 100% utilized. Relaxed to `<=` because the reserve is now zero,
    measured: 4 runs at reserve 0 against 3 at reserve 90 cut rejected minutes
    32% (13,923 -> 9,503 mean, non-overlapping) and moved 238,625 more units.

    Equality is still safe for the bug the strict version guarded. The
    dashboard divides by `window_minutes` (see dashboard.cap_for), never by
    the cap, and `_schedule_lijn` refuses any order finishing after shift end
    — so utilization can touch 100% but cannot pass it. Exceeding the window
    would be a real defect and is still caught.

    Removing the reserve does cost ~1 order per run that the day builder
    cannot fit; those are now reported as DAY-BUILD-DROP rejections rather
    than silently counted as scheduled (see create_plan.py's reconciliation).
    """
    for d in (monday, friday):
        for lijn in (1, 2):
            assert 0 < config.planning_cap(d, lijn) <= config.window_minutes(d, lijn)


def test_device_availability_excludes_comment_keys():
    devices = config.device_availability()
    assert all(not k.startswith("_") for k in devices)
    assert all(isinstance(v, int) for v in devices.values())
    assert devices["multifill"] == 1    # scarce: can cause an L1/L2 conflict
    assert devices["gernal"] == 2       # two since the 02/10 meeting: never conflicts
    assert devices["leonard"] == 2
    assert devices["turbo"] >= 2        # plentiful: never conflicts


def test_production_timing_uses_soft_delivery_proximity_not_a_hard_earliest_day():
    t = config.production_timing()
    assert t["min_days"] >= 0
    assert t["proximity_grace_days"] >= 0
    assert "max_days" not in t
