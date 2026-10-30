"""
allergen.py — A1, A2, A3, A5 (sate/peanut).

Allergen levels run ASCENDING through a production day.  Going back down
means the line carries residue a lower-allergen product must not pick up.
Level 6 ("X") is sate — peanut — which stops the other line entirely (A5).
"""
from __future__ import annotations

import os

from ..models import Product

# Level 6 is the sate (peanut) marker in the product database.
_SATE_LEVEL = 6

# A5 kill-switch: set FRIGI_SATE_EXCLUSIVE=0 to treat sate as ordinary level-6.
SATE_EXCLUSIVE: bool = os.environ.get("FRIGI_SATE_EXCLUSIVE", "1") == "1"

# Two sate blocks on one day run side by side (set FRIGI_SATE_PARALLEL=0 for old queue).
SATE_PARALLEL: bool = os.environ.get("FRIGI_SATE_PARALLEL", "1") == "1"


def allergen_level(product: Product) -> int:
    return product.allergie_niveau


def is_sate(product: Product) -> bool:
    """A5 — peanut/sate: stops the other production line when running."""
    return product.allergie_niveau >= _SATE_LEVEL


def mep_lead_days(product: Product) -> int:
    """D5 — how many days before production MEP must be prepared."""
    return product.mep_lead_days


def check_kruiscontaminatie(product_a: Product, product_b: Product) -> list[str]:
    """A2 — allergens B introduces that A did not carry ('X' vs '-')."""
    return [
        allergen for allergen, value in product_b.allergenen.items()
        if value == "X" and product_a.allergenen.get(allergen, "-") == "-"
    ]


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def sate_window(planned: list) -> tuple[int, int] | None:
    """A5 — [changeover_start, last_sate_end] in minutes, or None if no sate."""
    runs = [po for po in planned if is_sate(po.product)]
    if not runs:
        return None
    first = min(runs, key=lambda po: _minutes(po.start))
    return _minutes(first.start) - first.ombouw_voor, max(_minutes(po.eind) for po in runs)


def sate_tail_index(planned: list) -> int | None:
    """Index where the trailing uninterrupted sate block begins, or None."""
    if not planned or not is_sate(planned[-1].product):
        return None
    i = len(planned) - 1
    while i > 0 and is_sate(planned[i - 1].product):
        i -= 1
    return i


def overlaps_sate_window(po, window: tuple[int, int]) -> bool:
    """A5 — True if po may NOT run while the other line's sate window is open."""
    w_start, w_end = window
    if not (_minutes(po.start) < w_end and w_start < _minutes(po.eind)):
        return False
    return not (SATE_PARALLEL and is_sate(po.product))


def allergie_daling(prev: Product, curr: Product) -> bool:
    """A1 — does running `curr` after `prev` drop the allergen level?"""
    return curr.allergie_niveau < prev.allergie_niveau


_SATE_SYNC_TOLERANCE_MIN: int = int(
    __import__("os").environ.get("FRIGI_SATE_SYNC_TOLERANCE_MIN", "30")
)


def check_sate_conflict(lijn1_orders: list, lijn2_orders: list) -> list[str]:
    """A5 backstop — every run that breaks the sate rule across both lines."""
    conflicts: list[str] = []
    for own, other, own_nr, other_nr in (
        (lijn1_orders, lijn2_orders, 1, 2),
        (lijn2_orders, lijn1_orders, 2, 1),
    ):
        window = sate_window(own)
        if window is None:
            continue
        w_start, w_end = window
        for po in other:
            if overlaps_sate_window(po, window):
                conflicts.append(
                    f"Sate-conflict: L{other_nr} {po.product.naam} "
                    f"({po.start.strftime('%H:%M')}-{po.eind.strftime('%H:%M')}) "
                    f"loopt tijdens de sate-productie op L{own_nr} "
                    f"({w_start // 60:02d}:{w_start % 60:02d}-"
                    f"{w_end // 60:02d}:{w_end % 60:02d})"
                )
        first_sate = min(_minutes(po.start) for po in own if is_sate(po.product))
        for po in own:
            if not is_sate(po.product) and _minutes(po.start) > first_sate:
                conflicts.append(
                    f"Sate-conflict: L{own_nr} {po.product.naam} "
                    f"({po.start.strftime('%H:%M')}-{po.eind.strftime('%H:%M')}) "
                    f"loopt na de sate op dezelfde lijn — na sate kan er niets meer"
                )
    return conflicts


def check_sate_sync(lijn1_orders: list, lijn2_orders: list) -> str | None:
    """A5 — warn when both-line sate blocks don't open within tolerance."""
    if not SATE_PARALLEL:
        return None
    starts = []
    for planned in (lijn1_orders, lijn2_orders):
        runs = [_minutes(po.start) for po in planned if is_sate(po.product)]
        if not runs:
            return None
        starts.append(min(runs))
    gap = abs(starts[0] - starts[1])
    if gap <= _SATE_SYNC_TOLERANCE_MIN:
        return None
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    return (
        f"Sate start niet gelijktijdig: L1 {fmt(starts[0])}, L2 {fmt(starts[1])} "
        f"({gap} min verschil)"
    )
