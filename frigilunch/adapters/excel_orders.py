"""
excel_orders.py — Load orders from the orderverwerking Excel sheet.

Public API:
    load_excel_orders(path, sheet_name) -> list[Order]
    filter_by_delivery_range(orders, start_date, end_date) -> list[Order]
    filter_by_known_date(orders, as_of) -> (list[Order], list[str])
    save_orders_to_json(orders, output_path) -> None
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from frigilunch.domain.models import Order


def _remark(value: object) -> str:
    """Return cell as text, or '' for the spreadsheet's empty sentinels."""
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text in {"0", "0.0", "nan"} else text


def _plan_nr(value: object) -> str:
    """Return plan number as text, stripping float artefact '.0'."""
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_row(row: object, df: pd.DataFrame, idx: int) -> Order | None:
    """Parse a single DataFrame row; return None if required fields are absent."""
    if "art.nr" not in df.columns:
        return None
    art_nr = str(row.get("art.nr", "")).strip()
    if not art_nr or "datum leveren" not in df.columns or "besteld" not in df.columns:
        return None

    lever_datum = pd.to_datetime(row.get("datum leveren"), errors="coerce")
    if pd.isna(lever_datum):
        return None

    order_datum_val = row.get("order datum")
    order_nr = f"ORD_{idx}" if pd.isna(order_datum_val) else str(order_datum_val).strip()

    productie_datum = lever_datum.date()
    if "datum gepland" in df.columns:
        parsed = pd.to_datetime(row.get("datum gepland"), errors="coerce")
        if pd.notna(parsed):
            productie_datum = parsed.date()

    try:
        aantal = int(row.get("besteld", 0))
    except (ValueError, TypeError):
        return None
    if aantal <= 0:
        return None

    return Order(
        order_nr=order_nr,
        art_nr=art_nr,
        klant=str(row.get("klant", "")).strip(),
        aantal=aantal,
        lever_datum=lever_datum.date(),
        productie_datum=productie_datum,
        opmerking=_remark(row.get("opmerking")),
        opmerking_analyse=_remark(row.get("opletten opmerking uit analyse")),
        plan_nr=_plan_nr(row.get("plan nr.")),
    )


def _dedup(orders: list[Order]) -> list[Order]:
    """Remove exact-duplicate rows (same order date/article/client/qty/dates)."""
    seen: set[tuple] = set()
    out: list[Order] = []
    for o in orders:
        key = (o.order_nr, o.art_nr, o.klant, o.aantal, o.lever_datum, o.productie_datum)
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def read_orders(path: Path, sheet_name: str = "orderverwerking") -> list[Order]:
    """Read and deduplicate orders from the orderverwerking Excel sheet."""
    df = pd.read_excel(path, sheet_name=sheet_name, header=0)
    df.columns = df.columns.str.replace("\n", " ").str.strip().str.lower()

    raw: list[Order] = []
    skipped = 0
    for idx, row in df.iterrows():
        try:
            order = _parse_row(row, df, idx)
            if order is not None:
                raw.append(order)
        except Exception:
            skipped += 1
            continue

    deduped = _dedup(raw)
    n_dropped = len(raw) - len(deduped)
    if n_dropped:
        print(f"   [WARN] {n_dropped} exact-duplicate order(s) verwijderd (spreadsheet entry errors)")
    if skipped:
        print(f"   [WARN] {skipped} rij(en) overgeslagen door een leesfout")
    return deduped


load_excel_orders = read_orders


def filter_by_delivery_range(
    orders: list[Order], start_date: date, end_date: date
) -> list[Order]:
    """Keep orders delivering in [start_date, end_date] (both inclusive)."""
    return [o for o in orders if start_date <= o.lever_datum <= end_date]


def filter_by_known_date(
    orders: list[Order], as_of: date
) -> tuple[list[Order], list[str]]:
    """Keep only orders placed on or before `as_of`.

    order_nr is the order's placement timestamp (e.g. "2026-01-30 00:00:00").
    Orders whose order_nr cannot be parsed as a date are kept and warned.
    """
    kept: list[Order] = []
    warnings: list[str] = []
    for o in orders:
        try:
            order_date = date.fromisoformat(o.order_nr.split(" ")[0])
        except ValueError:
            kept.append(o)
            warnings.append(
                f"order_filter: kon order_nr '{o.order_nr}' niet als datum parsen — order behouden"
            )
            continue
        if order_date <= as_of:
            kept.append(o)
    return kept, warnings


def save_orders_to_json(orders: list[Order], output_path: str | Path) -> None:
    """Persist orders to JSON (data/generated/orders.json)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "order_nr": o.order_nr,
            "art_nr": o.art_nr,
            "klant": o.klant,
            "aantal": o.aantal,
            "lever_datum": o.lever_datum.isoformat(),
            "productie_datum": o.productie_datum.isoformat(),
            "opmerking": o.opmerking,
            "opmerking_analyse": o.opmerking_analyse,
            "plan_nr": o.plan_nr,
        }
        for o in orders
    ]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"   [OK] {len(orders)} orders opgeslagen naar {output_path}")
