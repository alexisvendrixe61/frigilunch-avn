"""
totals.py — the one calculation of "how did this plan do".

There used to be three, and they disagreed on the same run:

  create_plan / dashboard  counted unique order IDENTITIES  → 166
  export_multi_to_excel    summed lijn1_orders + lijn2_orders, i.e. CHUNK
                           ROWS, so a split order counted twice
  dashboard "orders_in_window"  counted input ROWS          → 174

174 in, 166 + 6 = 172 accounted for. Two orders were in none of the buckets,
because the input row count and the unique-identity count aren't the same
thing and were being subtracted from each other.

A split order is one order and several production runs. Both numbers are
useful; they just have to be labelled. So:

  orders      unique identities — what a customer would count
  chunks      production runs on the floor — what the shop sees
  units       pieces

`reconcile()` is the invariant: every input order ends up scheduled or
rejected, exactly once. If it doesn't, the caller says so loudly rather than
shipping a plan whose numbers don't add up.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from frigilunch.domain.state import order_identity


@dataclass
class PlanTotals:
    orders_in: int = 0
    scheduled_orders: int = 0
    rejected_orders: int = 0
    scheduled_chunks: int = 0
    scheduled_units: int = 0
    rejected_units: int = 0
    orders_split: int = 0
    unaccounted: list[str] = field(default_factory=list)
    double_counted: list[str] = field(default_factory=list)

    @property
    def fulfillment_pct(self) -> float:
        total = self.scheduled_orders + self.rejected_orders
        return (self.scheduled_orders / total * 100) if total else 0.0

    @property
    def units_pct(self) -> float:
        total = self.scheduled_units + self.rejected_units
        return (self.scheduled_units / total * 100) if total else 0.0

    @property
    def balances(self) -> bool:
        return not self.unaccounted and not self.double_counted


def compute(orders, scheduled, rejected) -> PlanTotals:
    """
    `orders`    every order that went into this run (locked + newly filtered)
    `scheduled` order-chunks that got a production slot (split orders appear
                once per chunk)
    `rejected`  whole orders that did not

    All three are counted by `order_identity`, so a split order contributes
    one to `scheduled_orders` and N to `scheduled_chunks`.
    """
    in_ids = {order_identity(o) for o in orders}

    chunks_per_order: dict[str, int] = {}
    scheduled_units = 0
    for o in scheduled:
        key = order_identity(o)
        chunks_per_order[key] = chunks_per_order.get(key, 0) + 1
        scheduled_units += o.aantal

    rejected_ids = {order_identity(o) for o in rejected}
    sched_ids = set(chunks_per_order)

    t = PlanTotals(
        orders_in=len(in_ids),
        scheduled_orders=len(sched_ids),
        rejected_orders=len(rejected_ids),
        scheduled_chunks=len(scheduled),
        scheduled_units=scheduled_units,
        rejected_units=sum(o.aantal for o in rejected),
        orders_split=sum(1 for n in chunks_per_order.values() if n > 1),
    )
    t.unaccounted = sorted(in_ids - sched_ids - rejected_ids)
    t.double_counted = sorted(sched_ids & rejected_ids)
    return t


def report_imbalance(t: PlanTotals, prefix: str = "[TOTALS]") -> list[str]:
    """Human-readable lines for anything that didn't add up. Empty when clean."""
    out: list[str] = []
    if t.unaccounted:
        out.append(
            f"{prefix} {len(t.unaccounted)} order(s) neither scheduled nor rejected "
            f"— they vanished between input and output: {t.unaccounted[:10]}"
        )
    if t.double_counted:
        out.append(
            f"{prefix} {len(t.double_counted)} order(s) reported BOTH scheduled and "
            f"rejected: {t.double_counted[:10]}"
        )
    return out
