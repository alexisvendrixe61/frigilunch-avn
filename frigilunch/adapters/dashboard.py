"""
dashboard.py — Write a simplified HTML capacity dashboard.

Public API:
    write_html(outcome, output_dir: Path, week: int) -> None

Simplified port of src/dashboard.py. Renders a single-page HTML showing:
  - Summary KPIs (scheduled / rejected / transition minutes)
  - One section per production day with L1/L2 order tables
"""
from __future__ import annotations

from pathlib import Path

from frigilunch.domain.models import DaySchedule, PlannedOrder, SolveOutcome

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
    return f'<div class="kpi"><div class="value">{value}</div><div class="label">{label}</div></div>'


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


def write_html(
    outcome: SolveOutcome,
    output_dir: Path,
    week: int,
) -> None:
    """Write capacity_dashboard_wkNN.html with KPIs and per-day order tables."""
    sc, rc = len(outcome.scheduled), len(outcome.rejected)
    trans = outcome.score.transition_minutes
    kpis = (
        _kpi_box(sc, "gepland") +
        _kpi_box(rc, "geweigerd") +
        _kpi_box(trans, "omboumw-min")
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


# ── src bridge (consolidated here, not in pipeline/cli) ──────────────────────

def build_dashboard_payload(
    all_orders: list,
    all_scheduled: list,
    rejected: list,
    stats: dict,
    days: list,
    src_products: dict,
    plan_start: object,
    plan_end: object,
    **kwargs,
) -> dict:
    """Build the chart-data payload via src.dashboard. Bridge lives here."""
    from src.dashboard import build_dashboard_payload as _src_build
    return _src_build(
        all_orders, all_scheduled, rejected, stats, days,
        src_products, plan_start, plan_end, **kwargs,
    )


def render_dashboard_html(payload: dict, template_path: object, output_path: object) -> None:
    """Render HTML dashboard from payload via src.dashboard. Bridge lives here."""
    from src.dashboard import render_dashboard_html as _src_render
    _src_render(payload, template_path, output_path)
