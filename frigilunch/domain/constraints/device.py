"""
device.py — M1. Single-unit machines shared between production lines.

A machine with ≥2 units can never cause a conflict.  One with a single unit
can only be used by one line at a time — the day builder staggers L2 around
L1's use of any shared machine.
"""
from __future__ import annotations

from ..models import Product


def scarce_devices_needed(
    product: Product,
    availability: dict[str, int] | None,
) -> set[str]:
    """M1 — single-unit machines this product occupies while it runs."""
    if not availability:
        return set()
    return {
        dev for dev, needed in product.toestellen.items()
        if needed and availability.get(dev, 1) < 2
    }
