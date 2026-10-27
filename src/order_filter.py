"""
order_filter.py — Load orders from Excel, filter them down to a plan window.

  load_excel_orders()      read the "orderverwerking" tab, dedupe exact rows
  filter_by_delivery_range() keep orders delivering inside [start, end]
  filter_by_known_date()   keep only orders actually PLACED by a given date,
                           so a run for week N sees only week N's knowledge
  save_orders_to_json()    persist to data/generated/orders.json
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from .models import Order


def _remark(value) -> str:
    """
    A per-order remark cell as text, or "" when it says nothing.

    Both remark columns use a literal "0" for "no remark" (the analysis column
    is filled on all 1809 rows, but only 925 of them actually say something),
    so "0" is emptiness here, not content — same convention the product
    database uses.
    """
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text in {"0", "0.0", "nan"} else text


def _plan_nr(value) -> str:
    """
    The "plan nr." cell as text.

    pandas reads the column as int64 today (all 1830 rows filled), but a single
    blank cell anywhere turns the whole column into float64 and every number
    into "40781.0", so the trailing ".0" is stripped rather than printed into
    the planning's most-read column.
    """
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def load_excel_orders(path: str | Path, sheet_name: str = "orderverwerking") -> list[Order]:
    """
    Laad orders uit "orderverwerking en planning.xlsm".

    Leest uit het "orderverwerking" sheet (niet "Instructie").

    Verwacht kolommen (Nederlands):
      - order datum: Order datum (identifier)
      - klant: Klant naam
      - art.nr (of art_nr): Artikel nummer
      - datum leveren: Leveringsdatum (delivery date)
      - datum gepland: Geplande productie datum (production deadline)
      - besteld: Hoeveelheid

    Args:
        path: Path to Excel file
        sheet_name: Sheet name to read (default: "orderverwerking")

    Returns:
        list[Order]: Geladen orders
    """
    # Read the correct sheet with header in row 0
    df = pd.read_excel(path, sheet_name=sheet_name, header=0)

    # Kolom normalisatie:
    # 1. Vervang newlines met spaties
    # 2. Strip spaties
    # 3. Lowercase
    df.columns = df.columns.str.replace('\n', ' ').str.strip().str.lower()

    print(f"   [INFO] Sheet geladen: '{sheet_name}'")
    print(f"   [INFO] Aantal rijen: {len(df)}")
    print(f"   [INFO] Kolommen: {list(df.columns)}")

    orders: list[Order] = []
    skipped_rows: list[str] = []

    for idx, row in df.iterrows():
        try:
            # Kolom mapping voor "orderverwerking" sheet
            # Kolommen (na newline replace + lowercase + strip):
            # - order datum (was 'order\ndatum')
            # - klant
            # - art.nr (note: NO space, just dot!)
            # - omschrijving (optional)
            # - datum leveren (was 'datum\nleveren')
            # - besteld

            # Art.nr (exact: no space, just dot)
            if "art.nr" not in df.columns:
                continue
            art_nr = str(row.get("art.nr", "")).strip()
            if not art_nr:
                continue  # Skip rows without art_nr

            # Customer name
            klant = str(row.get("klant", "")).strip()

            # Order identifier: use 'order datum'
            order_datum_val = row.get("order datum")
            if pd.isna(order_datum_val):
                order_nr = f"ORD_{idx}"
            else:
                order_nr = str(order_datum_val).strip()

            # Delivery date: 'datum leveren'
            if "datum leveren" not in df.columns:
                continue

            lever_datum = pd.to_datetime(row.get("datum leveren"), errors="coerce")
            if pd.isna(lever_datum):
                continue
            lever_datum = lever_datum.date()

            # Production deadline: use 'datum gepland' from Excel if available,
            # otherwise fall back to lever_datum.
            #
            # The parse lands in its own name: pd.to_datetime(..., errors="coerce")
            # returns NaT for anything it cannot read, and NaT is not None, so
            # assigning it straight to productie_datum walked it straight past the
            # fallback below and wrote the string "NaT" into orders.json. A cell
            # formatted as time and left at 00:00:00 -- what an empty planned date
            # looks like in this sheet -- is exactly that case.
            #
            # The solver overwrites productie_datum for every order it places, so
            # this value never decides anything; it survives only as a discriminator
            # in the dedup key below, where it keeps genuine sibling line items
            # (same order date, article, customer, quantity and delivery date) apart.
            productie_datum = None
            if "datum gepland" in df.columns:
                gepland_val = row.get("datum gepland")
                if pd.notna(gepland_val):
                    parsed = pd.to_datetime(gepland_val, errors="coerce")
                    if pd.notna(parsed):
                        productie_datum = parsed.date()

            # Fallback: use delivery date if no usable planned date in Excel
            if productie_datum is None:
                productie_datum = lever_datum

            # Quantity: 'besteld'
            if "besteld" not in df.columns:
                continue

            try:
                aantal = int(row.get("besteld", 0))
            except (ValueError, TypeError):
                continue

            if aantal <= 0:
                continue  # Skip rows with 0 quantity

            order = Order(
                order_nr=order_nr,
                art_nr=art_nr,
                klant=klant,
                aantal=aantal,
                lever_datum=lever_datum,
                productie_datum=productie_datum,
                opmerking=_remark(row.get("opmerking")),
                opmerking_analyse=_remark(row.get("opletten opmerking uit analyse")),
                plan_nr=_plan_nr(row.get("plan nr.")),
            )
            orders.append(order)

        except Exception as e:
            # Skip the row, but never silently — a spreadsheet that starts
            # losing rows to a formatting change should be visible, not just
            # produce a quietly smaller plan.
            skipped_rows.append(f"rij {idx + 2}: {type(e).__name__}: {e}")
            continue

    # Exact-duplicate rows (identical on every field) are spreadsheet entry
    # errors, not two separate real orders — confirmed against the source
    # sheet, which contains recurring exact-duplicate rows for the same
    # order date/article/customer/delivery date/quantity. Keep the first
    # occurrence, drop the rest.
    seen: set[tuple] = set()
    deduped: list[Order] = []
    n_dropped = 0
    for order in orders:
        key = (order.order_nr, order.art_nr, order.klant, order.aantal,
               order.lever_datum, order.productie_datum)
        if key in seen:
            n_dropped += 1
            continue
        seen.add(key)
        deduped.append(order)
    if n_dropped:
        print(f"   [WARN] {n_dropped} exact-duplicate order(s) verwijderd (spreadsheet entry errors)")
    if skipped_rows:
        print(f"   [WARN] {len(skipped_rows)} rij(en) overgeslagen door een leesfout:")
        for line in skipped_rows[:10]:
            print(f"           {line}")
        if len(skipped_rows) > 10:
            print(f"           ... en {len(skipped_rows) - 10} meer")

    return deduped


def filter_by_delivery_range(
    orders: list[Order], start_date: date, end_date: date
) -> list[Order]:
    """
    Filter orders within a delivery date range (inclusive).

    Useful for pulling orders across multiple months/weeks.

    Args:
        orders: List of Order objects
        start_date: Earliest delivery date (inclusive)
        end_date: Latest delivery date (inclusive)

    Returns:
        list[Order]: Orders with delivery dates in [start_date, end_date]
    """
    return [o for o in orders if start_date <= o.lever_datum <= end_date]


def filter_by_known_date(orders: list[Order], as_of: date) -> tuple[list[Order], list[str]]:
    """
    Keep only orders that were actually placed on or before `as_of`.

    `order_nr` is not an opaque id — it's the order's placement timestamp
    (e.g. "2026-01-30 00:00:00", see order_nr assignment in
    load_excel_orders()). A run for week N should only see demand that was
    genuinely known by week N; orders "placed" later than `as_of` are future
    information a real point-in-time plan wouldn't have had, so they're
    excluded here rather than filtered on delivery date alone.

    Orders whose order_nr can't be parsed as a date (e.g. the "ORD_{idx}"
    fallback used when the source Excel had no order date) are kept as-is —
    there's no way to know when they became known, so dropping them would
    silently lose real demand. Each such case is reported as a warning.

    Args:
        orders: List of Order objects (typically the not-yet-locked subset)
        as_of: Cutoff date — orders placed after this are excluded

    Returns:
        (kept_orders, warnings)
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
    """
    Save orders to JSON file.

    Args:
        orders: List of Order objects
        output_path: Path to output JSON file
    """
    import json

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert orders to dict format
    orders_data = []
    for order in orders:
        orders_data.append({
            "order_nr": order.order_nr,
            "art_nr": order.art_nr,
            "klant": order.klant,
            "aantal": order.aantal,
            "lever_datum": order.lever_datum.isoformat(),
            "productie_datum": order.productie_datum.isoformat(),
            "opmerking": order.opmerking,
            "opmerking_analyse": order.opmerking_analyse,
            "plan_nr": order.plan_nr,
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(orders_data, f, indent=2, ensure_ascii=False)

    print(f"   [OK] {len(orders)} orders opgeslagen naar {output_path}")
