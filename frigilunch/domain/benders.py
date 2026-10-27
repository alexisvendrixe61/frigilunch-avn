"""
benders.py — Logic-Based Benders feedback loop.

run_benders(orders, products, solvers, config) → SolveOutcome

Iterates until all orders placed, solver gives up, or max_iterations reached.
Callables are injected via a Solvers NamedTuple — no OR-Tools, no I/O here.

Solvers.cp(orders, frozen_dates, cuts, config) → dict:
    'status': 'feasible' | 'infeasible' | 'unknown'
    'assignments': {order_nr: {'date': date, 'lijn': int}}

Solvers.oracle(orders, products, date, config) → DaySchedule

config keys:
    frozen_dates: list[date]  (default [])
    max_iterations: int       (default 10)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Callable, NamedTuple

from .cuts import build_no_good_cut
from .models import DaySchedule, Order, PlanScore, Product, SolveOutcome


class Solvers(NamedTuple):
    """Injectable callables for the Benders loop."""

    cp: Callable   # (orders, frozen, cuts, config) → dict
    oracle: Callable  # (orders, products, date, config) → DaySchedule


class _OracleCtx(NamedTuple):
    """Bundle for oracle-pass helpers — avoids >4 params."""

    products: dict
    solvers: Solvers
    config: dict


# ── helpers ───────────────────────────────────────────────────────────────────

def _group_by_date(
    assignments: dict[str, dict],
    orders: list[Order],
) -> dict[date, list[Order]]:
    """Map production date → orders assigned there."""
    order_map = {o.order_nr: o for o in orders}
    by_date: dict[date, list[Order]] = defaultdict(list)
    for order_nr, slot in assignments.items():
        order = order_map.get(order_nr)
        if order is None:
            continue
        d = slot["date"] if isinstance(slot["date"], date) else date.fromisoformat(slot["date"])
        by_date[d].append(order)
    return dict(by_date)


def _stamp_lijn(order: Order, slot: dict, prod_date: date) -> Order:
    """Return a copy of `order` with assigned lijn and productie_datum."""
    return Order(
        order_nr=order.order_nr, art_nr=order.art_nr, klant=order.klant,
        aantal=order.aantal, lever_datum=order.lever_datum,
        productie_datum=prod_date, lijn=slot.get("lijn", 0),
        plan_nr=order.plan_nr, opmerking=order.opmerking,
    )


def _compute_score(
    scheduled: list[Order],
    rejected: list[Order],
    day_schedules: list[DaySchedule],
) -> PlanScore:
    transition_mins = sum(po.ombouw_voor for d in day_schedules for po in d.lijn1 + d.lijn2)
    return PlanScore(
        rejected_orders=len(rejected), rejected_units=sum(o.aantal for o in rejected),
        transition_minutes=transition_mins, scale_changes=0, early_days=0, moved_forecast_orders=0,
    )


def _outcome(
    status: str,
    scheduled: list[Order],
    rejected: list[Order],
    meta: dict,
) -> SolveOutcome:
    """Build a SolveOutcome from parts.  meta = {'days': ..., 'stats': ...}"""
    days = meta.get("days", [])
    stats = {**meta.get("stats", {}), "days": days}
    return SolveOutcome(
        status=status, scheduled=tuple(scheduled), rejected=tuple(rejected),
        score=_compute_score(scheduled, rejected, days),
        stats=stats,
    )


def _run_oracle_pass(
    by_date: dict[date, list[Order]],
    assignments: dict[str, dict],
    ctx: _OracleCtx,
) -> tuple[list[Order], list[Order], list[DaySchedule], list[dict]]:
    """Run oracle on each production date; return scheduled, dropped, schedules, cuts."""
    all_scheduled: list[Order] = []
    all_dropped: list[Order] = []
    day_schedules: list[DaySchedule] = []
    new_cuts: list[dict] = []

    for prod_date in sorted(by_date):
        day_orders = [_stamp_lijn(o, assignments[o.order_nr], prod_date) for o in by_date[prod_date]]
        schedule = ctx.solvers.oracle(day_orders, ctx.products, prod_date, ctx.config)
        day_schedules.append(schedule)
        placed = {po.order.order_nr for po in schedule.lijn1 + schedule.lijn2}
        for order in day_orders:
            (all_scheduled if order.order_nr in placed else all_dropped).append(order)
        if any(o.order_nr not in placed for o in day_orders):
            new_cuts.append(build_no_good_cut(prod_date, frozenset(o.order_nr for o in by_date[prod_date]), {}))

    return all_scheduled, all_dropped, day_schedules, new_cuts


def _iterate(
    orders: list[Order],
    frozen: list[date],
    cuts: list[dict],
    ctx: _OracleCtx,
) -> tuple[str, dict, list, list, list, list]:
    """Run one solver + oracle iteration.

    Returns (status, assignments, scheduled, dropped, days, new_cuts).
    On solver failure, returns (failure_status, {}, [], [], [], []).
    """
    result = ctx.solvers.cp(orders, frozen, cuts, ctx.config)
    status = result.get("status", "unknown")
    if status in ("infeasible", "unknown"):
        return status, {}, [], [], [], []
    assignments = result.get("assignments", {})
    by_date = _group_by_date(assignments, orders)
    scheduled, dropped, days, new_cuts = _run_oracle_pass(by_date, assignments, ctx)
    return status, assignments, scheduled, dropped, days, new_cuts


# ── main loop ─────────────────────────────────────────────────────────────────

def run_benders(
    orders: list[Order],
    products: dict[str, Product],
    solvers: Solvers,
    config: dict[str, Any],
) -> SolveOutcome:
    """Logic-Based Benders loop: solve → oracle → cut → repeat."""
    max_iter = config.get("max_iterations", 10)
    frozen: list[date] = config.get("frozen_dates", [])
    ctx = _OracleCtx(products=products, solvers=solvers, config=config)
    cuts: list[dict] = []
    stats: dict = {"iterations": 0, "total_oracle_drops": 0}
    assignments: dict[str, dict] = {}
    scheduled, dropped, days = ([] for _ in range(3))

    for i in range(max_iter):
        stats["iterations"] = i + 1
        status, assignments, scheduled, dropped, days, new_cuts = _iterate(
            orders, frozen, cuts, ctx,
        )
        if status in ("infeasible", "unknown"):
            return _outcome(status, [], list(orders), {"days": [], "stats": stats})
        stats["total_oracle_drops"] += len(dropped)
        if not dropped:
            unassigned = [o for o in orders if o.order_nr not in assignments]
            return _outcome("complete", scheduled, unassigned, {"days": days, "stats": stats})
        cuts.extend(new_cuts)

    unassigned = [o for o in orders if o.order_nr not in assignments]
    return _outcome("unknown", scheduled, dropped + unassigned, {"days": days, "stats": stats})
