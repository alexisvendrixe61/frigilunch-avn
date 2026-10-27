"""
cuts.py — Benders feasibility cut generation.

build_no_good_cut(day_order_ids, presence_vars) encodes the feedback from the
oracle back to the CP solver: "at least one of these orders must leave this day."

The cut is returned as a plain dict (not an OR-Tools object) so the domain
layer stays free of solver imports.  The application layer's run_benders_stage
translates the dict into an actual solver constraint.

Schema of the returned dict:
  {
    "type": "no_good",
    "day_date": "YYYY-MM-DD",
    "order_ids": ["id1", "id2", ...],  # the set that caused infeasibility
  }
"""
from __future__ import annotations

from datetime import date
from typing import Any


def build_no_good_cut(
    day_date: date,
    day_order_ids: frozenset[str],
    presence_vars: dict[str, Any],  # order_id → solver variable (opaque)
) -> dict:
    """Return a no-good cut for the given day assignment.

    The cut tells the solver: this exact combination of orders on this day
    was infeasible — at least one must be reassigned.

    `presence_vars` is passed through for the application layer to use when
    adding the actual constraint; the domain layer does not touch it.
    """
    if not day_order_ids:
        raise ValueError("Cannot build a cut for an empty day assignment")

    return {
        "type": "no_good",
        "day_date": day_date.isoformat(),
        "order_ids": sorted(day_order_ids),
        "presence_vars": presence_vars,
    }


def cuts_for_device_pair(
    day_date: date,
    order_pair: tuple[str, str],
    device: str,
    presence_vars: dict[str, Any],
) -> dict:
    """M1 device conflict cut — these two orders cannot share this day.

    `order_pair` is (order_a_id, order_b_id).
    Returned as a dict with 'type': 'device_pair' so the application layer
    can translate it into the appropriate mutual-exclusion constraint.
    """
    return {
        "type": "device_pair",
        "day_date": day_date.isoformat(),
        "order_ids": sorted(order_pair),
        "device": device,
        "presence_vars": presence_vars,
    }
