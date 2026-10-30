"""
sequencing.py — S1, S2. The order production runs in on one line.

mag_eerst / mag_laatst: whether a product may occupy an end of the day.
moet_eerst / moet_laatst: whether it is required there.
apply_positie_regels: pin the two legal ends of a sequence.
positie_ok: check that a built sequence has legal ends.
sort_orders_for_lijn: full greedy sort (G1/G2/G3 + S1/S2 + carry/look-ahead).
sate_last: push sate to the tail of any sequence (A5).
"""
from __future__ import annotations

from collections import defaultdict

from ..models import Order, Product
from .allergen import SATE_EXCLUSIVE, is_sate
from .grouping_rules import GEEN_VOLGENDE, Volgende, kapvorm_sleutel, schaal_sleutel


# ── S2 predicates ────────────────────────────────────────────────────────────

def moet_eerst(product: Product) -> bool:
    return product.opstart_moet


def mag_eerst(product: Product) -> bool:
    return not product.opstart_kan_niet or product.opstart_moet


def moet_laatst(product: Product) -> bool:
    return product.afsluit_moet


def mag_laatst(product: Product) -> bool:
    # Sate MUST close (A5), so it may — whatever the sheet says.
    if SATE_EXCLUSIVE and is_sate(product):
        return True
    return not product.afsluit_kan_niet or product.afsluit_moet


def sluit_samen(a: Product, b: Product) -> bool:
    """Do a and b share one closing run? Same article always does; sate + sate does too."""
    if a.art_nr == b.art_nr:
        return True
    return SATE_EXCLUSIVE and is_sate(a) and is_sate(b)


# ── sequence validation ───────────────────────────────────────────────────────

def _leading_run(products: list[Product]) -> int:
    """Length of the opening run of one article."""
    n = 1
    while n < len(products) and products[n].art_nr == products[0].art_nr:
        n += 1
    return n


def _closing_run(products: list[Product]) -> int:
    """Length of the closing run — one article, or one sate block."""
    n = 1
    while n < len(products) and sluit_samen(products[-1 - n], products[-1]):
        n += 1
    return n


def positie_ok(products: list[Product]) -> bool:
    """S2: is this sequence legal at both ends?"""
    if not products:
        return True
    if not (mag_eerst(products[0]) and mag_laatst(products[-1])):
        return False
    head_n = _leading_run(products)
    tail_n = _closing_run(products)
    for i, p in enumerate(products):
        if moet_eerst(p) and i >= head_n:
            return False
        if moet_laatst(p) and not moet_eerst(p) and i < len(products) - tail_n:
            return False
    return True


# ── S2 pinning ───────────────────────────────────────────────────────────────

def _pop_first(seq: list, pred) -> tuple | None:
    for i, (_, product) in enumerate(seq):
        if pred(product):
            return seq.pop(i)
    return None


def _pop_last(seq: list, pred) -> tuple | None:
    for i in range(len(seq) - 1, -1, -1):
        if pred(seq[i][1]):
            return seq.pop(i)
    return None


def _pop_same_art(rest: list, anchor: tuple) -> list:
    """Every other order of the anchor's product — keeps moet-articles as one run."""
    art = anchor[1].art_nr
    run = [x for x in rest if x[1].art_nr == art]
    rest[:] = [x for x in rest if x[1].art_nr != art]
    return run


def _pop_head(rest: list, tail_claimed: bool) -> tuple | None:
    """Choose the opening order without stealing the day's only legal closer."""
    fallback: int | None = None
    for i, (_, product) in enumerate(rest):
        if not mag_eerst(product):
            continue
        if fallback is None:
            fallback = i
        if tail_claimed:
            return rest.pop(i)
        if any(mag_laatst(q) for j, (_, q) in enumerate(rest) if j != i):
            return rest.pop(i)
    return rest.pop(fallback) if fallback is not None else None


def apply_positie_regels(seq: list) -> list:
    """S2 — pin legal ends, leave the middle in its current order.

    Requirements are claimed before preferences; both ends before fallbacks.
    When no legal end exists the sequence is returned unchanged.
    """
    if len(seq) < 2:
        return list(seq)

    rest = list(seq)
    head = _pop_first(rest, moet_eerst)
    tail = _pop_last(rest, moet_laatst)
    head_run = _pop_same_art(rest, head) if head is not None else []
    tail_run = _pop_same_art(rest, tail) if tail is not None else []
    if head is None:
        head = _pop_head(rest, tail_claimed=tail is not None)
    if tail is None:
        tail = _pop_last(rest, mag_laatst)
    return (
        ([head] if head else []) + head_run
        + rest
        + tail_run + ([tail] if tail else [])
    )


# ── A5: sate always last ──────────────────────────────────────────────────────

def sate_last(seq: list) -> list:
    """A5 — push all sate to the tail; everything else keeps its order."""
    if not SATE_EXCLUSIVE or not any(is_sate(p) for _, p in seq):
        return list(seq)
    return [x for x in seq if not is_sate(x[1])] + [x for x in seq if is_sate(x[1])]


# ── S1: full greedy sort ──────────────────────────────────────────────────────

def sort_orders_for_lijn(
    orders_products: list[tuple[Order, Product]],
    vorige: Product | None = None,
    volgende: Volgende = GEEN_VOLGENDE,
) -> list[tuple[Order, Product]]:
    """G1/G2/G3 grouping + allergen ascending + largest first; S2 pinned at ends.

    `vorige` — product this line closed with yesterday (carry, G2/G3).
    `volgende` — kapvorm/schaal keys this line runs tomorrow (look-ahead close).
    """
    blocks: dict[object, list[tuple[Order, Product]]] = {}
    for i, (order, product) in enumerate(orders_products):
        key = product.identiek_groep if product.identiek_groep else ("los", i)
        blocks.setdefault(key, []).append((order, product))
    for block in blocks.values():
        block.sort(key=lambda x: x[0].aantal)

    def _rank(items: list[tuple[Order, Product]]) -> tuple[int, int]:
        return (
            max(p.allergie_niveau for _, p in items),
            -sum(o.aantal for o, _ in items),
        )

    groups: dict[str, dict[str, list[list[tuple[Order, Product]]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for block in blocks.values():
        head = block[0][1]
        groups[kapvorm_sleutel(head) or "__geen__"][schaal_sleutel(head) or "__geen__"].append(block)

    carry_kap = kapvorm_sleutel(vorige) if vorige is not None else None
    carry_sch = schaal_sleutel(vorige) if vorige is not None else None
    next_kap, next_sch = volgende

    def _seam(key: str, carry: str | None, nxt: frozenset[str]) -> int:
        if carry is not None and key == carry:
            return 0
        return 2 if key in nxt else 1

    result: list[tuple[Order, Product]] = []
    for kap in sorted(groups, key=lambda k: (
        _seam(k, carry_kap, next_kap),
        _rank([x for sub in groups[k].values() for b in sub for x in b]),
    )):
        by_schaal = groups[kap]
        for sch in sorted(by_schaal, key=lambda s: (
            _seam(s, carry_sch, next_sch),
            _rank([x for b in by_schaal[s] for x in b]),
        )):
            for block in sorted(by_schaal[sch], key=lambda b: (
                max(p.allergie_niveau for _, p in b),
                -sum(o.aantal for o, _ in b),
                min(p.fte_base for _, p in b),
            )):
                result.extend(block)
    return apply_positie_regels(result)


def sluit_samen(a, b) -> bool:
    """Do `a` and `b` share one closing run?

    Same article always does. Under A5, any two sate products do too:
    after sate the line only runs more sate.
    """
    from frigilunch.domain.constraints.allergen import SATE_EXCLUSIVE, is_sate
    if a.art_nr == b.art_nr:
        return True
    return SATE_EXCLUSIVE and is_sate(a) and is_sate(b)
