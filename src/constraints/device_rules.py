"""
device_rules.py — M1. Machines shared between the two lines.

A machine with 2 or more units can never cause a conflict; one with a single
unit can only be used by one line at a time. The primary enforcement is
staggering inside the day builder (L1 first, then L2 pushed around it), which
makes a collision impossible by construction. The check below is the backstop
that proves it worked.
"""
from __future__ import annotations

from collections import defaultdict

from ..models import PlannedOrder, Product


def is_scarce(toestel: str, beschikbaarheid: dict[str, int] | None) -> bool:
    """A machine is scarce when fewer than 2 units exist. Unknown -> assume 1."""
    return (beschikbaarheid or {}).get(toestel, 1) < 2


def scarce_devices_needed(
    product: Product, beschikbaarheid: dict[str, int] | None
) -> list[str]:
    """Which single-unit machines this product occupies while it runs."""
    return [
        dev for dev, need in product.toestellen.items()
        if need and is_scarce(dev, beschikbaarheid)
    ]


def orders_overlap(po1: PlannedOrder, po2: PlannedOrder) -> bool:
    return po1.start < po2.eind and po2.start < po1.eind


def blocked_devices(
    product: Product,
    blocking: dict[str, list[tuple[int, int]]] | None,
    beschikbaarheid: dict[str, int] | None,
) -> list[str]:
    """
    Single-unit machines this product needs that the OTHER line has already
    claimed for part of the day.
    """
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
    """
    M1 — the earliest start at or after `start_min` where none of `devices`
    is still busy on the other line.

    This is what makes a cross-line device collision impossible by
    construction rather than something to detect afterwards: L1 is scheduled
    first, and each L2 order is pushed forward until every shared single-unit
    machine it needs is free. Returns the new start in minutes-of-day; the
    caller shifts the order by the difference.

    Iterative because pushing past one machine's busy window can move the
    order into another's.
    """
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
    planned: list[PlannedOrder], beschikbaarheid: dict[str, int] | None
) -> dict[str, list[tuple[int, int]]]:
    """
    Which single-unit machines this line occupies, and when — the input
    `stagger_around` needs for the other line.
    """
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for po in planned:
        for dev in scarce_devices_needed(po.product, beschikbaarheid):
            out[dev].append((
                po.start.hour * 60 + po.start.minute,
                po.eind.hour * 60 + po.eind.minute,
            ))
    return out


def check_toestel_conflict(
    lijn1_orders: list[PlannedOrder],
    lijn2_orders: list[PlannedOrder],
    toestellen_beschikbaarheid: dict[str, int] | None = None,
) -> list[str]:
    """
    M1 backstop — any single-unit machine claimed by both lines at once.

    Returns one message per clash; empty means the staggering did its job.
    """
    conflicts: list[str] = []
    for po1 in lijn1_orders:
        for po2 in lijn2_orders:
            if not orders_overlap(po1, po2):
                continue
            for toestel, nodig1 in po1.product.toestellen.items():
                if not nodig1 or not po2.product.toestellen.get(toestel, False):
                    continue
                if is_scarce(toestel, toestellen_beschikbaarheid):
                    conflicts.append(
                        f"Toestelconflict '{toestel}': "
                        f"{po1.product.naam} (L1 {po1.start.strftime('%H:%M')}-"
                        f"{po1.eind.strftime('%H:%M')}) <> "
                        f"{po2.product.naam} (L2 {po2.start.strftime('%H:%M')}-"
                        f"{po2.eind.strftime('%H:%M')})"
                    )
    return conflicts
