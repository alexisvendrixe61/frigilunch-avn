"""
state.py — Pure functions for rolling-freeze state management.

No file I/O here — that lives in adapters/json_state.py.

State dict format (extends json_state.py schema):
    {
        "schema_version": 1,
        "frozen_until": "YYYY-MM-DD" | null,
        "committed_days": [],          # kept for adapter compatibility
        "frozen_keys": {               # order_key → serialised order data
            "plan:40781": {...}
        },
        "completed_orders": []
    }
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .models import DaySchedule, Order

COMMIT_WEEKS = 4


# ── private helpers ───────────────────────────────────────────────────────────

def _week_monday(week: int, year: int) -> date:
    """Return the Monday of the given ISO week."""
    return date.fromisocalendar(year, week, 1)


def _order_key(order: Order) -> str:
    """Stable identity for an order (prefers plan_nr)."""
    if order.plan_nr:
        return f"plan:{order.plan_nr}"
    return f"{order.order_nr}#{order.art_nr}#{order.klant}#{order.lever_datum.isoformat()}"


def _serialize_order(order: Order) -> dict[str, Any]:
    """Minimal dict for storing an Order in the state."""
    return {
        "order_nr": order.order_nr,
        "art_nr": order.art_nr,
        "klant": order.klant,
        "aantal": order.aantal,
        "lever_datum": order.lever_datum.isoformat(),
        "productie_datum": order.productie_datum.isoformat(),
        "lijn": order.lijn,
        "plan_nr": order.plan_nr,
    }


def _deserialize_order(raw: dict[str, Any]) -> Order:
    """Reconstruct an Order from a serialised state entry."""
    return Order(
        order_nr=raw["order_nr"],
        art_nr=raw["art_nr"],
        klant=raw["klant"],
        aantal=raw["aantal"],
        lever_datum=date.fromisoformat(raw["lever_datum"]),
        productie_datum=date.fromisoformat(raw["productie_datum"]),
        lijn=raw.get("lijn", 0),
        plan_nr=raw.get("plan_nr", ""),
    )


# ── public API ────────────────────────────────────────────────────────────────

def is_frozen(state: dict, order_key: str) -> bool:
    """Return True if `order_key` is committed in the rolling state."""
    return order_key in state.get("frozen_keys", {})


def freeze_window(
    state: dict,
    week: int,
    year: int,
    schedule: list[DaySchedule],
) -> dict:
    """Return a new state with orders inside the commit window frozen.

    Commits all PlannedOrders on days in [monday, monday + COMMIT_WEEKS weeks).
    Existing frozen_keys are preserved; new ones are added.
    """
    monday = _week_monday(week, year)
    commit_end = monday + timedelta(weeks=COMMIT_WEEKS)
    frozen: dict[str, Any] = dict(state.get("frozen_keys", {}))

    for day in schedule:
        if not (monday <= day.datum < commit_end):
            continue
        for po in day.lijn1 + day.lijn2:
            key = _order_key(po.order)
            frozen[key] = _serialize_order(po.order)

    return {
        **state,
        "frozen_until": commit_end.isoformat(),
        "frozen_keys": frozen,
    }


def load_frozen_orders(
    state: dict,
    current_week: int,
    year: int,
) -> list[Order]:
    """Return orders that are frozen and still in the future.

    An order is "still in the future" when its productie_datum ≥ this week's
    Monday.  Orders whose production date has already passed are not returned —
    they were actually produced and need no re-planning.
    """
    monday = _week_monday(current_week, year)
    frozen_until_str = state.get("frozen_until")
    if not frozen_until_str:
        return []
    if date.fromisoformat(frozen_until_str) <= monday:
        return []

    orders: list[Order] = []
    for raw in state.get("frozen_keys", {}).values():
        order = _deserialize_order(raw)
        if order.productie_datum >= monday:
            orders.append(order)
    return orders
