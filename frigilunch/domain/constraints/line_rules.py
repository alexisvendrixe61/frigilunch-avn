"""line_rules.py — L1, L2, L3. Which line an order runs on."""
from __future__ import annotations

from ..models import Order, Product

_L1_MERKEN = {"BOF", "IGLO", "DMH", "BLUE GREEN"}
_L2_SCHALEN_PREFIX = ("C2227", "C2325")


def line_permission_code(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    if value == "voorkeur":
        return 1
    if value == "kan":
        return 2
    if value == "kan_niet":
        return 0
    return None


def derive_line_info(
    l1_setting: str | int | None,
    l2_setting: str | int | None,
    schaal: str,
    merk: str,
) -> dict[str, bool]:
    l1_setting = line_permission_code(l1_setting)
    l2_setting = line_permission_code(l2_setting)

    if l1_setting is None and l2_setting is None:
        if schaal == "UK-59" or merk in _L1_MERKEN:
            l1_setting, l2_setting = 1, 0
        elif schaal.startswith(_L2_SCHALEN_PREFIX) or schaal == "KRAFT ROND":
            l1_setting, l2_setting = 0, 1
        else:
            l1_setting, l2_setting = 2, 1

    can_l1 = l1_setting in {1, 2}
    can_l2 = l2_setting in {1, 2}
    l1_uitgesloten = l1_setting == 0 and l2_setting != 0
    l2_uitgesloten = l2_setting == 0 and l1_setting != 0
    prio_l1 = l1_setting == 1
    prio_l2 = l2_setting == 1

    if not prio_l1 and not prio_l2:
        if can_l1 and not can_l2:
            prio_l1 = True
        elif can_l2 and not can_l1:
            prio_l2 = True

    return {
        "prio_l1": prio_l1,
        "prio_l2": prio_l2,
        "alleen_l1": can_l1 and not can_l2,
        "alleen_l2": can_l2 and not can_l1,
        "l1_uitgesloten": l1_uitgesloten,
        "l2_uitgesloten": l2_uitgesloten,
    }


def permitted_lines(product: Product) -> list[int]:
    """L1 + L2 — the lines this product may ever run on."""
    if product.alleen_l1 or product.l2_uitgesloten:
        return [1]
    if product.alleen_l2 or product.l1_uitgesloten:
        return [2]
    return [1, 2]


def preferred_line(product: Product) -> int | None:
    """The line this product should run on when that line has room, or None."""
    if len(permitted_lines(product)) < 2:
        return None
    if product.prio_l1 and not product.prio_l2:
        return 1
    if product.prio_l2 and not product.prio_l1:
        return 2
    return None


def resolve_lijn(
    order: Order,
    product: Product,
    lijn1_minutes: int,
    lijn2_minutes: int,
) -> int:
    """L1/L2/L3 — decide which line this order actually runs on."""
    allowed = permitted_lines(product)
    if len(allowed) == 1:
        return allowed[0]
    if order.lijn in allowed:
        return order.lijn
    if product.prio_l1 and not product.prio_l2:
        return 1
    if product.prio_l2 and not product.prio_l1:
        return 2
    return 1 if lijn1_minutes <= lijn2_minutes else 2
