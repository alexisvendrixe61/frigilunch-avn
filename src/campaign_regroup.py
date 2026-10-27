"""
campaign_regroup.py — G2 / G3 over days, as a local search on the finished plan.

The planner (15/10) wants kapvorm and schaal "after each other as much as
possible, within a day and also over days". The assignment model carries
campaign terms for that, but they live in its soft stage, which ends far from
its bound — on wk40 the committed window got three feedback iterations in 240s
and kapvorm changes across days did not move (18 -> 21). Bigger weights cannot
help a stage that never gets to optimise them.

So this pass works on the answer instead of the model. With the set of
scheduled orders fixed it tries two kinds of change:

  MOVE  one order onto a line-day where its kapvorm or schaal already runs (or
        where that line's neighbouring workday does);
  SWAP  the same, when the host line-day is full: one of the host's orders
        goes back to the freed slot, so both days keep their load. On wk40,
        21 of 39 committed line-days were >= 90% full and 11 of the 15
        kapvorm orders that had a legal host were blocked by exactly that.
        When one order coming back is not enough room — a 7.5-hour Fitmeals
        run joining a day of short BOF runs — two of the host's orders go
        back together (SWAP2).

Every change is rebuilt with the REAL day builder — the touched days and their
neighbouring workdays, because a day's ends decide the seams on both sides —
and kept only if

  * no rebuilt day drops an order or gains an [ERROR] — so every hard rule the
    day builder enforces (S2 first/last, devices, sate, capacity, line and day
    restrictions) still holds, and the order count can never go down, and
  * the plan scores better, lexicographically:
      1. fewer kapvorm changes on L1 over the whole window, day seams
         included (`layout_metrics.l1_kapvorm_wissels`) — the automatic
         packing line's rule, which the planner put above every other
         setting (22/10);
      2. then `day_quality.line_score` summed over every line-day, each
         opened against the product its line closed the previous workday on
         — the sequencer's own objective, stretched across days.

Legal targets come from `slots.legal_slots`, the same FILTER the solver uses,
so a change never lands past delivery or on a forbidden day or line. An order
stays on its line or moves to its preferred line, never off it. Split orders
and orders on days that already drop something in a greedy build are left
alone: the first would need every chunk moved together, the second has no
clean baseline to compare against.
"""
from __future__ import annotations

import os as _os
import time as _time
from collections import Counter, defaultdict
from datetime import date

from . import config
from .constraints import calculate_duration, preferred_line
from .constraints import slots as slots_mod
from .constraints.grouping_rules import kapvorm_sleutel, schaal_sleutel, volgende_van
from .day_quality import closer, line_score
from .layout_metrics import l1_kapvorm_wissels
from .models import DaySchedule, Order, Product
from .plan_state import order_identity
from .scheduler import schedule_day

ENABLED = _os.environ.get("FRIGI_CAMPAIGN_REGROUP", "1") == "1"
BUDGET_SECONDS = float(_os.environ.get("FRIGI_CAMPAIGN_REGROUP_SECONDS", "120"))
SWAPS = _os.environ.get("FRIGI_CAMPAIGN_REGROUP_SWAPS", "1") == "1"

# Changeover a new order brings at least (same-format floor in setup_times).
_MIN_OMBOUW = 20


def _errors(ds: DaySchedule) -> int:
    return sum(1 for w in ds.warnings if w.startswith("[ERROR]"))


def _keys(p: Product) -> tuple[str | None, str | None]:
    return kapvorm_sleutel(p), schaal_sleutel(p)


def _line(ds: DaySchedule, lijn: int):
    return ds.lijn1 if lijn == 1 else ds.lijn2


def _busy(ds: DaySchedule, lijn: int) -> int:
    return sum(
        (po.eind.hour * 60 + po.eind.minute) - (po.start.hour * 60 + po.start.minute)
        + po.ombouw_voor
        for po in _line(ds, lijn)
    )


def _could_fit(ds: DaySchedule, lijn: int, minutes: int) -> bool:
    """
    Cheap necessary condition before a real build: the line's production and
    changeover plus `minutes` (which may be negative for a swap's net change)
    must fit the shift window. A build that overflows hands the day to the
    CP-SAT sequencer for seconds, only to be rejected anyway — this skips those.
    """
    return _busy(ds, lijn) + minutes + _MIN_OMBOUW <= config.window_minutes(ds.datum, lijn)


class _Plan:
    """The window's days, built greedily, with a cache keyed by day content."""

    def __init__(self, orders, products, setup_matrix, toestellen, workdays, vorige):
        self.vorige = vorige
        self.products = products
        self.setup_matrix = setup_matrix
        self.toestellen = toestellen
        self.workdays = workdays
        self.index = {d: i for i, d in enumerate(workdays)}
        self.by_day: dict[date, list[Order]] = defaultdict(list)
        for o in orders:
            self.by_day[o.productie_datum].append(o)
        self._cache: dict[tuple, DaySchedule] = {}
        self.built: dict[date, DaySchedule] = {}
        for d in workdays:  # in order: each day opens on the one before it
            self.built[d] = self.build(d, self.by_day, self.built)

    def closers_before(self, d: date, built: dict[date, DaySchedule]) -> dict[int, Product]:
        """What each line closed on, on its last built workday before `d`."""
        laatste = dict(self.vorige)
        for w in self.workdays:
            if w >= d:
                break
            ds = built.get(w)
            if ds is None:
                continue
            for lijn, planned in ((1, ds.lijn1), (2, ds.lijn2)):
                if planned:
                    laatste[lijn] = closer(planned)
        return laatste

    def volgende_na(self, d: date, by_day: dict[date, list[Order]]) -> dict:
        """Per line, the kapvorm / schaal keys its next workday after `d` runs."""
        out = {}
        for lijn in (1, 2):
            for w in self.workdays[self.index[d] + 1:]:
                ps = [self.products[o.art_nr] for o in by_day.get(w, []) if o.lijn == lijn]
                if ps:
                    out[lijn] = volgende_van(ps)
                    break
        return out

    def build(
        self, d: date, by_day: dict[date, list[Order]], built: dict[date, DaySchedule],
    ) -> DaySchedule:
        day_orders = by_day.get(d, [])
        vorige = self.closers_before(d, built)
        volgende = self.volgende_na(d, by_day)
        key = (
            d, frozenset((id(o), o.lijn) for o in day_orders),
            tuple(sorted((l, p.art_nr) for l, p in vorige.items())),
            tuple(sorted(volgende.items())),
        )
        if key not in self._cache:
            self._cache[key] = schedule_day(
                day_orders, self.products, self.setup_matrix, d, self.toestellen,
                vorige=vorige, volgende=volgende,
            )
        return self._cache[key]

    def score(self, built: dict[date, DaySchedule]) -> tuple[int, int]:
        """(-L1 kapvorm changes, summed line_score): higher is better."""
        total = 0
        laatste = dict(self.vorige)
        for d in self.workdays:
            ds = built[d]
            for lijn, planned in ((1, ds.lijn1), (2, ds.lijn2)):
                if not planned:
                    continue
                total += line_score(planned, lijn, laatste.get(lijn))
                laatste[lijn] = closer(planned)
        l1 = l1_kapvorm_wissels(
            [built[d].lijn1 for d in self.workdays], self.vorige.get(1)
        )
        return -l1, total

    def evaluate(self, changes: list[tuple[Order, date, int]]):
        """
        Apply `changes` tentatively, rebuild the touched days and their
        neighbouring workdays in date order, and return (score, built, by_day)
        — or None when any rebuilt day drops an order or gains an error.
        Orders are always restored; the caller commits a result it accepts.
        """
        by_day = {d: list(v) for d, v in self.by_day.items()}
        saved = [(o, o.productie_datum, o.lijn) for o, _, _ in changes]
        touched: set[date] = set()
        for o, d, lijn in changes:
            by_day[o.productie_datum] = [x for x in by_day[o.productie_datum] if x is not o]
            touched.add(o.productie_datum)
            touched.add(d)
        for o, d, lijn in changes:
            o.productie_datum, o.lijn = d, lijn
            by_day.setdefault(d, []).append(o)
        affected = set(touched)
        for d in touched:
            i = self.index[d]
            affected.update(self.workdays[j] for j in (i - 1, i + 1) if 0 <= j < len(self.workdays))
        try:
            built = dict(self.built)
            for d in self.workdays:
                if d in affected:
                    ds = self.build(d, by_day, built)
                    before = self.built[d]
                    if (
                        len(ds.dropped) > len(before.dropped)
                        or _errors(ds) > _errors(before)
                    ):
                        return None
                    built[d] = ds
            return self.score(built), built, by_day
        finally:
            for o, d, lijn in saved:
                o.productie_datum, o.lijn = d, lijn

    def commit(self, changes, built, by_day) -> None:
        for o, d, lijn in changes:
            o.productie_datum, o.lijn = d, lijn
        self.by_day = defaultdict(list, by_day)
        self.built = built


def _lines_for(order: Order, product: Product) -> set[int]:
    lines = {order.lijn}
    voorkeur = preferred_line(product)
    if voorkeur is not None:
        lines.add(voorkeur)
    return lines


def _legal(order, product, plan: _Plan, start, frozen_until) -> set[tuple[date, int]]:
    ss = slots_mod.legal_slots(order, product, plan.workdays, start, frozen_until)
    lines = _lines_for(order, product)
    return {(plan.workdays[di], lijn) for di, lijn in ss.slots if lijn in lines}


def _hosts(order, product, plan: _Plan, legal) -> list[tuple[date, int]]:
    """Legal (day, line) slots where this order would join or extend a run."""
    kap, sch = _keys(product)

    def shares(d: date, lijn: int) -> bool:
        ds = plan.built.get(d)
        if ds is None:
            return False
        for po in _line(ds, lijn):
            if po.order is order:
                continue
            k, s = _keys(po.product)
            if (kap is not None and k == kap) or (sch is not None and s == sch):
                return True
        return False

    out = []
    for d, lijn in legal:
        if d == order.productie_datum and lijn == order.lijn:
            continue
        i = plan.index[d]
        neighbours = [plan.workdays[j] for j in (i - 1, i, i + 1) if 0 <= j < len(plan.workdays)]
        if any(shares(n, lijn) for n in neighbours):
            out.append((d, lijn))
    return out


def _stranded(order: Order, product: Product, plan: _Plan) -> bool:
    """Alone with its kapvorm or its schaal on a line-day that runs others."""
    planned = _line(plan.built[order.productie_datum], order.lijn)
    for key in (kapvorm_sleutel, schaal_sleutel):
        k = key(product)
        if k is None:
            continue
        c = Counter(key(po.product) for po in planned if key(po.product) is not None)
        if c[k] == 1 and len(c) > 1:
            return True
    return False


def regroup_campaigns(
    orders: list[Order],
    products: dict[str, Product],
    setup_matrix: dict,
    toestellen: dict[str, int] | None,
    start: date,
    end: date,
    voorgaand: list[DaySchedule] | None = None,
    frozen_until: date | None = None,
    budget_seconds: float = BUDGET_SECONDS,
) -> dict:
    """
    Move orders in [start, end) in place (productie_datum, lijn) to group
    kapvorm and schaal across days. Returns a small report dict.
    """
    report = {"moves": 0, "swaps": 0, "evaluated": 0,
              "score_before": 0, "score_after": 0,
              "l1_kapvorm_before": 0, "l1_kapvorm_after": 0, "seconds": 0.0}
    if not ENABLED or end <= start:
        return report
    t0 = _time.monotonic()

    def out_of_time() -> bool:
        return _time.monotonic() - t0 >= budget_seconds

    workdays = slots_mod.workdays_between(start, end)
    in_window = [o for o in orders if start <= o.productie_datum < end]
    if not in_window:
        return report

    counts: dict[str, int] = defaultdict(int)
    for o in orders:
        counts[order_identity(o)] += 1

    vorige: dict[int, Product] = {}
    for ds in sorted(voorgaand or [], key=lambda d: d.datum):
        for lijn, planned in ((1, ds.lijn1), (2, ds.lijn2)):
            if planned and ds.datum < start:
                vorige[lijn] = closer(planned)

    plan = _Plan(in_window, products, setup_matrix, toestellen, workdays, vorige)
    # A day the greedy build already cannot fit is left alone: the final
    # render's sequencer fits it, so there is no clean baseline to beat.
    dirty = {d for d, ds in plan.built.items() if ds.dropped or _errors(ds)}
    current = plan.score(plan.built)
    report["l1_kapvorm_before"], report["score_before"] = -current[0], current[1]

    movable = [
        o for o in in_window
        if counts[order_identity(o)] == 1
        and o.art_nr in products
        and o.lijn in (1, 2)
        and any(_keys(products[o.art_nr]))
    ]
    legal = {
        id(o): _legal(o, products[o.art_nr], plan, start, frozen_until) for o in movable
    }
    movable_ids = {id(o) for o in movable}

    def try_best(candidates) -> bool:
        nonlocal current
        best = None
        for changes in candidates:
            if out_of_time():
                break
            report["evaluated"] += 1
            res = plan.evaluate(changes)
            if res is None:
                continue
            score = res[0]
            if score > current and (best is None or score > best[0]):
                best = (score, changes, res)
        if best is None:
            return False
        score, changes, (_, built, by_day) = best
        plan.commit(changes, built, by_day)
        current = score
        return True

    def move_candidates(order: Order):
        product = products[order.art_nr]
        minutes = calculate_duration(order, product)
        for d, lijn in _hosts(order, product, plan, legal[id(order)]):
            if d in dirty:
                continue
            same_line_day = d == order.productie_datum and lijn == order.lijn
            if not same_line_day and not _could_fit(plan.built[d], lijn, minutes):
                continue
            yield [(order, d, lijn)]

    def swap_candidates(order: Order):
        """Host full: send one of its orders back to the slot this one leaves."""
        product = products[order.art_nr]
        kap, sch = _keys(product)
        src, src_lijn = order.productie_datum, order.lijn
        minutes = calculate_duration(order, product)
        for d, lijn in _hosts(order, product, plan, legal[id(order)]):
            if d in dirty or d == src:
                continue
            if _could_fit(plan.built[d], lijn, minutes):
                continue  # a plain move covers it
            for po in _line(plan.built[d], lijn):
                other = po.order
                if id(other) not in movable_ids or (src, src_lijn) not in legal[id(other)]:
                    continue
                if _keys(po.product) == (kap, sch):
                    continue  # trading like for like gains nothing
                other_min = calculate_duration(other, po.product)
                if not _could_fit(plan.built[d], lijn, minutes - other_min - _MIN_OMBOUW):
                    continue
                if not _could_fit(plan.built[src], src_lijn, other_min - minutes - _MIN_OMBOUW):
                    continue
                yield [(order, d, lijn), (other, src, src_lijn)]

    def swap2_candidates(order: Order):
        """SWAP2 — host full even after one order leaves: two go back instead."""
        product = products[order.art_nr]
        kap, sch = _keys(product)
        src, src_lijn = order.productie_datum, order.lijn
        minutes = calculate_duration(order, product)
        for d, lijn in _hosts(order, product, plan, legal[id(order)]):
            if d in dirty or d == src:
                continue
            if _could_fit(plan.built[d], lijn, minutes):
                continue  # a plain move covers it
            back = [
                po for po in _line(plan.built[d], lijn)
                if id(po.order) in movable_ids
                and (src, src_lijn) in legal[id(po.order)]
                and _keys(po.product) != (kap, sch)
            ]
            for i, a in enumerate(back):
                a_min = calculate_duration(a.order, a.product)
                if _could_fit(plan.built[d], lijn, minutes - a_min - _MIN_OMBOUW):
                    continue  # a single swap covers it
                for b in back[i + 1:]:
                    out_min = a_min + calculate_duration(b.order, b.product)
                    if not _could_fit(plan.built[d], lijn, minutes - out_min - 2 * _MIN_OMBOUW):
                        continue
                    if not _could_fit(plan.built[src], src_lijn, out_min - minutes):
                        continue
                    yield [(order, d, lijn), (a.order, src, src_lijn), (b.order, src, src_lijn)]

    improved = True
    while improved and not out_of_time():
        improved = False
        for order in movable:
            if out_of_time():
                break
            if order.productie_datum in dirty:
                continue
            if try_best(move_candidates(order)):
                report["moves"] += 1
                improved = True
        if not SWAPS:
            continue
        for order in movable:
            if out_of_time():
                break
            if order.productie_datum in dirty:
                continue
            # On L1 any order may be worth trading: a two-order Fitmeals block
            # on the wrong day is not "stranded", but it is still a campaign
            # break the planner wants gone first (22/10).
            if order.lijn != 1 and not _stranded(order, products[order.art_nr], plan):
                continue
            if try_best(swap_candidates(order)):
                report["swaps"] += 1
                improved = True
            elif try_best(swap2_candidates(order)):
                report["swaps"] += 1
                improved = True

    report["l1_kapvorm_after"], report["score_after"] = -current[0], current[1]
    report["seconds"] = round(_time.monotonic() - t0, 1)
    return report
