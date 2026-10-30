"""
dashboard.py — Build the capacity/rejection dashboard payload and render HTML.

Public API:
    build_dashboard_payload(...) -> dict    native, no src bridge
    render_dashboard_html(payload, template_path, output_path) -> None
    write_html(outcome, output_dir, week) -> None   simplified stub
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from frigilunch.adapters.config import device_availability, production_timing, window_minutes
from frigilunch.application.totals import compute as totals_compute
from frigilunch.domain.constraints.changeover import production_minutes as calculate_duration
from frigilunch.domain.constraints.device import scarce_devices_needed
from frigilunch.domain.state import order_identity

WERKDAGEN = {0, 1, 2, 3, 4}
PLACEHOLDER = "__DATA_JSON__"


def build_dashboard_payload(
    all_orders,
    all_scheduled,
    rejected,
    stats: dict,
    schedules,
    products: dict,
    plan_start: date,
    plan_end: date,
    cycle_weeks: int = 4,
    committed_start: date | None = None,
    committed_end: date | None = None,
    completed_records: dict | None = None,
    completed_source_orders=None,
) -> dict:
    """Turn a solved plan into the JSON payload the dashboard template renders."""
    if committed_start is None:
        committed_start = plan_start
    if committed_end is None:
        committed_end = plan_start + timedelta(weeks=cycle_weeks) - timedelta(days=1)
    cycle_weeks = max(1, ((committed_end - committed_start).days + 7) // 7)
    proximity_grace_days = production_timing()["proximity_grace_days"]
    toestellen_avail = device_availability()

    def cap_for(d: date, lijn: int) -> int:
        return window_minutes(d, lijn)

    # ── Weekly capacity ──
    weeks: dict = {}
    d = plan_start
    while d < plan_end:
        if d.weekday() in WERKDAGEN:
            iso_year, iso_week, _ = d.isocalendar()
            wk = (iso_year, iso_week)
            w = weeks.setdefault(wk, {
                "week": iso_week, "iso_year": iso_year,
                "cap_l1": 0, "cap_l2": 0,
                "used_l1": 0, "used_l2": 0,
                "rej_min": 0, "rej_orders": 0, "rej_units": 0,
                "sched_orders": 0, "sched_units": 0,
                "hard_errors": 0, "lead_sum": 0, "lead_count": 0,
                "ombouw_min": 0, "first_day": d, "days": 0,
            })
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

    for o in all_scheduled:
        wk = o.productie_datum.isocalendar()[:2]
        if wk in weeks:
            weeks[wk]["sched_orders"] += 1
            weeks[wk]["sched_units"] += o.aantal
            weeks[wk]["lead_sum"] += (o.lever_datum - o.productie_datum).days
            weeks[wk]["lead_count"] += 1

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

    # ── Daily capacity ──
    days: dict = {}
    d = plan_start
    while d < plan_end:
        if d.weekday() in WERKDAGEN:
            days[d] = {
                "date": d.isoformat(), "week": d.isocalendar()[1],
                "weekday": d.strftime("%a"),
                "cap_l1": cap_for(d, 1), "cap_l2": cap_for(d, 2),
                "used_l1": 0, "used_l2": 0, "hard_errors": 0, "ombouw_min": 0,
                "sched_orders": 0, "sched_units": 0,
                "rej_min": 0, "rej_orders": 0, "rej_units": 0,
            }
        d += timedelta(days=1)

    for s in schedules:
        if s.datum in days:
            days[s.datum]["used_l1"] += s.kpis.get("lijn1_werk_min", 0)
            days[s.datum]["used_l2"] += s.kpis.get("lijn2_werk_min", 0)
            days[s.datum]["hard_errors"] += sum(1 for w in s.warnings if w.startswith("[ERROR]"))
            days[s.datum]["ombouw_min"] += sum(po.ombouw_voor for po in s.lijn1 + s.lijn2)

    for o in all_scheduled:
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
            "rejected_min": dv["rej_min"], "rejected_orders": dv["rej_orders"],
            "rejected_units": dv["rej_units"],
            "scheduled_orders": dv["sched_orders"], "scheduled_units": dv["sched_units"],
            "hard_errors": dv["hard_errors"], "ombouw_min": dv["ombouw_min"],
            "util_l1": round(dv["used_l1"] / dv["cap_l1"] * 100, 1) if dv["cap_l1"] else 0,
            "util_l2": round(dv["used_l2"] / dv["cap_l2"] * 100, 1) if dv["cap_l2"] else 0,
            "util_total": round(used / cap * 100, 1) if cap else 0,
        })

    full_days = max((w["days"] for w in weeks.values()), default=0)
    week_rows = []
    week_zone: dict = {}
    for wk in sorted(weeks):
        w = weeks[wk]
        cap = w["cap_l1"] + w["cap_l2"]
        used = w["used_l1"] + w["used_l2"]
        zone = "committed" if committed_start <= w["first_day"] <= committed_end else "open"
        row = {
            "week": w["week"], "iso_year": w["iso_year"], "zone": zone,
            "capacity": cap, "used": used,
            "rejected_min": w["rej_min"], "rejected_orders": w["rej_orders"],
            "rejected_units": w["rej_units"],
            "scheduled_orders": w["sched_orders"], "scheduled_units": w["sched_units"],
            "hard_errors": w["hard_errors"], "ombouw_min": w["ombouw_min"],
            "lead_sum": w["lead_sum"], "lead_count": w["lead_count"],
            "util_l1": round(w["used_l1"] / w["cap_l1"] * 100, 1) if w["cap_l1"] else 0,
            "util_l2": round(w["used_l2"] / w["cap_l2"] * 100, 1) if w["cap_l2"] else 0,
            "util_total": round(used / cap * 100, 1) if cap else 0,
        }
        if w["days"] < full_days:
            row["flag"] = f"{w['days']} production day" + ("s" if w["days"] != 1 else "")
        week_rows.append(row)
        week_zone[wk] = zone

    committed_rows = [r for r in week_rows if r["zone"] == "committed"]
    committed_ombouw_min = sum(r["ombouw_min"] for r in committed_rows)
    total_ombouw_min = sum(r["ombouw_min"] for r in week_rows)

    # ── Rejection list ──
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
        out = {k: 0 for k in (
            "never_placed", "oracle_dropped", "arrived_after_freeze",
            "window_passed", "line_day_restriction", "positie_regel", "sate",
            "combined_drop", "day_build_drop", "other",
        )}
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
            "diagnostic_category": diagnostic_categories.get(order_identity(o), "unknown"),
            "scarce_devices": (
                sorted(scarce_devices_needed(products[o.art_nr], toestellen_avail))
                if o.art_nr in products else []
            ),
            "attributed_week": (
                rejected_attribution.get(order_identity(o), {}).get("week") or (None, None)
            )[1],
            "zone": week_zone.get(
                rejected_attribution.get(order_identity(o), {}).get("week")
            ),
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

    lead_days = [(o.lever_datum - o.productie_datum).days for o in all_scheduled]
    avg_lead_time_days = round(sum(lead_days) / len(lead_days), 1) if lead_days else 0

    # ── Committed-window totals ──
    committed_scheduled_keys = {
        order_identity(o) for o in all_scheduled
        if committed_start <= o.productie_datum <= committed_end
    }
    committed_scheduled_units = sum(
        o.aantal for o in all_scheduled if committed_start <= o.productie_datum <= committed_end
    )
    committed_rejected = [r for r in rejected_list if r["zone"] == "committed"]
    committed_rejected_units = sum(r["aantal"] for r in committed_rejected)
    committed_lead_days = [
        (o.lever_datum - o.productie_datum).days for o in all_scheduled
        if committed_start <= o.productie_datum <= committed_end
    ]
    committed_totals = {
        "orders_in_window": len(committed_scheduled_keys) + len(committed_rejected),
        "scheduled": len(committed_scheduled_keys),
        "rejected": len(committed_rejected),
        "scheduled_units": committed_scheduled_units,
        "rejected_units": committed_rejected_units,
        "rejected_by_cause": _breakdown(committed_rejected),
        "hard_errors": sum(
            w["hard_errors"] for wk, w in weeks.items() if week_zone.get(wk) == "committed"
        ),
        "ombouw_min": committed_ombouw_min,
        "avg_lead_time_days": (
            round(sum(committed_lead_days) / len(committed_lead_days), 1)
            if committed_lead_days else 0
        ),
    }

    # ── Committed ledger ──
    plan_totals = totals_compute(list(all_orders), list(all_scheduled), list(rejected))
    scheduled_by_key = {order_identity(o): o for o in all_scheduled}
    committed_ledger = []
    for o in all_orders:
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

    ledger_keys = {order_identity(o) for o in all_orders}
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

    return {
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
            "orders_in_window": plan_totals.orders_in - dq_totals["count"],
            "scheduled": plan_totals.scheduled_orders,
            "rejected": plan_totals.rejected_orders - dq_totals["count"],
            "scheduled_units": plan_totals.scheduled_units,
            "rejected_units": plan_totals.rejected_units - dq_units,
            "scheduled_chunks": plan_totals.scheduled_chunks,
            "unaccounted": plan_totals.unaccounted,
            "rejected_by_cause": _breakdown(rejected_list),
            "orders_split": plan_totals.orders_split,
            "hard_errors": sum(
                1 for s in schedules for w in s.warnings if w.startswith("[ERROR]")
            ),
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


def render_dashboard_html(
    payload: dict, template_path: object, output_path: object
) -> None:
    """Inject payload into dashboard_template.html."""
    template = Path(template_path).read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise ValueError(f"{template_path} has no {PLACEHOLDER} placeholder")
    Path(output_path).write_text(
        template.replace(PLACEHOLDER, json.dumps(payload, indent=2)), encoding="utf-8"
    )


# ── simplified write_html (for testing / no template) ────────────────────────

from frigilunch.domain.models import DaySchedule, PlannedOrder, SolveOutcome  # noqa: E402

_CSS = """
body{font-family:sans-serif;margin:20px;background:#f5f5f5}
h1{color:#2c3e50}h2{color:#34495e;border-bottom:2px solid #bdc3c7;padding-bottom:4px}
.kpi-row{display:flex;gap:16px;margin-bottom:24px}
.kpi{background:#fff;border-radius:8px;padding:16px;min-width:120px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.1)}
.kpi .value{font-size:2em;font-weight:bold;color:#2c3e50}
.kpi .label{font-size:.85em;color:#7f8c8d}
table{border-collapse:collapse;width:100%;background:#fff;margin-bottom:16px}
th{background:#2c3e50;color:#fff;padding:6px 10px;text-align:left}
td{padding:5px 10px;border-bottom:1px solid #ecf0f1}
.lijn1 td:first-child{border-left:4px solid #3498db}
.lijn2 td:first-child{border-left:4px solid #9b59b6}
.warn{background:#fff3cd;color:#856404;padding:8px;border-radius:4px;margin:4px 0}
"""
_TABLE_HEADERS = ["plan nr.", "art.nr.", "omschrijving", "aantal", "lever datum",
                  "start", "eind", "allergen"]


def _kpi_box(value: object, label: str) -> str:
    return (
        f'<div class="kpi"><div class="value">{value}</div>'
        f'<div class="label">{label}</div></div>'
    )


def _order_row(po: PlannedOrder, css_class: str) -> str:
    o, p = po.order, po.product
    allergen = {0: "-", 1: "!", 2: "!!", 3: "!!!", 4: "!!!!", 5: "!!!!!", 6: "X"}.get(
        p.allergie_niveau, "-"
    )
    cells = "".join(
        f"<td>{v}</td>"
        for v in [o.plan_nr, o.art_nr, p.naam, o.aantal, o.lever_datum,
                  po.start.strftime("%H:%M"), po.eind.strftime("%H:%M"), allergen]
    )
    return f'<tr class="{css_class}">{cells}</tr>'


def _day_section(day: DaySchedule) -> str:
    header = "".join(f"<th>{h}</th>" for h in _TABLE_HEADERS)
    rows1 = "".join(_order_row(po, "lijn1") for po in day.lijn1)
    rows2 = "".join(_order_row(po, "lijn2") for po in day.lijn2)
    warns = "".join(f'<div class="warn">{w}</div>' for w in day.warnings)
    title = day.datum.strftime("%A %d %B %Y")
    n1, n2 = len(day.lijn1), len(day.lijn2)
    return (
        f"<h2>{title} — L1: {n1} orders, L2: {n2} orders</h2>"
        f"{warns}"
        f"<table><thead><tr>{header}</tr></thead><tbody>{rows1}{rows2}</tbody></table>"
    )


def write_html(outcome: SolveOutcome, output_dir: Path, week: int) -> None:
    """Write capacity_dashboard_wkNN.html with KPIs and per-day order tables."""
    sc, rc = len(outcome.scheduled), len(outcome.rejected)
    trans = outcome.score.transition_minutes
    kpis = (
        _kpi_box(sc, "gepland") + _kpi_box(rc, "geweigerd") + _kpi_box(trans, "ombouw-min")
    )
    days: list[DaySchedule] = outcome.stats.get("days", [])
    sections = "".join(_day_section(d) for d in sorted(days, key=lambda d: d.datum))
    html = (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>Planning week {week}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>Frigilunch planning — week {week}</h1>"
        f'<div class="kpi-row">{kpis}</div>'
        f"{sections}</body></html>"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"capacity_dashboard_wk{week:02d}.html"
    path.write_text(html, encoding="utf-8")
    print(f"[OK] Dashboard geschreven: {path}")
