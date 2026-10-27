"""
config.py — the one place capacity.json is read, and the one definition of
what a production day looks like.

Before this module existed, four separate modules opened capacity.json at
import time and the shift END times weren't in it at all — they were
hardcoded as `time(20, 30) if vr else time(22, 30)` literals in BOTH
`scheduler.py` and `validator.py`, so the builder and its own validator could
silently drift apart.

## Window vs. planning cap — they are not the same number, on purpose

Two different limits, and conflating them is what made utilization able to
report over 100%:

  SHIFT WINDOW   the hard bound. `schedule_day` refuses any order that would
                 end after it. L1 06:00-22:30, L2 06:30-22:30, both to 20:30
                 on Friday.

  PLANNING CAP   what the day/line ASSIGNMENT solver is allowed to book into
                 a day. Deliberately smaller than the window: the last stretch
                 is held back as reserve, because the solver books against an
                 approximate per-order setup buffer and only finds out the
                 real sequence-dependent changeover cost when the oracle runs.

The reserve is exactly what reconciles the two sets of numbers that used to
look contradictory:

    L1 normal   06:00-22:30 = 990 - 90  = 900   \\
    L2 normal   06:30-22:30 = 960 - 90  = 870    |  the capacity_minuten
    L1 friday   06:00-20:30 = 870 - 120 = 750    |  values in capacity.json
    L2 friday   06:30-20:30 = 840 - 120 = 720   /

So `capacity_minuten` was never wrong — it was window-minus-reserve, with the
window and the reserve both undocumented. Here they are declared, and the caps
are DERIVED, so the two can no longer disagree.

Set `capacity_minuten` explicitly in capacity.json to override the derived
value (a mismatch is reported, never silently accepted).
"""
from __future__ import annotations

import json
from datetime import date, time
from functools import lru_cache
from pathlib import Path

CAPACITY_FILE = Path(__file__).parent.parent / "data" / "capacity.json"

# Line start times, and shift end per day type. These are the HARD bound —
# `schedule_day` drops anything that would run past `shift_end`.
_LINE_START = {1: time(6, 0), 2: time(6, 30)}
_SHIFT_END = {"norm": time(22, 30), "vr": time(20, 30)}

# Minutes of the window held back from the assignment solver, per day type.
#
# Was 90 (120 on Friday) — reverse-engineered to reproduce the 900/870/750/720
# that capacity.json had always carried, whose own origin is not recorded
# anywhere. Now 0: the solver may book the whole shift.
#
# Measured on wk32 (--dry-run --reset, 4 runs at 0 against 3 at 90): rejected
# minutes 13,923 -> 9,503 (-32%, arms non-overlapping), 238,625 more units
# scheduled, L1 +10 points. The reserve was the single largest source of
# unbookable time in the plan.
#
# It is not free. Without it the day builder fails to fit roughly one order
# per run, where before it had 90 minutes of slack to absorb a changeover that
# turned out worse than stage 1's per-order estimate. Those orders are no
# longer lost silently — create_plan.py reconciles the solver's list against
# the built day plans and reports the difference as DAY-BUILD-DROP.
#
# Restore to {"norm": 90, "vr": 120} (and capacity_minuten to 900/870/750/720)
# if that per-run manual exception costs more than the recovered volume.
_PLANNING_RESERVE = {"norm": 0, "vr": 0}


def day_type(d: date) -> str:
    """'vr' on Fridays, 'norm' otherwise. The only day-shape distinction."""
    return "vr" if d.weekday() == 4 else "norm"


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def line_start(lijn: int) -> time:
    """Earliest production start for a line. L1 06:00, L2 06:30."""
    return _LINE_START.get(lijn, _LINE_START[2])


def shift_end(d: date) -> time:
    """Latest an order may finish on this day. 20:30 Friday, 22:30 otherwise."""
    return _SHIFT_END[day_type(d)]


def window_minutes(d: date, lijn: int) -> int:
    """Full length of the shift window for this line on this day."""
    return _minutes(shift_end(d)) - _minutes(line_start(lijn))


@lru_cache(maxsize=1)
def _raw() -> dict:
    with open(CAPACITY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def planning_caps() -> dict[str, dict[int, int]]:
    """
    {"norm": {1: .., 2: ..}, "vr": {1: .., 2: ..}} — minutes the assignment
    solver may book per (day type, line).

    Derived from the shift window minus the planning reserve. If capacity.json
    carries an explicit `capacity_minuten` block it wins, but any disagreement
    with the derived value is reported so the two can't drift unnoticed.
    """
    derived = {
        dt: {
            lijn: _minutes(_SHIFT_END[dt]) - _minutes(_LINE_START[lijn]) - _PLANNING_RESERVE[dt]
            for lijn in (1, 2)
        }
        for dt in ("norm", "vr")
    }

    cfg = _raw().get("capacity_minuten")
    if not cfg:
        return derived

    explicit = {
        "norm": {1: cfg["normale_dag"]["lijn1"], 2: cfg["normale_dag"]["lijn2"]},
        "vr": {1: cfg["vrijdag"]["lijn1"], 2: cfg["vrijdag"]["lijn2"]},
    }
    for dt in ("norm", "vr"):
        for lijn in (1, 2):
            if explicit[dt][lijn] != derived[dt][lijn]:
                print(
                    f"[CONFIG] capacity_minuten[{dt}][L{lijn}] = {explicit[dt][lijn]} "
                    f"but the shift window ({line_start(lijn).strftime('%H:%M')}–"
                    f"{_SHIFT_END[dt].strftime('%H:%M')}) minus a "
                    f"{_PLANNING_RESERVE[dt]}min reserve derives {derived[dt][lijn]}. "
                    f"Using the explicit value from capacity.json."
                )
    return explicit


def planning_cap(d: date, lijn: int) -> int:
    """Minutes the assignment solver may book on this (day, line)."""
    return planning_caps()[day_type(d)][lijn]


def max_bezetting_delta() -> float:
    """Largest allowed INCREASE in per-line headcount between two orders."""
    return float(_raw().get("staffing", {}).get("max_bezetting_delta", 1.0))


def production_timing() -> dict[str, int]:
    """Hard delivery cutoff and soft production-proximity preference.

    There is no generic maximum earliness. Known work may use any open day;
    actual shelf-life limits belong to product-specific constraints.
    """
    t = _raw().get("production_timing", {})
    return {
        "min_days": t.get("min_days_before_delivery", 0),
        "proximity_grace_days": t.get("delivery_proximity_grace_days", 7),
    }


def solver_config() -> dict:
    """
    or_tools_solver settings, with FRIGI_* env overrides applied.

    NOTE feedback_timeout_seconds is the budget for ONE phase of the loop; the
    three-phase solver spends it up to three times plus reconciliation.
    """
    import os

    cfg = dict(_raw().get("or_tools_solver", {}))
    if os.environ.get("FRIGI_FEEDBACK_TIMEOUT"):
        cfg["feedback_timeout_seconds"] = int(os.environ["FRIGI_FEEDBACK_TIMEOUT"])
    if os.environ.get("FRIGI_ITER_TIMEOUT"):
        cfg["feedback_iter_timeout_seconds"] = int(os.environ["FRIGI_ITER_TIMEOUT"])
    return cfg


def device_availability() -> dict[str, int]:
    """
    Unit count per machine. Keys starting with "_" are comments in the JSON.
    A machine with >= 2 units can never cause an L1/L2 conflict.
    """
    return {
        k: v
        for k, v in _raw().get("toestellen_beschikbaarheid", {}).items()
        if not k.startswith("_") and isinstance(v, int)
    }
