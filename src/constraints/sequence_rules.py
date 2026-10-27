"""
sequence_rules.py — S1, S2. The order production runs in on one line.

This is a policy, not a mechanism: it decides both how much changeover the day
costs and — because the day builder walks this list and drops whatever no
longer fits — WHICH orders survive an over-full day.

`day_sequencer.py` can override it with a CP-SAT search when the greedy walk
drops something; this is the fixed order production has always used and the
baseline that search has to beat.
"""
from __future__ import annotations

from collections import defaultdict

from ..models import Order, Product
from .allergen_rules import SATE_EXCLUSIVE, is_sate
from .grouping_rules import GEEN_VOLGENDE, Volgende, kapvorm_sleutel, schaal_sleutel


def sort_orders_for_lijn(
    orders_products: list[tuple[Order, Product]],
    vorige: Product | None = None,
    volgende: Volgende = GEEN_VOLGENDE,
) -> list[tuple[Order, Product]]:
    """
    Drie groeperingslagen (planner, meetings 02/10 en 15/10), daarna de
    bestaande regels:

      1. G1 — identieke producten (kolom "identiek product") vormen één blok,
         aaneensluitend, kleinste order eerst.
      2. G2 — blokken met dezelfde kapvorm na elkaar. Producten zonder echte
         kapvorm ("NVT", bijna heel L2) vallen samen in één emmer.
      3. G3 — binnen een kapvorm-emmer de blokken met dezelfde schaal na
         elkaar. Op L2, waar bijna alles "NVT" is, is dit de laag die telt.
      4. Kapvorm-groepen en schaal-groepen gesorteerd op (max allergie_niveau
         oplopend, totaal aantal dalend); binnen een schaal-groep de blokken op
         allergie_niveau oplopend, aantal dalend en fte_base oplopend.

    `vorige` is het product waarmee deze lijn de vorige werkdag afsloot: de
    kapvorm-groep (en daarbinnen de schaal-groep) die daarop aansluit gaat
    voorop, zodat de dag verdergaat waar gisteren stopte (G2/G3 carry). De
    opstart/afsluit-regels (S2) gaan daar nog altijd boven.

    `volgende` is het omgekeerde: de kapvormen en schalen die deze lijn de
    volgende werkdag draait. Een groep die daarin voorkomt gaat achteraan, zodat
    de dag eindigt waar morgen kan beginnen. Aansluiten op gisteren wint als
    het dezelfde groep niet kan zijn.

    Dit minimaliseert kapvorm- en schaalwissels, allergie-dalingen én scherpe
    bezetting-sprongen tussen opeenvolgende orders — en zorgt dat als een dag
    overloopt, de kleinere orders het eerst sneuvelen (_schedule_lijn plaatst
    in deze volgorde en laat valt wat niet meer past), niet willekeurig welke
    toevallig later in de lijst stond.
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

    # kapvorm -> schaal -> blocks
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
        """0 continues yesterday, 2 leads into tomorrow, 1 everything between."""
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


# ── S2: must / cannot be first or last of the day ────────────────────────────
#
# Columns 28-31 of the product database ("opstart moet", "opstart kan niet",
# "afsluit moet", "afsluit kan niet"). A handful of rows set both a "moet" and
# the matching "kan niet" — a contradiction in the sheet, not a case to model —
# so an explicit requirement wins over the prohibition beside it.

def moet_eerst(product: Product) -> bool:
    return product.opstart_moet


def mag_eerst(product: Product) -> bool:
    return not product.opstart_kan_niet or product.opstart_moet


def moet_laatst(product: Product) -> bool:
    return product.afsluit_moet


def mag_laatst(product: Product) -> bool:
    # Sate MUST close (A5, planner 22/10), so it may — whatever the sheet says.
    # One row (66811700) sets both "afsluit moet" and "afsluit kan niet".
    if SATE_EXCLUSIVE and is_sate(product):
        return True
    return not product.afsluit_kan_niet or product.afsluit_moet


def sluit_samen(a: Product, b: Product) -> bool:
    """
    Do `a` and `b` share one closing run? The same article always does. Under
    A5 any two sate products do too: "afsluit moet" on sate means "nothing
    after me", and a second sate block behind it breaks nothing — after sate
    the line only runs more sate.
    """
    if a.art_nr == b.art_nr:
        return True
    return SATE_EXCLUSIVE and is_sate(a) and is_sate(b)


def sate_last(
    seq: list[tuple[Order, Product]],
) -> list[tuple[Order, Product]]:
    """
    A5 — sate to the back of the sequence, everything else in the order it
    arrived in.

    A rule, not a preference (planner 22/10): after sate nothing else can run
    on that line. A product the sheet says MUST close the day can therefore
    not share a line-day with sate — the assignment model keeps them apart,
    and if a day arrives here anyway the position check (S2) refuses the
    closer, not the sate.

    This is the choke point rather than the policy: `apply_positie_regels`
    already sorts sate to the back (it is the highest allergen level) and the
    CP-SAT sequencer has no arc leading out of it, but both are proposals.
    This runs inside the walker, so no route into a built day can leave an
    ordinary product behind the peanut by accident.
    """
    if not SATE_EXCLUSIVE or not any(is_sate(p) for _, p in seq):
        return list(seq)
    return [x for x in seq if not is_sate(x[1])] + [x for x in seq if is_sate(x[1])]


def _pop_first(seq: list[tuple[Order, Product]], pred) -> tuple[Order, Product] | None:
    for i, (_, product) in enumerate(seq):
        if pred(product):
            return seq.pop(i)
    return None


def _pop_last(seq: list[tuple[Order, Product]], pred) -> tuple[Order, Product] | None:
    for i in range(len(seq) - 1, -1, -1):
        if pred(seq[i][1]):
            return seq.pop(i)
    return None


def apply_positie_regels(
    seq: list[tuple[Order, Product]],
) -> list[tuple[Order, Product]]:
    """
    S2 — pin the two ends of the day, leaving everything between them in the
    S1 order it arrived in.

    Requirements are claimed before preferences, both ends before either
    fallback: otherwise the head fills itself with a product that merely MAY
    start but MUST close, and the genuine "afsluit moet" never reaches the end.
    A product carrying both requirements goes to the head, since that claim is
    settled first.

    Prohibitions are hard here — nothing barred from an end is left sitting on
    it while a legal swap exists. When no legal occupant exists at all the
    sequence is returned untouched; that day is impossible, and saying so is
    the validator's job, not this function's.

    Note the interaction with `_schedule_lijn`, which walks this list and drops
    whatever no longer fits: an "afsluit moet" product is now pinned last, so
    on an over-full day it is the first casualty. That is inherent — an order
    cannot be both last and protected from the overflow.
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


def _pop_same_art(
    rest: list[tuple[Order, Product]],
    anchor: tuple[Order, Product],
) -> list[tuple[Order, Product]]:
    """
    Every other order of the anchor's product, so a "moet" product that has
    two orders on one line-day opens (or closes) it as one run. The second
    order of the same product in the middle of the day would break the rule
    just as surely as a different product would.
    """
    art = anchor[1].art_nr
    run = [x for x in rest if x[1].art_nr == art]
    rest[:] = [x for x in rest if x[1].art_nr != art]
    return run


def _leading_run(products: list[Product]) -> int:
    """Length of the opening run of one article — the slot a 'moet eerst' owns."""
    n = 1
    while n < len(products) and products[n].art_nr == products[0].art_nr:
        n += 1
    return n


def _closing_run(products: list[Product]) -> int:
    """Length of the closing run — one article, or one sate block (`sluit_samen`)."""
    n = 1
    while n < len(products) and sluit_samen(products[-1 - n], products[-1]):
        n += 1
    return n


def positie_ok(products: list[Product]) -> bool:
    """
    S2, the whole rule: is this line-day sequence legal at both ends?

      * the opener may open and the closer may close (prohibitions), and
      * every "opstart moet" product sits in the opening run, and every
        "afsluit moet" product (that does not also have to open — the head
        claim wins) sits in the closing run.

    A run is consecutive orders of ONE article, so two orders of the same
    "moet eerst" product open the day together. Two different products that
    both must open can never share a line-day: one of them goes to another day.
    The closing run is the exception: a block of different sate products is
    one run (`sluit_samen`). Hard since the planner's 15/10 instruction — "the
    product database's first/last and line rules are set in stone".
    """
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


def _pop_head(
    rest: list[tuple[Order, Product]],
    tail_claimed: bool,
) -> tuple[Order, Product] | None:
    """
    Choose the opening order without stealing the day's only legal closer.

    Roughly a quarter of the catalogue carries "afsluit kan niet", so a short
    day often has exactly one product that may close it. If that product also
    may open and the head simply takes the first legal candidate it sees, the
    day is left with no legal closer and gets reported as an S2 error it never
    had to have. Prefer a head that leaves someone behind who can close; fall
    back to any legal head when no such split exists.
    """
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
