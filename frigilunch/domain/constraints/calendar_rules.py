"""calendar_rules.py — C2. Weekday naming and per-line start times."""
from __future__ import annotations

from datetime import time

from frigilunch.adapters.config import line_start

WEEKDAG_MAP: dict[int, str] = {
    0: "ma", 1: "di", 2: "wo", 3: "do", 4: "vr", 5: "za", 6: "zo"
}
WERKDAGEN: set[int] = {0, 1, 2, 3, 4}
PRODUCTIEDAGEN: tuple[str, ...] = ("ma", "di", "wo", "do", "vr")


def weekdag(d) -> str:
    """Dutch two-letter weekday for a date."""
    return WEEKDAG_MAP.get(d.weekday(), "")


def get_earliest_start(lijn: int) -> time:
    """C2 — L1 starts 06:00, L2 06:30."""
    return line_start(lijn)
