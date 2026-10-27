"""
excel_orders.py — Load orders from the orderverwerking Excel sheet.

Public API:
    read_orders(path: Path) -> list[Order]

Ports load_excel_orders() from src/order_filter.py.
Each helper is ≤30 lines; no side effects beyond reading the file.
"""
from __future__ import annotations

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
    for idx, row in df.iterrows():
        try:
            order = _parse_row(row, df, idx)
            if order is not None:
                raw.append(order)
        except Exception:
            continue

    return _dedup(raw)
