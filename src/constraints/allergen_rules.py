"""
allergen_rules.py — A1, A2, A3, A5.

Allergen levels run ASCENDING through a production day. Going back down means
the line carries residue a lower-allergen product must not pick up, so a drop
requires cleaning. Introducing an allergen the previous product did not carry
(cross-contamination) costs the same.

The level scale is 0-6, straight from the spreadsheet's own legend:
"-" (0) is clean, "!" .. "!!!!!" (1-5) climb, and "X" (6) is sate — peanut,
the highest risk on the floor. High enough that it is not just a sequencing
question: see A5 at the bottom of this file, where sate stops the OTHER line.
"""
from __future__ import annotations

import os

from ..models import Product


def allergie_niveau_from_allergenen(allergenen: dict[str, str]) -> int:
    """
    A3 — level from the number of 'X' (cross-contamination) allergens.

      0 X -> 0,  1 X -> 1,  2-3 X -> 2,  4-6 X -> 3,  7+ X -> 5

    Only used when the spreadsheet carries no explicit level label; the
    labelled scale runs 0-6 ("-" .. "!!!!!" .. "X"), and level 6 is reserved
    for the "X = sate" label, which a marker count cannot tell you.

    Kept in step with product_setup_loader._allergy_level_from_x_count.
    """
    x_count = sum(1 for v in allergenen.values() if v == "X")
    if x_count == 0:
        return 0
    if x_count == 1:
        return 1
    if x_count <= 3:
        return 2
    if x_count <= 6:
        return 3
    return 5


def allergie_daling(prev: Product, curr: Product) -> bool:
    """A1 — does running `curr` after `prev` drop the allergen level?"""
    return curr.allergie_niveau < prev.allergie_niveau


def check_kruiscontaminatie(product_a: Product, product_b: Product) -> list[str]:
    """
    A2 — allergens that product B carries as 'X' while product A does not carry
    them at all ('-'). Each one is residue B would introduce onto a line that
    was clean of it, so the transition needs cleaning.

    Returns the allergen names, empty if the transition is clean.
    """
    issues: list[str] = []
    for allergeen, waarde_b in product_b.allergenen.items():
        if waarde_b == "X" and product_a.allergenen.get(allergeen, "-") == "-":
            issues.append(allergeen)
    return issues


# ── A5: sate ('X') runs alone in the hall ────────────────────────────────────
#
# Level 6 is not one more step on the ladder. The other five levels are a
# property of ONE line's sequence — clean up before you go down, and the line
# is safe. Peanut is not: it travels through the hall, so while sate runs on
# one line the other line produces nothing ELSE. Sate beside sate is fine
# since the second Gernal (planner, 22/10): two sate blocks on one day run
# side by side and start together, instead of queueing.
#
# And sate closes its line's day — hard, not a preference (planner, 22/10):
# after sate nothing else can run on that line.
#
# That makes it the only allergen rule with a cross-line dimension, which is
# why it is shaped like the shared-machine rule (M1) rather than like A1/A2:
# a window on one line that the other line may not overlap.

SATE_NIVEAU = 6

# The whole of A5 — the closing position, the cut-off on the other line, and
# the capacity it reserves in the assignment model — hangs off this one switch,
# so an A/B run turns off all three or none. Set FRIGI_SATE_EXCLUSIVE=0 to plan
# sate as an ordinary level-6 product, the way the plan did before the rule
# existed.
SATE_EXCLUSIVE = os.environ.get("FRIGI_SATE_EXCLUSIVE", "1") == "1"

# Two sate blocks on one day run SIDE BY SIDE, both opening at the same moment
# (planner 22/10: a second Gernal is installed; "beide satéproducties moeten
# ongeveer gelijktijdig opstarten"). Set FRIGI_SATE_PARALLEL=0 for the old
# queue, one block behind the other — the fallback for a day the second
# Gernal is out of service.
SATE_PARALLEL = os.environ.get("FRIGI_SATE_PARALLEL", "1") == "1"

# How far apart the two blocks' openings may be before the validator says so.
# The builder lines them up exactly; it can only miss when sliding one would
# put it on a single-unit machine the other line holds (M1).
SATE_SYNC_TOLERANCE_MIN = int(os.environ.get("FRIGI_SATE_SYNC_TOLERANCE_MIN", "30"))


def is_sate(product: Product) -> bool:
    """A5 — does this product carry the 'X' (sate / peanut) label?"""
    return product.allergie_niveau >= SATE_NIVEAU


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def sate_window(planned: list) -> tuple[int, int] | None:
    """
    A5 — the stretch of this line's day, in minutes-of-day, during which the
    other line must stand still. None when the line runs no sate.

    Spans from the first sate run to the last, so two sate orders with
    something between them (which A1 would already have to clean for) block
    the gap as well — the hall does not get clean in between.

    It starts at the CHANGEOVER in front of the first sate run, not at the
    first pack coming off the line: the peanut arrives at the line while it is
    being changed over. That is the careful reading, and it costs the other
    line those 20-40 minutes.
    """
    runs = [po for po in planned if is_sate(po.product)]
    if not runs:
        return None
    first = min(runs, key=lambda po: _minutes(po.start))
    return _minutes(first.start) - first.ombouw_voor, max(_minutes(po.eind) for po in runs)


def sate_tail_index(planned: list) -> int | None:
    """
    Index where the day's trailing, uninterrupted run of sate orders begins,
    or None when the line ends on something else.

    Only a tail can be slid towards closing time as one block, which is what
    keeps the other line's loss to the last hours of the day instead of
    cutting it in half. `sequence_rules.sate_last` puts every sate order
    there, so on a built day this is None only when the line runs no sate.
    """
    if not planned or not is_sate(planned[-1].product):
        return None
    i = len(planned) - 1
    while i > 0 and is_sate(planned[i - 1].product):
        i -= 1
    return i


def overlaps_sate_window(po, window: tuple[int, int]) -> bool:
    """
    A5 — may `po` NOT run while the other line's sate window is open? Anything
    ordinary overlapping it may not. Sate may, when blocks run side by side
    (SATE_PARALLEL); in the old queue it may not either.
    """
    w_start, w_end = window
    if not (_minutes(po.start) < w_end and w_start < _minutes(po.eind)):
        return False
    return not (SATE_PARALLEL and is_sate(po.product))


def check_sate_conflict(lijn1_orders: list, lijn2_orders: list) -> list[str]:
    """
    A5 backstop — every run that breaks the sate rule. Empty means the day is
    legal. Three ways to break it:

      * an ordinary product on one line overlaps the other line's sate window
        (sate beside sate only counts when SATE_PARALLEL is off);
      * anything that is not sate runs after the sate on its own line — after
        sate the line produces nothing else (planner, 22/10).
    """
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
                    f"({w_start // 60:02d}:{w_start % 60:02d}-{w_end // 60:02d}:{w_end % 60:02d})"
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
    """
    A5 — with sate on both lines the two blocks open together (planner 22/10:
    "ongeveer gelijktijdig opstarten"). A warning, not an error, when their
    first sate runs start more than SATE_SYNC_TOLERANCE_MIN apart.
    """
    if not SATE_PARALLEL:
        return None
    starts = []
    for planned in (lijn1_orders, lijn2_orders):
        runs = [_minutes(po.start) for po in planned if is_sate(po.product)]
        if not runs:
            return None
        starts.append(min(runs))
    gap = abs(starts[0] - starts[1])
    if gap <= SATE_SYNC_TOLERANCE_MIN:
        return None
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    return (
        f"Sate start niet gelijktijdig: L1 {fmt(starts[0])}, L2 {fmt(starts[1])} "
        f"({gap} min verschil)"
    )
