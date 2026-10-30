"""
device.py — M1. Single-unit machines shared between production lines.

A machine with ≥2 units can never cause a conflict.  One with a single unit
can only be used by one line at a time — the day builder staggers L2 around
L1's use of any shared machine.
"""
from __future__ import annotations

from collections import defaultdict

from ..models import PlannedOrder, Product


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


def blocked_devices(
    product: Product,
    blocking: dict[str, list[tuple[int, int]]] | None,
    beschikbaarheid: dict[str, int] | None,
) -> list[str]:
    """Single-unit machines this product needs that the other line has already claimed."""
    if not blocking or not beschikbaarheid:
        return []
    return [
        dev for dev in scarce_devices_needed(product, beschikbaarheid)
        if blocking.get(dev)
    ]


def stagger_around(
    start_min: int,
    eind_min: int,
    devices: list[str],
    blocking: dict[str, list[tuple[int, int]]],
) -> int:
    """M1 — earliest start at or after start_min where none of devices is busy."""
    duur = eind_min - start_min
    moved = True
    while moved:
        moved = False
        for dev in devices:
            for busy_start, busy_end in blocking.get(dev, ()):
                if start_min < busy_end and busy_start < start_min + duur:
                    start_min = busy_end
                    moved = True
    return start_min


def busy_intervals(
    planned: list[PlannedOrder],
    beschikbaarheid: dict[str, int] | None,
) -> dict[str, list[tuple[int, int]]]:
    """Which single-unit machines this line occupies, and when."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for po in planned:
        for dev in scarce_devices_needed(po.product, beschikbaarheid):
            out[dev].append((
                po.start.hour * 60 + po.start.minute,
                po.eind.hour * 60 + po.eind.minute,
            ))
    return out


def _is_scarce(toestel: str, beschikbaarheid: dict[str, int] | None) -> bool:
    return (beschikbaarheid or {}).get(toestel, 1) < 2


def _orders_overlap(po1: PlannedOrder, po2: PlannedOrder) -> bool:
    return po1.start < po2.eind and po2.start < po1.eind


def check_toestel_conflict(
    lijn1_orders: list[PlannedOrder],
    lijn2_orders: list[PlannedOrder],
    toestellen_beschikbaarheid: dict[str, int] | None = None,
) -> list[str]:
    """M1 backstop — any single-unit machine claimed by both lines at once."""
    conflicts: list[str] = []
    for po1 in lijn1_orders:
        for po2 in lijn2_orders:
            if not _orders_overlap(po1, po2):
                continue
            for toestel, nodig1 in po1.product.toestellen.items():
                if not nodig1 or not po2.product.toestellen.get(toestel, False):
                    continue
                if _is_scarce(toestel, toestellen_beschikbaarheid):
                    conflicts.append(
                        f"Toestelconflict '{toestel}': "
                        f"{po1.product.naam} (L1 {po1.start.strftime('%H:%M')}-"
                        f"{po1.eind.strftime('%H:%M')}) <> "
                        f"{po2.product.naam} (L2 {po2.start.strftime('%H:%M')}-"
                        f"{po2.eind.strftime('%H:%M')})"
                    )
    return conflicts
