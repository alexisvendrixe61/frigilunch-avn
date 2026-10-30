"""
slots.py — the FILTER enforcement point.

Every rule that can be expressed as "this order may not run there" lives here,
in one function. The solver only ever chooses from what `legal_slots` returns,
so those rules cannot be violated by construction — there is no need for a
downstream check to catch them, and no way for a solver objective tweak to
trade one away.

Constraints enforced here: C3 (production window), C4 (workdays), C5 (frozen
days), D1 (allowed weekdays), L1/L2 (line permission and exclusion), K1
(planning capacity — via `planning_cap`, applied by the solver's own capacity
rows rather than by slot removal).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from frigilunch.adapters import config
from frigilunch.domain.models import Order, Product
from .calendar_rules import WERKDAGEN, weekdag
from .day_rules import dag_toegelaten
from .line_rules import permitted_lines

# Re-exported so the registry can point at one place for K1.
planning_cap = config.planning_cap


class NoSlotCause(str):
    """Why an order ended up with zero legal slots. Str-valued for easy reporting."""


ARRIVED_AFTER_FREEZE = "arrived_after_freeze"
WINDOW_PASSED = "window_passed"
LINE_DAY_RESTRICTION = "line_day_restriction"


@dataclass
class SlotSet:
    """
    Where one order may legally be produced.

    `slots` are (day_index, lijn) pairs indexing into the caller's `workdays`
    list. Empty slots always come with a `cause` saying why — the three reasons
    are genuinely different problems and used to be reported identically.
    """
    slots: list[tuple[int, int]]
    days: list[int]
    lines: list[int]
    cause: str | None = None

    def __bool__(self) -> bool:
        return bool(self.slots)


def workdays_between(start: date, end: date) -> list[date]:
    """
    C4 — every production day in [start, end).

    Exclusive end: callers derive `end` as start + N weeks, which lands on the
    NEXT window's Monday. Including it would sneak a spurious one-day extra
    week into both the schedulable window and every report built from it.
    """
    out: list[date] = []
    d = start
    while d < end:
        if d.weekday() in WERKDAGEN:
            out.append(d)
        d += timedelta(days=1)
    return out


def production_window(order: Order, earliest_open_day: date) -> tuple[date, date]:
    """
    C3 — the [earliest, latest] production dates for this order, from its
    delivery date and the configured lead-time window.
    """
    timing = config.production_timing()
    latest = order.lever_datum - timedelta(days=timing["min_days"])
    return earliest_open_day, latest


def legal_slots(
    order: Order,
    product: Product,
    workdays: list[date],
    start_date: date,
    frozen_until: date | None = None,
) -> SlotSet:
    """
    Every (day, line) this order may legally occupy, and why it has none.

    `start_date` is the first day the solver may place anything on — normally
    the plan start, but after a previous run's commit it is the freeze boundary
    (C5). `frozen_until` is that boundary when one is in force, and is used
    only to tell the two "no slot" causes apart.

    The three empty-set causes:

      ARRIVED_AFTER_FREEZE  The whole production window sits inside days an
                            earlier run already committed. **No future run can
                            ever place this** — the frozen zone only moves
                            forward past it. Needs a human decision, not
                            another solve.
      WINDOW_PASSED         The window closed before this plan starts. Past due.
      LINE_DAY_RESTRICTION  There are candidate days, but none is a weekday
                            this product may run on, on a line it may use.
    """
    lines = permitted_lines(product)                      # L1, L2
    earliest, latest = production_window(order, start_date)  # C3/C5
    window_ends_before_start = latest < start_date

    days = [
        di for di, dd in enumerate(workdays)
        if earliest <= dd <= latest and dag_toegelaten(product, dd)   # C3, D1
    ]
    slots = [(di, lijn) for di in days for lijn in lines]

    cause = None
    if not slots:
        if window_ends_before_start and frozen_until is not None and latest < frozen_until:
            cause = ARRIVED_AFTER_FREEZE
        elif window_ends_before_start:
            cause = WINDOW_PASSED
        else:
            cause = LINE_DAY_RESTRICTION

    return SlotSet(slots=slots, days=days, lines=lines, cause=cause)


def describe_no_slot(order: Order, cause: str, start_date: date, frozen_until: date | None) -> str:
    """The planner-facing explanation for an order that has nowhere to go."""
    timing = config.production_timing()
    if cause == ARRIVED_AFTER_FREEZE:
        must_by = order.lever_datum - timedelta(days=timing["min_days"])
        return (
            f"ARRIVED-AFTER-FREEZE: this order must be produced by {must_by} "
            f"(delivery {order.lever_datum}), but every day up to {frozen_until} is "
            f"frozen by an earlier run's commit. No solve can ever place it — it needs "
            f"a decision: expedite into the committed plan, move the delivery date, or "
            f"add capacity."
        )
    if cause == WINDOW_PASSED:
        return (
            f"WINDOW-PASSED: the production window for delivery {order.lever_datum} "
            f"closed before this plan starts ({start_date}). Past due, not a capacity "
            f"problem."
        )
    return (
        f"LINE-DAY-RESTRICTION: no production day/line this product is allowed to "
        f"run on falls inside its delivery window before {order.lever_datum}."
    )
