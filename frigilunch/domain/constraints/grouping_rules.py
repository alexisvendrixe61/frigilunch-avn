"""grouping_rules.py — G1, G2, G3. Identical products, kapvorm, schaal grouping."""
from __future__ import annotations

import re

from ..models import Product

_ART_NR = re.compile(r"\b\d{8}\b")
_GEEN_KAPVORM = {"", "-", "NVT"}


def parse_identiek_refs(text: str) -> set[str]:
    """Every 8-digit article number in a column-15 cell; nothing for junk."""
    return set(_ART_NR.findall(str(text or "")))


def identiek_groepen(refs_by_art: dict[str, set[str]]) -> dict[str, str]:
    """G1 — art_nr -> group id for every product linked to at least one other.

    Union-find over the links. The group id is the smallest art_nr in the
    component, so it is stable across runs.
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
    """G2 — normalised kapvorm, or None when the product has none."""
    key = (product.kapvorm or "").strip().upper()
    return None if key in _GEEN_KAPVORM else key


def schaal_sleutel(product: Product) -> str | None:
    """G3 — normalised schaal, or None when the cell is empty."""
    key = " ".join((product.schaal or "").split()).upper()
    return None if key in _GEEN_KAPVORM else key


Volgende = tuple[frozenset[str], frozenset[str]]
GEEN_VOLGENDE: Volgende = (frozenset(), frozenset())


def volgende_van(products: list[Product]) -> Volgende:
    """The next day's keys, from the products that line will run then."""
    kaps = {kapvorm_sleutel(p) for p in products}
    schs = {schaal_sleutel(p) for p in products}
    kaps.discard(None)
    schs.discard(None)
    return frozenset(kaps), frozenset(schs)
