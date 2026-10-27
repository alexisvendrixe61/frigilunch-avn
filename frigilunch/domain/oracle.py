"""
oracle.py — deterministic single-day scheduler.

schedule_day(orders, products, date, config) → DaySchedule

Pure: no file I/O, no side effects, no OR-Tools.
Deterministic: identical inputs produce identical outputs.
Every function is ≤30 lines and ≤4 parameters.

Config dict keys:
  setup_matrix: dict  — changeover rules (see constraints/changeover.py)
  line_start: dict[int, time]  — {1: time(6,0), 2: time(6,30)}
  shift_end: time     — time(22,30) weekdays, time(20,30) Fridays
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from .models import DaySchedule, Order, PlannedOrder, Product
from .constraints.changeover import changeover_minutes, production_minutes
from .constraints.sequencing import apply_positie_regels, positie_ok


# ── time arithmetic ───────────────────────────────────────────────────────────

def _add_minutes(t: time, minutes: int) -> time:
    dt = datetime.combine(date.today(), t) + timedelta(minutes=minutes)
    return dt.time()


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


# ── line assignment ───────────────────────────────────────────────────────────

def _resolve_lijn(order: Order, product: Product, load1: int, load2: int) -> int:
    """Hard constraints win; load-balance is the tiebreaker."""
    if order.lijn in (1, 2):
        return order.lijn
    if product.alleen_l1 or product.l2_uitgesloten:
        return 1
    if product.alleen_l2 or product.l1_uitgesloten:
        return 2
    if product.prio_l1 and not product.prio_l2:
        return 1
    if product.prio_l2 and not product.prio_l1:
        return 2
    return 1 if load1 <= load2 else 2


def _assign_orders(
    day_orders: list[Order],
    products: dict[str, Product],
) -> tuple[list[tuple[Order, Product]], list[tuple[Order, Product]], list[str]]:
    """Partition day_orders into (lijn1_pairs, lijn2_pairs, warnings)."""
    lijn1: list[tuple[Order, Product]] = []
    lijn2: list[tuple[Order, Product]] = []
    warnings: list[str] = []

    for order in day_orders:
        product = products.get(order.art_nr)
        if product is None:
            warnings.append(f"Order {order.order_nr}: product {order.art_nr} not found — skipped")
            continue
        load1 = sum(production_minutes(o, p) for o, p in lijn1)
        load2 = sum(production_minutes(o, p) for o, p in lijn2)
        lijn = _resolve_lijn(order, product, load1, load2)
        (lijn1 if lijn == 1 else lijn2).append((order, product))

    return lijn1, lijn2, warnings


# ── sorting ───────────────────────────────────────────────────────────────────

def _sort_for_lijn(pairs: list[tuple[Order, Product]]) -> list[tuple[Order, Product]]:
    """S1 ascending allergen, S2 pins legal ends."""
    sorted_pairs = sorted(pairs, key=lambda op: (op[1].allergie_niveau, -op[0].aantal))
    return apply_positie_regels(sorted_pairs)


# ── timeline builder ──────────────────────────────────────────────────────────

def _build_line(
    pairs: list[tuple[Order, Product]],
    lijn_nr: int,
    config: dict[str, Any],
) -> tuple[list[PlannedOrder], list[Order]]:
    """Walk the sequence and assign start/end times.

    Drops orders that would cross midnight or exceed shift_end.
    """
    setup_matrix = config.get("setup_matrix", {})
    line_start: dict = config.get("line_start", {1: time(6, 0), 2: time(6, 30)})
    max_end_min = _to_minutes(config.get("shift_end", time(22, 30)))
    current = line_start.get(lijn_nr, time(6, 0))
    prev: Product | None = None
    planned: list[PlannedOrder] = []
    dropped: list[Order] = []

    for order, product in pairs:
        ombouw = changeover_minutes(prev, product, setup_matrix) if prev is not None else 0
        start = _add_minutes(current, ombouw)
        eind = _add_minutes(start, production_minutes(order, product))
        eind_min, start_min = _to_minutes(eind), _to_minutes(start)
        if eind_min > max_end_min or eind_min < start_min:
            dropped.append(order)
            continue
        planned.append(PlannedOrder(order=order, product=product, lijn=lijn_nr,
                                    start=start, eind=eind, ombouw_voor=ombouw))
        prev, current = product, eind

    return planned, dropped


# ── S2 repair ─────────────────────────────────────────────────────────────────

def _greedy_remove(
    survivors: list[tuple[Order, Product]],
    lijn_nr: int,
    config: dict[str, Any],
) -> tuple[list[PlannedOrder], list[Order]]:
    """Remove smallest orders until the remaining sequence has legal ends."""
    removed: list[Order] = []
    while survivors:
        reordered = apply_positie_regels(survivors)
        rebuilt, _ = _build_line(reordered, lijn_nr, config)
        if positie_ok([po.product for po in rebuilt]):
            return rebuilt, removed
        survivors.sort(key=lambda op: op[0].aantal)
        removed.append(survivors.pop(0)[0])
    return [], removed


def _enforce_ends(
    planned: list[PlannedOrder],
    lijn_nr: int,
    config: dict[str, Any],
) -> tuple[list[PlannedOrder], list[Order]]:
    """S2 — reorder or remove to get legal ends; returns (kept, removed)."""
    if positie_ok([po.product for po in planned]):
        return planned, []

    survivors = [(po.order, po.product) for po in planned]
    retry, _ = _build_line(apply_positie_regels(survivors), lijn_nr, config)
    if positie_ok([po.product for po in retry]) and len(retry) == len(planned):
        return retry, []

    return _greedy_remove(survivors, lijn_nr, config)


# ── KPIs ──────────────────────────────────────────────────────────────────────

def _day_kpis(datum: date, lijn1: list[PlannedOrder], lijn2: list[PlannedOrder]) -> dict:
    """Compute utilisation KPIs for both lines."""
    kpis: dict = {"datum": datum.isoformat()}
    for lijn_nr, planned in ((1, lijn1), (2, lijn2)):
        work = sum(po.ombouw_voor + (_to_minutes(po.eind) - _to_minutes(po.start)) for po in planned)
        kpis[f"lijn{lijn_nr}_orders"] = len(planned)
        kpis[f"lijn{lijn_nr}_werk_min"] = work
    return kpis


# ── public entry point ────────────────────────────────────────────────────────

def schedule_day(
    orders: list[Order],
    products: dict[str, Product],
    datum: date,
    config: dict[str, Any],
) -> DaySchedule:
    """Schedule all orders for `datum` on L1 and L2.

    Pure, deterministic, no I/O.
    """
    day_orders = [o for o in orders if o.productie_datum == datum]
    lijn1_pairs, lijn2_pairs, warnings = _assign_orders(day_orders, products)

    planned1, dropped1 = _build_line(_sort_for_lijn(lijn1_pairs), 1, config)
    planned2, dropped2 = _build_line(_sort_for_lijn(lijn2_pairs), 2, config)

    planned1, s2_removed1 = _enforce_ends(planned1, 1, config)
    planned2, s2_removed2 = _enforce_ends(planned2, 2, config)

    return DaySchedule(
        datum=datum,
        lijn1=planned1,
        lijn2=planned2,
        warnings=warnings,
        kpis=_day_kpis(datum, planned1, planned2),
        dropped=dropped1 + dropped2 + s2_removed1 + s2_removed2,
        positie_dropped=s2_removed1 + s2_removed2,
    )
