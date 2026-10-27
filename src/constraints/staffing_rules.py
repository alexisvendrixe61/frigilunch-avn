"""
staffing_rules.py — P1. How fast per-line headcount may change between orders.
"""
from __future__ import annotations

from .. import config
from ..models import PlannedOrder

MAX_BEZETTING_DELTA = config.max_bezetting_delta()


def personeel_for_lijn(product, lijn_nr: int) -> int:
    """Headcount this product needs on this line."""
    if lijn_nr == 1:
        return product.personeel_assemblage_l1
    if lijn_nr == 2:
        return product.personeel_assemblage_l2
    return product.personeel_kartonnage


def check_bezetting_continuity(
    prev_po: PlannedOrder,
    curr_po: PlannedOrder,
    max_delta: float = MAX_BEZETTING_DELTA,
) -> str | None:
    """
    P1 — cap how sharply staffing may ramp UP between consecutive orders.

    Compares real per-line headcount (po.man, from personeel_assemblage_l1/l2,
    typically 1-8 people), not the bucketed fte_base estimate — that tops out
    at 2.0, so a larger jump could never be seen there at all.

    Only increases are capped: you cannot conjure extra staff mid-shift beyond
    a small ramp, but an order needing FEWER people than the one before it is
    never a staffing problem, so a drop of any size passes.

    A jump beyond max_delta is still allowed when the incoming order's own
    production period runs longer than 4h — there is room inside that run to
    bring people on gradually, so an abrupt-looking jump on paper is not an
    abrupt one on the floor.

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
