"""
sequencing.py — S1, S2. Position rules for a single line-day.

mag_eerst / mag_laatst: whether a product may occupy an end of the day.
moet_eerst / moet_laatst: whether it is required there.
apply_positie_regels: pin the two legal ends of a sequence.
positie_ok: check that a built sequence has legal ends.
"""
from __future__ import annotations

from ..models import Product


# ── S2 predicates ────────────────────────────────────────────────────────────

def moet_eerst(product: Product) -> bool:
    return product.opstart_moet


def mag_eerst(product: Product) -> bool:
    return not product.opstart_kan_niet or product.opstart_moet


def moet_laatst(product: Product) -> bool:
    return product.afsluit_moet


def mag_laatst(product: Product) -> bool:
    return not product.afsluit_kan_niet or product.afsluit_moet


# ── sequence validation ───────────────────────────────────────────────────────

def positie_ok(products: list[Product]) -> bool:
    """S2: is this sequence legal at both ends?"""
    if not products:
        return True
    if not (mag_eerst(products[0]) and mag_laatst(products[-1])):
        return False
    for i, p in enumerate(products):
        if moet_eerst(p) and i > 0:
            return False
        if moet_laatst(p) and not moet_eerst(p) and i < len(products) - 1:
            return False
    return True


# ── S2 pinning ───────────────────────────────────────────────────────────────

def _pop_where(
    seq: list[tuple[Order, Product]],  # type: ignore[name-defined]
    pred,
    from_end: bool = False,
) -> tuple | None:
    indices = range(len(seq) - 1, -1, -1) if from_end else range(len(seq))
    for i in indices:
        if pred(seq[i][1]):
            return seq.pop(i)
    return None


def apply_positie_regels(
    seq: list[tuple],
) -> list[tuple]:
    """S2 — pin legal ends, leave the middle in its current order.

    Requirements are claimed before preferences; both ends before fallbacks.
    When no legal end exists the sequence is returned unchanged — that day is
    impossible, and reporting it is the validator's job.
    """
    if len(seq) < 2:
        return list(seq)

    rest = list(seq)
    head = _pop_where(rest, moet_eerst)
    tail = _pop_where(rest, moet_laatst, from_end=True)

    if head is None:
        head = _pop_where(rest, mag_eerst)
    if tail is None:
        tail = _pop_where(rest, mag_laatst, from_end=True)

    return (
        ([head] if head else [])
        + rest
        + ([tail] if tail else [])
    )
