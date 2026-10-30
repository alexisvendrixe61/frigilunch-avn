"""
changeover.py — O1, O2, O3, V1.

changeover_minutes(a, b, config) returns the number of minutes the line
stands still between product a and product b.  All logic is a pure function
of the two products and the setup matrix — no I/O, no state.
"""
from __future__ import annotations

import math

from ..models import Order, Product


# ── scale format families (O2) ────────────────────────────────────────────────

def schaal_formaat(schaal: str) -> str | None:
    """Group a scale into a format family for changeover purposes.

    Unknown or blank values return None — never "the same format" as anything.
    """
    s = schaal.strip().upper()
    if not s:
        return None
    if s.startswith(("C2227", "AR-1150", "AR 1150", "AR1150")):
        return "C2227"
    if s.startswith("C2325"):
        return "C2325"
    if s.startswith("C2208"):
        return "C2208"
    if s.startswith("C2176"):
        return "C2176"
    if s.startswith(("UK", "OB", "C2187", "NOVATO")):
        return "UK59"
    if s.startswith("KRAFT"):
        return "KRAFT ROND"
    return None


_schaal_formaat = schaal_formaat  # private alias kept for tests


# ── allergen-related helpers (A1, A2) ────────────────────────────────────────

def _allergen_drop(prev: Product, curr: Product) -> bool:
    """A1 — does running curr after prev lower the allergen level?"""
    return curr.allergie_niveau < prev.allergie_niveau


def _cross_contamination(prev: Product, curr: Product) -> list[str]:
    """A2 — allergens curr introduces that prev did not carry."""
    return [
        allergen for allergen, value in curr.allergenen.items()
        if value == "X" and prev.allergenen.get(allergen, "-") == "-"
    ]


# ── changeover (O1, O3) ───────────────────────────────────────────────────────

def changeover_minutes(prev: Product, curr: Product, setup_matrix: dict) -> int:
    """O1 — changeover minutes between prev and curr; takes the maximum
    of all applicable scenarios (steps overlap physically).

    O3: named article pairs short-circuit to a fixed time.
    """
    for combo in setup_matrix.get("daesang_uitzondering", {}).get("art_nr_combinaties", []):
        if prev.art_nr == combo.get("van") and curr.art_nr == combo.get("naar"):
            return combo.get("tijd", 20)

    standaard = setup_matrix.get("ombouw_standaard", {})
    fmt_prev = schaal_formaat(prev.schaal)
    fmt_curr = schaal_formaat(curr.schaal)
    same_format = fmt_prev is not None and fmt_prev == fmt_curr

    cost = standaard.get("zelfde_formaat", 20) if same_format else standaard.get("ander_formaat", 30)

    if prev.kapvorm != curr.kapvorm:
        extra = setup_matrix.get("kapvorm_wissel_extra", {}).get("andere_kapvorm", 15)
        cost = max(cost, extra)

    for machine, floor_min in setup_matrix.get("zelfde_machine_wissel", {}).items():
        if isinstance(floor_min, int) and prev.toestellen.get(machine) and curr.toestellen.get(machine):
            cost = max(cost, floor_min)

    if _allergen_drop(prev, curr) or _cross_contamination(prev, curr):
        cleaning = setup_matrix.get("reiniging", {}).get("allergie_daling", 30)
        cost = max(cost, cleaning)

    return cost


# ── production duration (V1) ─────────────────────────────────────────────────

def production_minutes(order: Order, product: Product) -> int:
    """V1 — production time in minutes, rounded up to the nearest 5."""
    minutes = (order.aantal / product.snelheid) * 60
    return math.ceil(minutes / 5) * 5


calculate_duration = production_minutes
transition_cost = changeover_minutes
