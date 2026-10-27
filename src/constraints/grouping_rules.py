"""
grouping_rules.py — G1, G2. Which orders belong together.

G1 — identical products. Column 15 of the product database ("identiek
product") links products that are the same recipe in another pack: VAT and
VAT EW, DMB and DMB GESNEDEN, the three Lidl pho soups. The planner wants them
on the same day and line, back to back, smallest first (meeting 02/10). The
cell is a LINK LIST, not a group id: "63319800 - 63963500 ", sometimes one
number, sometimes junk ("f", a seven-digit typo). Links are meant to be
symmetric but the sheet does not always state both directions — 63963500
names only two of its three partners — so the groups are the connected
components of every link, which is what `identiek_groepen` computes.

G2 — same kapvorm together. The second layer, under G1. `kapvorm_sleutel`
returns None for products without a real kapvorm ("NVT", nearly all of L2),
so on L2 this layer simply falls through to the next rule.

G3 — same schaal together, next to G2. Planner, 15/10: kapvorm and schaal
"as much as possible after each other, within a day and over days". So both
are also priced ACROSS days: a kapvorm or schaal that a line runs on
consecutive workdays is one campaign, and every new campaign costs.

All three are preferences. The assignment model prices a group spread over
several (day, line) slots and every campaign start, the day sort keeps a group
contiguous, the sequencer pays a bonus for back-to-back arcs and for opening
the day with what the line closed on — none of them ever rejects an order to
keep a group together. The position rules (S2) and line rules (L1/L2) are hard
and always win over grouping.
"""
from __future__ import annotations

import re

from ..models import Product

_ART_NR = re.compile(r"\b\d{8}\b")
_GEEN_KAPVORM = {"", "-", "NVT"}


def parse_identiek_refs(text: str) -> set[str]:
    """Every 8-digit article number in a column-15 cell; nothing for junk."""
    return set(_ART_NR.findall(str(text or "")))


def identiek_groepen(refs_by_art: dict[str, set[str]]) -> dict[str, str]:
    """
    G1 — art_nr -> group id, for every product linked to at least one other.

    Union-find over the links. A reference to an article that is not in the
    database is ignored rather than turned into a group of one. The group id is
    the smallest art_nr in the component, so it is stable across runs.
    """
    parent: dict[str, str] = {a: a for a in refs_by_art}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, refs in refs_by_art.items():
        for b in refs:
            if b == a or b not in parent:
                continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

    members: dict[str, list[str]] = {}
    for a in parent:
        members.setdefault(find(a), []).append(a)
    return {
        a: min(group)
        for group in members.values() if len(group) >= 2
        for a in group
    }


def kapvorm_sleutel(product: Product) -> str | None:
    """G2 — normalised kapvorm, or None when the product has none ("NVT")."""
    key = (product.kapvorm or "").strip().upper()
    return None if key in _GEEN_KAPVORM else key


# The kapvorm and schaal keys a line runs on its NEXT workday — what this day
# should close on so the seam between the two days needs no swap (G2/G3).
Volgende = tuple[frozenset[str], frozenset[str]]
GEEN_VOLGENDE: Volgende = (frozenset(), frozenset())


def volgende_van(products: list[Product]) -> Volgende:
    """The next day's keys, from the products that line will run then."""
    kaps = {kapvorm_sleutel(p) for p in products}
    schs = {schaal_sleutel(p) for p in products}
    kaps.discard(None)
    schs.discard(None)
    return frozenset(kaps), frozenset(schs)


def schaal_sleutel(product: Product) -> str | None:
    """
    G3 — normalised schaal, or None when the cell is empty.

    The exact tray, not the changeover family (`changeover.schaal_formaat`):
    UK-59 and C2187-1F share a head, so ombouw between them is cheap, but the
    planner still reads them as two schalen and wants each run in one block.
    Whitespace and case are folded so "UK59 KRAFT" and "uk59  kraft" match.
    """
    key = " ".join((product.schaal or "").split()).upper()
    return None if key in _GEEN_KAPVORM else key
