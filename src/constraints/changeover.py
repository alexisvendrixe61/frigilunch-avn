"""
changeover.py — O1, O2, O3, V1. How long things take.

Two questions live here: how long an order runs, and how long the line stands
still between two orders.
"""
from __future__ import annotations

import math

from ..models import Order, Product
from .allergen_rules import check_kruiscontaminatie


def calculate_duration(order: Order, product: Product) -> int:
    """V1 — production time in minutes, rounded up to the nearest 5."""
    minutes = (order.aantal / product.snelheid) * 60
    return math.ceil(minutes / 5) * 5


def schaal_formaat(schaal: str) -> str | None:
    """
    O2 — group a scale into a "format" family for changeover purposes.

    Based on head (kop) equivalence from the customer reference table:
    UK59/UK59kraft/UK57/OB36/C2187/Novato all share the UK59 head; AR-1150
    shares C2227's; C2325, C2208, C2176 and KRAFT ROND each stand alone.
    Top-plate differences inside a family (UK57 vs UK59) do not count — only
    the head decides the format.

    Unknown or blank values (raw-material placeholders like "krat 20kg",
    dirty fields) return None, and None is never "the same format" as
    anything — not even as another None.
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


def calculate_setup_time(
    prev_product: Product,
    next_product: Product,
    setup_matrix: dict,
    allergie_daling: bool = False,
    kruiscontaminatie_allergenen: list[str] | None = None,
) -> int:
    """
    O1 — changeover minutes, as the MAXIMUM of every applicable scenario.

    Never the sum: the steps physically overlap, so the total stoppage is the
    longest component, not the total work.

      same scale format      20
      different format       30
      kapvorm change         15
      same-machine floor     Leonard 40, multifill 30 (the one machine has to
                             be fully rebuilt even when the format matches)
      cleaning               30, for an allergen drop or cross-contamination

    O3: named article pairs (Daesang, same risotto) short-circuit to a fixed
    time instead.
    """
    standaard: dict = setup_matrix.get("ombouw_standaard", {})

    for combo in setup_matrix.get("daesang_uitzondering", {}).get("art_nr_combinaties", []):
        if prev_product.art_nr == combo.get("van") and next_product.art_nr == combo.get("naar"):
            return combo.get("tijd", 20)

    formaat_van = schaal_formaat(prev_product.schaal)
    formaat_naar = schaal_formaat(next_product.schaal)
    zelfde_formaat = formaat_van is not None and formaat_van == formaat_naar

    setup = (
        standaard.get("zelfde_formaat", 20) if zelfde_formaat
        else standaard.get("ander_formaat", 30)
    )

    if prev_product.kapvorm != next_product.kapvorm:
        setup = max(setup, setup_matrix.get("kapvorm_wissel_extra", {}).get("andere_kapvorm", 15))

    for machine, floor_min in setup_matrix.get("zelfde_machine_wissel", {}).items():
        if not isinstance(floor_min, int):
            continue
        if prev_product.toestellen.get(machine) and next_product.toestellen.get(machine):
            setup = max(setup, floor_min)

    if allergie_daling or kruiscontaminatie_allergenen:
        setup = max(setup, setup_matrix.get("reiniging", {}).get("allergie_daling", 30))

    return setup


def transition_cost(prev_product: Product, next_product: Product, setup_matrix: dict) -> int:
    """
    The full O1 cost of running `next_product` after `prev_product`, with the
    allergen consequences (A1, A2) worked out here rather than by each caller.
    """
    return calculate_setup_time(
        prev_product,
        next_product,
        setup_matrix,
        allergie_daling=next_product.allergie_niveau < prev_product.allergie_niveau,
        kruiscontaminatie_allergenen=check_kruiscontaminatie(prev_product, next_product) or None,
    )
