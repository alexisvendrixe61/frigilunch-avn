"""
line_rules.py — L1, L2, L3. Which line an order runs on.

`permitted_lines` is the hard part (L1/L2) and is what `slots.py` uses to keep
the solver from ever proposing an illegal line. `resolve_lijn` is the BUILD-time
decision, which re-applies the hard rules before honouring anything softer —
so a line the optimizer picked, or one set by hand, still cannot beat an
explicit exclusion.
"""
from __future__ import annotations

from ..models import Order, Product

# Fallback line assignment when the sheet's L1/L2 columns are both blank,
# by scale and brand.
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
    # Explicit 'kan_niet' (0) on ONE line, with the other line usable/unset,
    # is an absolute exclusion for that one line — it must never be scheduled
    # there even if something else (the optimizer's own line pick) suggests
    # otherwise. If BOTH lines are marked kan_niet, that's not read as "truly
    # impossible" — it's treated the same as no restriction at all (can run on
    # either), since a 0/0 pair in the sheet is not meant as a hard block.
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
    """
    L1 + L2 — the lines this product may EVER run on.

    An explicit `kan_niet` exclusion is absolute. `alleen_l1`/`alleen_l2` say
    the same thing from the other direction (usable on one line only).

    Note both-lines-excluded is not treated as "impossible": a 0/0 pair in the
    spreadsheet means the columns were not filled in, not that the product
    cannot be made. See loader._derive_line_info.
    """
    if product.alleen_l1 or product.l2_uitgesloten:
        return [1]
    if product.alleen_l2 or product.l1_uitgesloten:
        return [2]
    return [1, 2]


def preferred_line(product: Product) -> int | None:
    """
    The line this product SHOULD run on when that line has room — or None when
    it has no preference worth expressing.

    This is the soft half of the sheet's Prio Lijn block ("voorkeur" vs "kan"),
    and the counterpart of `permitted_lines`, which is the hard half. It is
    None in the three cases where a preference says nothing:

      - the product is locked to one line anyway (`permitted_lines` decides,
        and a preference term would only add noise to the solver's objective)
      - both lines are marked "voorkeur" (4 products) — no preference stated
      - neither is

    The rule it serves is the 04/09 meeting's "producten prio L1 enkel op L2
    inplannen indien geen plaats op L1", applied in both directions because the
    field itself is symmetric: 71 line-flexible products prefer L1 and 83
    prefer L2, and both were being placed off-preference about as often.
    """
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
    """
    L1/L2/L3 — decide which line this order actually runs on, in priority order:

      1. hard product restriction (alleen_* / *_uitgesloten) — always wins,
         including over a line the optimizer already chose
      2. the line the optimizer assigned (order.lijn), if it set one
      3. a soft product preference (prio_l1 / prio_l2)
      4. load balance on production minutes already placed

    2 deliberately still beats 3, now that the optimizer carries a preference
    term of its own (`preferred_line`, priced in scheduler_ortools_v3's
    objective). Both answer the same question, but only the optimizer can see
    whether the preferred line still had room on that day — which is the whole
    condition ("enkel op L2 indien geen plaats op L1"). Step 3 is the answer
    for orders that never went through the optimizer: a hand-set date, a
    replayed day, a direct schedule_day call.

    `lijn1_minutes` / `lijn2_minutes` are the minutes already assigned to each
    line today, used only for step 4.
    """
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
