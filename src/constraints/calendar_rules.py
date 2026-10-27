"""
calendar_rules.py — C2. Weekday naming and per-line start times.

The shift bounds themselves live in src/config.py; this module is the thin
constraint-facing view of them.
"""
from __future__ import annotations

from datetime import time

from .. import config

# ISO weekday 0=Monday … 6=Sunday → Dutch abbreviation
WEEKDAG_MAP: dict[int, str] = {
    0: "ma", 1: "di", 2: "wo", 3: "do", 4: "vr", 5: "za", 6: "zo"
}

# The days production can happen at all (C4), as ISO weekday numbers and as
# the Dutch abbreviations the product day-columns use.
WERKDAGEN: set[int] = {0, 1, 2, 3, 4}
PRODUCTIEDAGEN: tuple[str, ...] = ("ma", "di", "wo", "do", "vr")


def weekdag(d) -> str:
    """Dutch two-letter weekday for a date."""
    return WEEKDAG_MAP.get(d.weekday(), "")


def get_earliest_start(lijn: int) -> time:
    """C2 — L1 starts 06:00, L2 06:30. No Friday exception on the start."""
    return config.line_start(lijn)
