"""
layout_metrics.py — how the plan LOOKS to the floor, as numbers.

The planner's 02/10 feedback was about shape, not volume: days that stop hours
before the end of the shift, holes in the middle of a line, identical products
on different days, the same kapvorm broken up. Totals (src/totals.py) cannot
see any of that. These can, so a run — and an A/B between two settings — can
be judged on it.
"""
from __future__ import annotations

from datetime import date

from . import config
from .constraints.grouping_rules import kapvorm_sleutel, schaal_sleutel
from .day_quality import adjacency_counts, idle_minutes
from .models import DaySchedule, PlannedOrder, Product


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def line_day_metrics(planned: list[PlannedOrder], datum: date, lijn_nr: int) -> dict:
    """Idle minutes inside the line-day and minutes left unused before shift end."""
    if not planned:
        return {"idle_min": 0, "end_gap_min": 0, "orders": 0}
    last = max(_minutes(po.eind) for po in planned)
    return {
        "idle_min": idle_minutes(planned, lijn_nr),
        "end_gap_min": max(0, _minutes(config.shift_end(datum)) - last),
        "orders": len(planned),
    }


def _kapvorm_breaks(planned: list[PlannedOrder], key=kapvorm_sleutel) -> int:
    """Extra runs of a kapvorm on one line-day: H22 H25 H22 is one break."""
    seq = [key(po.product) for po in sorted(planned, key=lambda p: _minutes(p.start))]
    runs: dict[str, int] = {}
    prev = None
    for key in seq:
        if key is not None and key != prev:
            runs[key] = runs.get(key, 0) + 1
        prev = key
    return sum(n - 1 for n in runs.values())


def _wissels(keys: list[str | None]) -> int:
    """Changes between consecutive known keys; an unknown one (NVT) is skipped."""
    known = [k for k in keys if k is not None]
    return sum(1 for a, b in zip(known, known[1:]) if a != b)


def l1_kapvorm_wissels(days: list[list[PlannedOrder]], vorige: Product | None = None) -> int:
    """
    G2 on L1 — every kapvorm change on the automatic packing line, following it
    through `days` (its line-days in date order) as one sequence, opened by
    `vorige`, what it closed on before the first of them. The number the
    planner's 22/10 rule asks to minimise above everything else.

    Unlike `_wissels`, "NVT" counts as a kapvorm of its own here: on L1 going
    from no lid to H25 is a changeover too (the validator's KAPVORM_WISSEL
    already reports NVT -> H25).
    """
    keys = [kapvorm_sleutel(vorige) or "NVT"] if vorige is not None else []
    for day in days:
        keys += [
            kapvorm_sleutel(po.product) or "NVT"
            for po in sorted(day, key=lambda p: _minutes(p.start))
        ]
    return sum(1 for a, b in zip(keys, keys[1:]) if a != b)


def plan_layout_summary(
    day_schedules: list[DaySchedule],
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """
    Totals over the line-days in [start, end). `identiek_groups_split` counts
    groups whose orders landed on more than one (day, line);
    `identiek_not_adjacent` counts groups that share a line-day but not back to
    back, smallest first. `kapvorm_wissels` / `schaal_wissels` follow each line
    through the whole window as one sequence, so a change at a day boundary
    counts too (G2 / G3 over days); `*_dag_wissels` is the part of that which
    falls on a day boundary.
    """
    out = {
        "idle_min": 0, "end_gap_min": 0, "active_line_days": 0,
        "identiek_groups_split": 0, "identiek_not_adjacent": 0, "kapvorm_breaks": 0,
        "schaal_breaks": 0, "kapvorm_wissels": 0, "schaal_wissels": 0,
        "kapvorm_dag_wissels": 0, "schaal_dag_wissels": 0, "l1_kapvorm_wissels": 0,
    }
    per_line: dict[int, list[list[PlannedOrder]]] = {1: [], 2: []}
    slots_by_group: dict[str, set[tuple[date, int]]] = {}
    for ds in day_schedules:
        if (start and ds.datum < start) or (end and ds.datum >= end):
            continue
        for lijn_nr, planned in ((1, ds.lijn1), (2, ds.lijn2)):
            if not planned:
                continue
            m = line_day_metrics(planned, ds.datum, lijn_nr)
            out["idle_min"] += m["idle_min"]
            out["end_gap_min"] += m["end_gap_min"]
            out["active_line_days"] += 1
            out["kapvorm_breaks"] += _kapvorm_breaks(planned)
            out["schaal_breaks"] += _kapvorm_breaks(planned, schaal_sleutel)
            per_line[lijn_nr].append(sorted(planned, key=lambda p: _minutes(p.start)))

            members: dict[str, int] = {}
            for po in planned:
                g = po.product.identiek_groep
                if g:
                    members[g] = members.get(g, 0) + 1
                    slots_by_group.setdefault(g, set()).add((ds.datum, lijn_nr))
            ident_pairs, _, _ = adjacency_counts(planned)
            out["identiek_not_adjacent"] += max(
                0, sum(n - 1 for n in members.values()) - ident_pairs
            )
    out["identiek_groups_split"] = sum(1 for s in slots_by_group.values() if len(s) > 1)
    for name, key in (("kapvorm", kapvorm_sleutel), ("schaal", schaal_sleutel)):
        for days in per_line.values():
            totaal = _wissels([key(po.product) for day in days for po in day])
            binnen = sum(_wissels([key(po.product) for po in day]) for day in days)
            out[f"{name}_wissels"] += totaal
            out[f"{name}_dag_wissels"] += totaal - binnen
    out["l1_kapvorm_wissels"] = l1_kapvorm_wissels(per_line[1])
    return out


def format_summary(summary: dict) -> str:
    return (
        f"idle {summary['idle_min']} min, eind-gap {summary['end_gap_min']} min, "
        f"{summary['active_line_days']} lijn-dagen, "
        f"identiek gesplitst {summary['identiek_groups_split']}, "
        f"identiek niet aansluitend {summary['identiek_not_adjacent']}, "
        f"kapvorm-breuken {summary['kapvorm_breaks']}, "
        f"schaal-breuken {summary['schaal_breaks']}, "
        f"kapvorm-wissels {summary['kapvorm_wissels']} "
        f"(waarvan {summary['kapvorm_dag_wissels']} over dagen), "
        f"schaal-wissels {summary['schaal_wissels']} "
        f"(waarvan {summary['schaal_dag_wissels']} over dagen), "
        f"L1 kapvorm-wissels incl. NVT {summary['l1_kapvorm_wissels']}"
    )
