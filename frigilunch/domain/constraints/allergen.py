"""
allergen.py — A1, A2, A3, A5 (sate/peanut).

Allergen levels run ASCENDING through a production day.  Going back down
means the line carries residue a lower-allergen product must not pick up.
Level 6 ("X") is sate — peanut — which stops the other line entirely (A5).
"""
from __future__ import annotations

from ..models import Product

# Level 6 is the sate (peanut) marker in the product database.
_SATE_LEVEL = 6


def allergen_level(product: Product) -> int:
    return product.allergie_niveau


def is_sate(product: Product) -> bool:
    """A5 — peanut/sate: stops the other production line when running."""
    return product.allergie_niveau >= _SATE_LEVEL


def mep_lead_days(product: Product) -> int:
    """D5 — how many days before production MEP must be prepared."""
    return product.mep_lead_days
