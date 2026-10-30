"""
staffing.py — P1. Per-line headcount and how fast it may ramp between orders.
"""
from __future__ import annotations

from frigilunch.adapters.config import max_bezetting_delta as _max_delta
from ..models import PlannedOrder

_MAX_BEZETTING_DELTA: float = _max_delta()
MAX_BEZETTING_DELTA = _MAX_BEZETTING_DELTA  # public alias for day_sequencer


def personeel_for_lijn(product, lijn_nr: int) -> int:
    """Headcount this product needs on this line."""
    if lijn_nr == 1:
        return product.personeel_assemblage_l1
    if lijn_nr == 2:
        return product.personeel_assemblage_l2
    return getattr(product, "personeel_kartonnage", 1)


def check_bezetting_continuity(
    prev_po: PlannedOrder,
    curr_po: PlannedOrder,
    max_delta: float = _MAX_BEZETTING_DELTA,
) -> str | None:
    """P1 — cap how sharply staffing may ramp up between consecutive orders.

    Returns None if fine, a warning string if the ramp is too steep.
    """
    increase = curr_po.man - prev_po.man
    if increase <= max_delta:
        return None

    duration_min = (
        curr_po.eind.hour * 60 + curr_po.eind.minute
        - curr_po.start.hour * 60 - curr_po.start.minute
    )
    if duration_min > 4 * 60:
        return None

    return (
        f"Bezetting jump: {prev_po.product.naam} ({prev_po.man:.0f} pers) -> "
        f"{curr_po.product.naam} ({curr_po.man:.0f} pers), "
        f"increase {increase:.0f} > max {max_delta:.0f} "
        f"(productieperiode {duration_min / 60:.1f}u <= 4u)"
    )
