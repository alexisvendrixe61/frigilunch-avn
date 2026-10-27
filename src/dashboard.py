"""
dashboard.py — build the capacity/rejection dashboard payload from an
already-solved plan, and render it into dashboard_template.html.

Shared by gen_chart_data.py (standalone dashboard CLI) and
create_plan.py (so the Excel export and the dashboard always describe
the SAME solve — re-running the solver a second time would double the
runtime and risk a dashboard that doesn't match the Excel, since the
feedback-loop solver is not deterministic between runs).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from . import config, totals
from .constraints import calculate_duration
from .constraints.device_rules import scarce_devices_needed
from .plan_state import order_identity

WERKDAGEN = {0, 1, 2, 3, 4}
PLACEHOLDER = "__DATA_JSON__"


def build_dashboard_payload(
    orders,
    scheduled,
    rejected,
    stats,
    schedules,
    products,
    plan_start: date,
    plan_end: date,
    cycle_weeks: int = 4,
    committed_start: date | None = None,
    committed_end: date | None = None,
    completed_records: dict[str, dict] | None = None,
    completed_source_orders=None,
) -> dict:
    """
    Turn a solved plan into the JSON payload the dashboard template renders.

    `committed_start`/`committed_end` describe the ACTUAL committed window
    (from plan_state.json — see src/plan_state.py) when the caller is doing
    rolling-lock planning. If omitted, falls back to the first `cycle_weeks`
    weeks from `plan_start` — a display-only approximation for callers (like
    gen_chart_data.py) that don't do real cross-run locking.
    """
    if committed_start is None:
        committed_start = plan_start
    if committed_end is None:
        committed_end = plan_start + timedelta(weeks=cycle_weeks) - timedelta(days=1)
    cycle_weeks = max(1, ((committed_end - committed_start).days + 7) // 7)
    proximity_grace_days = config.production_timing()["proximity_grace_days"]

    def cap_for(d: date, lijn: int) -> int:
        """
        The line's real shift window for that day.

        Deliberately the WINDOW, not the solver's planning cap: `used` is now
        actual work minutes (scheduler.werk_minuten), and work can legitimately
        fill the whole window — the planning cap holds back a reserve the day
        builder is still allowed to use. Measuring window-sized work against a
        reserve-sized cap is what let the dashboard report 104% utilization.
        """
        return config.window_minutes(d, lijn)

    # ── Weekly capacity (workdays in plan window) ──
    # Keyed by (iso_year, iso_week), not the bare week number — the plan
    # horizon can now span more than a year (open-ended), and a bare week
    # number collides across years (e.g. week 18 of 2026 and week 18 of
    # 2027 would otherwise merge into one bucket, doubling its capacity/
    # used/rejected totals and scrambling chronological order).
    weeks: dict = {}
    d = plan_start
    while d < plan_end:
        if d.weekday() in WERKDAGEN:
            iso_year, iso_week, _ = d.isocalendar()
            wk = (iso_year, iso_week)
            w = weeks.setdefault(wk, {"week": iso_week, "iso_year": iso_year,
                                      "cap_l1": 0, "cap_l2": 0,
                                      "used_l1": 0, "used_l2": 0,
                                      "rej_min": 0, "rej_orders": 0, "rej_units": 0,
                                      "sched_orders": 0, "sched_units": 0,
                                      "hard_errors": 0, "lead_sum": 0, "lead_count": 0,
                                      "ombouw_min": 0,
                                      "first_day": d, "days": 0})
            w["cap_l1"] += cap_for(d, 1)
            w["cap_l2"] += cap_for(d, 2)
            w["days"] += 1
        d += timedelta(days=1)

    for s in schedules:
        wk = s.datum.isocalendar()[:2]
        if wk in weeks:
            weeks[wk]["used_l1"] += s.kpis.get("lijn1_werk_min", 0)
            weeks[wk]["used_l2"] += s.kpis.get("lijn2_werk_min", 0)
            weeks[wk]["hard_errors"] += sum(1 for w in s.warnings if w.startswith("[ERROR]"))
            weeks[wk]["ombouw_min"] += sum(po.ombouw_voor for po in s.lijn1 + s.lijn2)

    for o in scheduled:
        wk = o.productie_datum.isocalendar()[:2]
        if wk in weeks:
            weeks[wk]["sched_orders"] += 1
            weeks[wk]["sched_units"] += o.aantal
            weeks[wk]["lead_sum"] += (o.lever_datum - o.productie_datum).days
            weeks[wk]["lead_count"] += 1

    # Rejected demand, attributed to the LATEST week it could be produced
    # (production must happen within max_days before delivery, so the
    # pressure lands in the weeks just before the delivery date). Every
    # order's attributed week/day is recorded (see rejected_attribution
    # below) so the frontend can filter by timeframe without re-deriving
    # this clamping logic in JS.
    rejected_attribution: dict[str, dict] = {}
    for o in rejected:
        p = products.get(o.art_nr)
        key = order_identity(o)
        if not p:
            rejected_attribution[key] = {"week": None, "day": None}
            continue
        wk = o.lever_datum.isocalendar()[:2]
        if wk not in weeks:
            cands = [w for w in weeks if w <= wk]
            wk = max(cands) if cands else None
        if wk is not None:
            weeks[wk]["rej_min"] += calculate_duration(o, p)
            weeks[wk]["rej_orders"] += 1
            weeks[wk]["rej_units"] += o.aantal
        rejected_attribution[key] = {"week": wk, "day": None}

    # ── Daily capacity (drilldown behind the weekly view) ──
    days: dict = {}
    d = plan_start
    while d < plan_end:
        if d.weekday() in WERKDAGEN:
            days[d] = {
                "date": d.isoformat(), "week": d.isocalendar()[1],
                "weekday": d.strftime("%a"),
                "cap_l1": cap_for(d, 1), "cap_l2": cap_for(d, 2),
                "used_l1": 0, "used_l2": 0, "hard_errors": 0, "ombouw_min": 0,
                "sched_orders": 0, "sched_units": 0, "rej_min": 0, "rej_orders": 0, "rej_units": 0,
            }
        d += timedelta(days=1)

    for s in schedules:
        if s.datum in days:
            days[s.datum]["used_l1"] += s.kpis.get("lijn1_werk_min", 0)
            days[s.datum]["used_l2"] += s.kpis.get("lijn2_werk_min", 0)
            days[s.datum]["hard_errors"] += sum(1 for w in s.warnings if w.startswith("[ERROR]"))
            days[s.datum]["ombouw_min"] += sum(po.ombouw_voor for po in s.lijn1 + s.lijn2)

    for o in scheduled:
        if o.productie_datum in days:
            days[o.productie_datum]["sched_orders"] += 1
            days[o.productie_datum]["sched_units"] += o.aantal

    for o in rejected:
        p = products.get(o.art_nr)
        key = order_identity(o)
        if not p:
            continue
        dd = o.lever_datum
        if dd not in days:
            cands = [x for x in days if x <= dd]
            dd = max(cands) if cands else None
        if dd is not None:
            days[dd]["rej_min"] += calculate_duration(o, p)
            days[dd]["rej_orders"] += 1
            days[dd]["rej_units"] += o.aantal
        if key in rejected_attribution:
            rejected_attribution[key]["day"] = dd.isoformat() if dd else None

    day_rows = []
    for dd in sorted(days):
        dv = days[dd]
        cap = dv["cap_l1"] + dv["cap_l2"]
        used = dv["used_l1"] + dv["used_l2"]
        day_rows.append({
            "date": dv["date"], "week": dv["week"], "weekday": dv["weekday"],
            "zone": "committed" if committed_start <= dd <= committed_end else "open",
            "capacity": cap, "used": used,
            "cap_l1": dv["cap_l1"], "cap_l2": dv["cap_l2"],
            "used_l1": dv["used_l1"], "used_l2": dv["used_l2"],
            "rejected_min": dv["rej_min"], "rejected_orders": dv["rej_orders"], "rejected_units": dv["rej_units"],
            "scheduled_orders": dv["sched_orders"], "scheduled_units": dv["sched_units"],
            "hard_errors": dv["hard_errors"], "ombouw_min": dv["ombouw_min"],
            "util_l1": round(dv["used_l1"] / dv["cap_l1"] * 100, 1) if dv["cap_l1"] else 0,
            "util_l2": round(dv["used_l2"] / dv["cap_l2"] * 100, 1) if dv["cap_l2"] else 0,
            "util_total": round(used / cap * 100, 1) if cap else 0,
        })

    # ── Planning cycle: the committed window is [committed_start, committed_end], the rest get re-solved ──
    full_days = max((w["days"] for w in weeks.values()), default=0)

    week_rows = []
    week_zone: dict = {}
    for wk in sorted(weeks):
        w = weeks[wk]
        cap = w["cap_l1"] + w["cap_l2"]
        used = w["used_l1"] + w["used_l2"]
        zone = "committed" if committed_start <= w["first_day"] <= committed_end else "open"
        row = {
            "week": w["week"],
            "iso_year": w["iso_year"],
            "zone": zone,
            "capacity": cap,
            "used": used,
            "rejected_min": w["rej_min"],
            "rejected_orders": w["rej_orders"],
            "rejected_units": w["rej_units"],
            "scheduled_orders": w["sched_orders"],
            "scheduled_units": w["sched_units"],
            "hard_errors": w["hard_errors"],
            "ombouw_min": w["ombouw_min"],
            "lead_sum": w["lead_sum"],
            "lead_count": w["lead_count"],
            "util_l1": round(w["used_l1"] / w["cap_l1"] * 100, 1) if w["cap_l1"] else 0,
            "util_l2": round(w["used_l2"] / w["cap_l2"] * 100, 1) if w["cap_l2"] else 0,
            "util_total": round(used / cap * 100, 1) if cap else 0,
        }
        if w["days"] < full_days:
            row["flag"] = f"{w['days']} production day" + ("s" if w["days"] != 1 else "")
        week_rows.append(row)
        week_zone[wk] = zone

    committed = [r for r in week_rows if r["zone"] == "committed"]
    open_rows = [r for r in week_rows if r["zone"] == "open"]

    # Total changeover (ombouw) minutes, split committed (first cycle_weeks,
    # normally 4) vs the rest of the horizon — safe to sum straight from
    # week_rows (unlike order/unit counts, ombouw is a per-schedule-day
    # sequencing artifact, not tied to a split order's identity, so there's
    # no double-counting risk in summing it across weeks).
    committed_ombouw_min = sum(r["ombouw_min"] for r in committed)
    total_ombouw_min = sum(r["ombouw_min"] for r in week_rows)

    # ── Rejected orders, with the reason the scheduler recorded, and the
    # week/zone they're attributed to so the frontend timeframe toggle can
    # filter this list the same way it filters the weekly/daily views. ──
    plan_totals = totals.compute(orders, scheduled, rejected)

    # Rejections are counted straight off the reason strings, so the buckets
    # always sum to the rejected total and "other" stays genuinely residual.
    # It used to be three buckets fed from solver counters, which left every
    # structural rejection (unknown product, no legal day, frozen window)
    # lumped into "other" — on a real run that was 100% of them.
    def _bucket(reason: str) -> str:
        for prefix, name in (
            ("NEVER-PLACED", "never_placed"),
            ("ORACLE-DROPPED", "oracle_dropped"),
            ("ARRIVED-AFTER-FREEZE", "arrived_after_freeze"),
            ("WINDOW-PASSED", "window_passed"),
            ("LINE-DAY-RESTRICTION", "line_day_restriction"),
            ("POSITIE-REGEL", "positie_regel"),
            ("SATE", "sate"),
            ("COMBINED-SCHEDULE-DROP", "combined_drop"),
            ("DAY-BUILD-DROP", "day_build_drop"),
            ("MISSING-SPEED", "missing_speed"),
            ("Product ", "unknown_product"),
        ):
            if reason.startswith(prefix):
                return name
        return "other"

    _DQ_CAUSES = {"missing_speed", "unknown_product"}

    def _breakdown(rows) -> dict:
        out = {
            k: 0 for k in (
                "never_placed", "oracle_dropped", "arrived_after_freeze",
                "window_passed", "line_day_restriction", "positie_regel", "sate",
                "combined_drop", "day_build_drop", "other",
            )
        }
        for r in rows:
            cause = _bucket(r["reason"])
            if cause not in _DQ_CAUSES:
                out[cause] += 1
        return out

    reasons = stats.get("rejection_reasons", {})
    diagnostic_categories = {
        item["order_key"]: item["category"]
        for item in stats.get("rejection_diagnostics", [])
    }
    # Which single-unit machines each rejected order needs. Carried as data (not
    # just prose inside `reason`) so the dashboard can group and filter on it:
    # an order blocked by the one Gernal is a different conversation from one
    # blocked by a full line, and only one of the two is solved by more hours.
    toestellen_avail = config.device_availability()
    rejected_list = [
        {
            "order_nr": o.order_nr,
            "plan_nr": o.plan_nr,
            "art_nr": o.art_nr,
            "klant": o.klant,
            "aantal": o.aantal,
            "lever_datum": o.lever_datum.isoformat(),
            "reason": reasons.get(order_identity(o), "No reason recorded"),
            "cause": _bucket(reasons.get(order_identity(o), "")),
            "diagnostic_category": diagnostic_categories.get(
                order_identity(o), "unknown"
            ),
            "scarce_devices": (
                sorted(scarce_devices_needed(products[o.art_nr], toestellen_avail))
                if o.art_nr in products else []
            ),
            "attributed_week": (rejected_attribution.get(order_identity(o), {}).get("week") or (None, None))[1],
            "zone": week_zone.get(rejected_attribution.get(order_identity(o), {}).get("week")),
        }
        for o in sorted(rejected, key=lambda o: o.lever_datum)
    ]

    dq_rejected_list = [r for r in rejected_list if r["cause"] in _DQ_CAUSES]
    rejected_list    = [r for r in rejected_list if r["cause"] not in _DQ_CAUSES]
    dq_units = sum(r["aantal"] for r in dq_rejected_list)
    dq_totals = {
        "count": len(dq_rejected_list),
        "by_cause": {
            "unknown_product": sum(1 for r in dq_rejected_list if r["cause"] == "unknown_product"),
            "missing_speed":   sum(1 for r in dq_rejected_list if r["cause"] == "missing_speed"),
        },
    }

    # ── Lead time: how many days before delivery each scheduled order was produced ──
    lead_days = [(o.lever_datum - o.productie_datum).days for o in scheduled]
    avg_lead_time_days = round(sum(lead_days) / len(lead_days), 1) if lead_days else 0

    # ── Committed-window totals, computed the same correct way as the global
    # totals above (unique orders, not chunk-rows) — NOT a sum of week_rows'
    # per-week counts, which double-counts a split order once per week its
    # chunks land in. A split order counts as "committed" if ANY of its
    # chunks lands in the committed window (matches the per-week table's
    # own "activity happened here" semantics for that week). ──
    committed_scheduled_keys = {
        order_identity(o) for o in scheduled
        if committed_start <= o.productie_datum <= committed_end
    }
    committed_scheduled_units = sum(
        o.aantal for o in scheduled if committed_start <= o.productie_datum <= committed_end
    )
    committed_rejected = [r for r in rejected_list if r["zone"] == "committed"]
    committed_rejected_units = sum(r["aantal"] for r in committed_rejected)

    committed_lead_days = [
        (o.lever_datum - o.productie_datum).days for o in scheduled
        if committed_start <= o.productie_datum <= committed_end
    ]
    committed_totals = {
        "orders_in_window": len(committed_scheduled_keys) + len(committed_rejected),
        "scheduled": len(committed_scheduled_keys),
        "rejected": len(committed_rejected),
        "scheduled_units": committed_scheduled_units,
        "rejected_units": committed_rejected_units,
        "rejected_by_cause": _breakdown(committed_rejected),
        "hard_errors": sum(w["hard_errors"] for wk, w in weeks.items() if week_zone.get(wk) == "committed"),
        "ombouw_min": committed_ombouw_min,
        "avg_lead_time_days": (
            round(sum(committed_lead_days) / len(committed_lead_days), 1) if committed_lead_days else 0
        ),
    }

    # ── Committed-window order ledger: every order due within the committed
    # cycle, fulfilled or not, so the near-term commitment is auditable
    # order-by-order rather than only as aggregate counts. ──
    scheduled_by_key = {order_identity(o): o for o in scheduled}
    committed_ledger = []
    for o in orders:
        if not (committed_start <= o.lever_datum <= committed_end):
            continue
        key = order_identity(o)
        sched = scheduled_by_key.get(key)
        row = {
            "order_nr": o.order_nr,
            "art_nr": o.art_nr,
            "klant": o.klant,
            "aantal": o.aantal,
            "lever_datum": o.lever_datum.isoformat(),
            "fulfilled": sched is not None,
        }
        if sched is not None:
            row["productie_datum"] = sched.productie_datum.isoformat()
            row["lead_time_days"] = (sched.lever_datum - sched.productie_datum).days
        else:
            row["reason"] = reasons.get(key, "No reason recorded")
        committed_ledger.append(row)
    ledger_keys = {order_identity(o) for o in orders}
    completed_records = completed_records or {}
    for o in completed_source_orders or []:
        key = order_identity(o)
        if key in ledger_keys or not (committed_start <= o.lever_datum <= committed_end):
            continue
        record = completed_records.get(key)
        if not record:
            continue
        production_date = date.fromisoformat(record["production_date"])
        committed_ledger.append({
            "order_nr": o.order_nr,
            "plan_nr": o.plan_nr,
            "art_nr": o.art_nr,
            "klant": o.klant,
            "aantal": o.aantal,
            "lever_datum": o.lever_datum.isoformat(),
            "fulfilled": True,
            "productie_datum": production_date.isoformat(),
            "lead_time_days": (o.lever_datum - production_date).days,
            "completed": True,
        })
    committed_ledger.sort(key=lambda r: r["lever_datum"])

    payload = {
        "generated": date.today().isoformat(),
        "plan_start": plan_start.isoformat(),
        "plan_end": plan_end.isoformat(),
        "delivery_proximity_grace_days": proximity_grace_days,
        "cycle": {
            "committed_weeks": cycle_weeks,
            "committed_from": committed_start.isoformat(),
            "committed_to": committed_end.isoformat(),
            "open_from": (committed_end + timedelta(days=1)).isoformat(),
            "open_to": plan_end.isoformat(),
        },
        "totals": {
            # Counted by src/totals.py — unique order identities, not input
            # rows and not chunk rows, so scheduled + rejected == orders_in_window
            # actually holds. It didn't before: orders_in_window counted rows
            # (174) while scheduled counted identities (166), leaving two
            # orders in neither bucket.
            "orders_in_window": plan_totals.orders_in - dq_totals["count"],
            "scheduled": plan_totals.scheduled_orders,
            "rejected": plan_totals.rejected_orders - dq_totals["count"],
            "scheduled_units": plan_totals.scheduled_units,
            "rejected_units": plan_totals.rejected_units - dq_units,
            "scheduled_chunks": plan_totals.scheduled_chunks,
            "unaccounted": plan_totals.unaccounted,
            "rejected_by_cause": _breakdown(rejected_list),
            "orders_split": plan_totals.orders_split,
            "hard_errors": sum(1 for s in schedules for w in s.warnings if w.startswith("[ERROR]")),
            "ombouw_min": total_ombouw_min,
            "avg_lead_time_days": avg_lead_time_days,
        },
        "committed_totals": committed_totals,
        "weeks": week_rows,
        "days": day_rows,
        "rejected_list": rejected_list,
        "dq_rejected_list": dq_rejected_list,
        "dq_totals": dq_totals,
        "committed_ledger": committed_ledger,
    }
    return payload


def render_dashboard_html(payload: dict, template_path: Path, html_out_path: Path) -> None:
    """Inject the payload into dashboard_template.html. The template holds no data of its own."""
    template = Path(template_path).read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise ValueError(f"{template_path} has no {PLACEHOLDER} placeholder")
    Path(html_out_path).write_text(
        template.replace(PLACEHOLDER, json.dumps(payload, indent=2)), encoding="utf-8"
    )
