"""
excel_export.py — Write planning output to Excel.

Public API:
    write_excel(outcome, output_dir: Path, week: int) -> None
    write_action_csv(outcome, output_dir: Path, week: int) -> None

Simplified port of src/exporter.py. Writes one row per PlannedOrder,
one sheet per day. The action CSV lists rejected orders for follow-up.
"""
from __future__ import annotations

import csv
from pathlib import Path

from frigilunch.domain.models import DaySchedule, Order, PlannedOrder, SolveOutcome

_ALLERGIE_LABEL = {0: "-", 1: "!", 2: "!!", 3: "!!!", 4: "!!!!", 5: "!!!!!", 6: "X"}
_HEADERS = [
    "plan nr.", "datum", "art.nr.", "omschrijving", "aantal",
    "lever datum", "lijn", "start", "eind", "allergen", "opmerking",
]
_ACTION_HEADERS = ["art.nr.", "klant", "aantal", "lever datum", "reden"]


def _po_row(po: PlannedOrder, datum: object) -> list:
    """Convert a PlannedOrder to one spreadsheet row."""
    o, p = po.order, po.product
    return [
        o.plan_nr, str(datum), o.art_nr, p.naam, o.aantal,
        str(o.lever_datum), po.lijn,
        po.start.strftime("%H:%M"), po.eind.strftime("%H:%M"),
        _ALLERGIE_LABEL.get(p.allergie_niveau, "-"),
        o.opmerking,
    ]


def _write_day_sheet(ws: object, day: DaySchedule) -> None:
    """Write one DaySchedule to an openpyxl worksheet."""
    ws.append(_HEADERS)
    for po in day.lijn1:
        ws.append(_po_row(po, day.datum))
    for po in day.lijn2:
        ws.append(_po_row(po, day.datum))
    if day.warnings:
        ws.append([])
        for w in day.warnings:
            ws.append(["WARN", w])


def write_excel(
    outcome: SolveOutcome,
    output_dir: Path,
    week: int,
) -> None:
    """Write one sheet per production day to planning_wkNN.xlsx."""
    try:
        import openpyxl
    except ImportError:
        print("[WARN] openpyxl not installed — Excel export skipped")
        return

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default empty sheet

    days: list[DaySchedule] = outcome.stats.get("days", [])
    if not days:
        ws = wb.create_sheet("(geen data)")
        ws.append(["Geen productiedagen beschikbaar"])
    else:
        for day in sorted(days, key=lambda d: d.datum):
            title = day.datum.strftime("%Y-%m-%d")
            ws = wb.create_sheet(title)
            _write_day_sheet(ws, day)

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"planning_wk{week:02d}.xlsx"
    wb.save(path)
    print(f"[OK] Excel geschreven: {path}")


def write_action_csv(
    outcome: SolveOutcome,
    output_dir: Path,
    week: int,
) -> None:
    """Write rejected orders to actie_vereist_wkNN.csv."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"actie_vereist_wk{week:02d}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(_ACTION_HEADERS)
        for order in outcome.rejected:
            writer.writerow([
                order.art_nr, order.klant, order.aantal,
                str(order.lever_datum), "rejected",
            ])
    print(f"[OK] Actie CSV geschreven: {path}")


# ── src bridge (consolidated here, not in pipeline) ───────────────────────────

def export_multi_to_excel(
    days: list,
    path: str,
    *,
    assignment_warnings: list | None = None,
    product_warnings: list | None = None,
    rejected_orders: list | None = None,
    rejection_reasons: dict | None = None,
) -> None:
    """Write planning Excel via src.exporter. Bridge lives here, not in pipeline."""
    from src.exporter import export_multi_to_excel as _src_export
    _src_export(
        days,
        path,
        assignment_warnings=assignment_warnings or [],
        product_warnings=product_warnings or [],
        rejected_orders=rejected_orders,
        rejection_reasons=rejection_reasons or {},
    )
