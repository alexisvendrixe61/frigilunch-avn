"""
day_quality.py — one answer to "which build of this line-day is better?".

The day builder makes that choice in several places: keep the greedy walk or
the CP-SAT sequencer's proposal (`scheduler._maybe_recover_drops`), keep L1 or
L2 first on the shared machines (`scheduler._pair_quality`). Each used to have
its own yardstick — "fewer changeover minutes" — which threw away any sequence
the sequencer had grouped or compacted at the price of a few setup minutes.

`line_score` is the sequencer's own objective, recomputed on a walked
timeline, with the SAME weights (imported from day_sequencer). More orders
always wins first; the score breaks ties.
"""
from __future__ import annotations

from .constraints.calendar_rules import get_earliest_start
from .constraints.grouping_rules import (
    GEEN_VOLGENDE, Volgende, kapvorm_sleutel, schaal_sleutel,
)
from .day_sequencer import (
    IDENTIEK_BONUS, IDLE_WEIGHT, SCHAAL_BONUS, carry_bonus, close_bonus,
    kapvorm_bonus,
)
from .models import PlannedOrder, Product


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def idle_minutes(planned: list[PlannedOrder], lijn_nr: int) -> int:
    """
    O6 — minutes between the line start and its last order's end that are
    neither production nor changeover. Zero for an empty line.
    """
    if not planned:
        return 0
    idle = 0
    prev_end = _minutes(get_earliest_start(lijn_nr))
    for po in sorted(planned, key=lambda p: _minutes(p.start)):
        idle += max(0, _minutes(po.start) - po.ombouw_voor - prev_end)
        prev_end = _minutes(po.eind)
    return idle


def adjacency_counts(planned: list[PlannedOrder]) -> tuple[int, int, int]:
    """
    (identical pairs back to back with the smaller one first,
     same-kapvorm pairs back to back,
     same-schaal pairs back to back) — G1, G2 and G3 as the floor sees them.
    """
    ident = kap = sch = 0
    seq = sorted(planned, key=lambda p: _minutes(p.start))
    for a, b in zip(seq, seq[1:]):
        g = a.product.identiek_groep
        if g and g == b.product.identiek_groep and a.order.aantal <= b.order.aantal:
            ident += 1
        k = kapvorm_sleutel(a.product)
        if k is not None and k == kapvorm_sleutel(b.product):
            kap += 1
        s = schaal_sleutel(a.product)
        if s is not None and s == schaal_sleutel(b.product):
            sch += 1
    return ident, kap, sch


def opener(planned: list[PlannedOrder]) -> Product | None:
    return min(planned, key=lambda p: _minutes(p.start)).product if planned else None


def closer(planned: list[PlannedOrder]) -> Product | None:
    return max(planned, key=lambda p: _minutes(p.eind)).product if planned else None


def line_score(
    planned: list[PlannedOrder], lijn_nr: int, vorige: Product | None = None,
    volgende: Volgende = GEEN_VOLGENDE,
) -> int:
    """
    The sequencer's objective without the order count: higher is better.
    `vorige` is what this line closed its previous workday with (carry),
    `volgende` the keys it runs on its next workday (look-ahead close).
    """
    ident, kap, sch = adjacency_counts(planned)
    first, last = opener(planned), closer(planned)
    return (
        IDENTIEK_BONUS * ident
        + kapvorm_bonus(lijn_nr) * kap
        + SCHAAL_BONUS * sch
        + (carry_bonus(vorige, first, lijn_nr) if first is not None else 0)
        + (close_bonus(last, volgende, lijn_nr) if last is not None else 0)
        - IDLE_WEIGHT * idle_minutes(planned, lijn_nr)
        - sum(po.ombouw_voor for po in planned)
    )


def line_quality(
    planned: list[PlannedOrder], lijn_nr: int, vorige: Product | None = None,
    volgende: Volgende = GEEN_VOLGENDE,
) -> tuple[int, int]:
    """(orders placed, line_score) — compare two builds with `>`."""
    return len(planned), line_score(planned, lijn_nr, vorige, volgende)
