from __future__ import annotations

from .models import DaySchedule, PlannedOrder
from .constraints import is_sate
from . import totals
from .plan_state import order_identity

# ── kleurcodering (openpyxl verwacht hex zonder #) ───────────────────────────
# Levels are the spreadsheet's own 0-6 classification scale, so the labels
# below print exactly what column 7 holds ("-", "!" .. "!!!!!", "X").
# They used to be shifted one step: level 1 rendered as "!!".
#
# Order rows are NOT coloured by that level any more ("kleuren weg", meeting
# 04/09): a sheet where every row carries a background is unreadable, and the
# level is already written in its own column. Colour is left only on the
# structural rows — headers, LIJN separators, warnings — and on the sate
# marker, which is a marking, not a level.
_ALLERGIE_LABEL = {0: "-", 1: "!", 2: "!!", 3: "!!!", 4: "!!!!", 5: "!!!!!", 6: "X"}
_LIJN1_FILL     = "BDD7EE"
_LIJN2_FILL     = "E2CFEF"
_WARNING_FILL   = "FFE0B2"

# ── sate ─────────────────────────────────────────────────────────────────────
# Level 6 is sate (aardnoten) — the top of the scale, and the one production
# that empties the other line (A5). The source workbook marks it with a bold
# pink "Saté" row directly above the first sate production of a block (90 such
# rows against 104 sate productions), and that is what "aanduiding sate prod
# idem planning" asks for, so it is reproduced here rather than invented.
_SATE_LABEL = "Saté"
_SATE_FILL = "FF6699"


def is_sate_order(po: PlannedOrder) -> bool:
    """Same question A5 asks — asked in the same place, so the export and the
    scheduler can never disagree about what counts as sate."""
    return is_sate(po.product)


def _plan_nr(po: PlannedOrder) -> int | str:
    """
    The order's plan number for the "plan nr." column — as a NUMBER when it is
    one, so the column sorts and filters like the source planning's does
    instead of landing in Excel as left-aligned text.
    """
    nr = po.order.plan_nr
    return int(nr) if nr.isdigit() else nr


# ── hulp mat. ────────────────────────────────────────────────────────────────
# The assembly devices this product occupies, in the source workbook's own
# shorthand ("I-L-2T", "2H-4T-K"): the abbreviations come from "Planning
# info.xlsx" -> "machines assemblage", the counts from the product database's
# "Toestellen Assemblage" block (Product.assemblage_toestellen).
#
# The multiweger prints as "I" on both lines. It is a different machine per
# line — Multipond on L1, Ishida on L2 — but the sheet does not distinguish
# them, and reading the count off the PLANNED line rather than off either
# column loses every product whose database row only fills in the line it
# usually runs on (measured against the source planning: 90.0% agreement when
# read per-line, 93.0% when read as "a multiweger is needed").
#
# Order is fixed (I-L-M-H-T-G-K-CFS), matching the most frequent spellings in
# the source sheet — which is not internally consistent ("I-L-2T" 252x vs
# "L-I-2T-K" 14x, "H-T" 76x vs "T-H" 43x).
_HULP_MAT_VOLGORDE = [
    ("I", ("multipond_l1_nodig", "isihda_l2_nodig")),
    ("L", ("leonard_nodig",)),
    ("M", ("multifill_nodig",)),
    ("H", ("handtmann_aantal",)),
    ("T", ("turbo_aantal",)),
    ("G", ("grote_slicer_nodig",)),
    ("K", ("kaasstrooier_nodig",)),
    ("CFS", ("cfs_pomp_nodig",)),
]


def _hulp_materiaal(product) -> str:
    """
    "hulp mat." for one product, or "" when its database row says nothing
    about assembly devices — 505 of the 1729 source planning rows have no
    device data at all, and a blank cell is the honest rendering of that.
    """
    counts = product.assemblage_toestellen or {}
    parts: list[str] = []
    for afkorting, keys in _HULP_MAT_VOLGORDE:
        n = max((int(counts.get(k) or 0) for k in keys), default=0)
        if n > 0:
            parts.append(f"{n}{afkorting}" if n > 1 else afkorting)
    return "-".join(parts)


_HEADERS = [
    "plan nr.", "datum", "art.nr.", "omschrijving", "aantal", "lever datum",
    "CSB", "man", "lijn", "start hh:mm", "eind hh:mm", "st/hr",
    "turnover", "!!!!!", "V-A", "ma x st", "# we st in cha",
    "schaal", "hulp mat.", "kap vorm", "type karton", "#krt", "opmerking",
    "opmerking van tabblad orderverwerking", "Opmerking uit de analyselijst",
]

_COL_WIDTHS = {
    "A": 9, "B": 12, "C": 13, "D": 42, "E": 8, "F": 13,
    "G": 7, "H": 7, "I": 6, "J": 11, "K": 11, "L": 8,
    "M": 11, "N": 7, "O": 7, "P": 10, "Q": 14, "R": 14,
    "S": 12, "T": 13, "U": 14, "V": 6, "W": 40,
    "X": 40, "Y": 40,
}


# ── Multi-dag Excel export ───────────────────────────────────────────────────

_NL_DAG = {0: "ma", 1: "di", 2: "wo", 3: "do", 4: "vr", 5: "za", 6: "zo"}

# Column layout matches "orderverwerking en planning.xlsm" -> "planning
# maaltijden" (the source workbook's own continuous planning tab) as closely
# as our data supports. Columns we don't compute (CSB, V-A, max st/min, #
# weegschalen, uren steamer) are left blank rather than guessed.
_CONTINU_HEADERS = [
    "plan nr.", "datum", "art.nr.", "omschrijving", "aantal", "lever datum",
    "CSB", "man", "lijn", "start hh:mm", "eind hh:mm", "",
    "st/hr", "! ! ! ! !", "V-A", "max st/min", "# weegschalen",
    "schaal", "hulp mat.", "kap vorm", "type karton", "#krt",
    "opmerking van tabblad orderverwerking", "Opmerking uit de analyselijst",
    "uren steamer",
]


def _write_continuous_planning_sheet(ws, schedules: list[DaySchedule]) -> None:
    """
    One flat row per order across the whole plan, in the same column order
    as the source workbook's "planning maaltijden" tab — no per-day tabs,
    no LIJN section headers, just a continuous sheet sorted by
    (datum, lijn, start).
    """
    from openpyxl.styles import Alignment, Font, PatternFill

    def mf(hex_color):
        return PatternFill("solid", fgColor=hex_color)

    ws.append(_CONTINU_HEADERS)
    for cell in ws[1]:
        cell.fill = mf(_LIJN1_FILL)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    widths = ["9", "12", "13", "42", "8", "13", "7", "7", "6", "11", "11",
              "6", "8", "9", "6", "10", "12", "13", "12", "12", "14", "6",
              "34", "34", "10"]
    for col_idx, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = int(w)

    rows: list[tuple] = []
    for s in schedules:
        for po in list(s.lijn1) + list(s.lijn2):
            rows.append((s.datum, po.lijn, po.start, s, po))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))

    vorige_sate: tuple | None = None   # (datum, lijn) of the row above, if sate
    vorige_datum = None                # the day of the row above, for the break
    for datum, lijn, start, s, po in rows:
        allergie_label = _ALLERGIE_LABEL.get(po.product.allergie_niveau, "")
        krt = (po.order.aantal // po.product.stuks_per_karton
               if po.product.stuks_per_karton else 0)

        # One blank row between days, so a continuous sheet of ~480 rows still
        # reads as a sequence of production days. The source workbook separates
        # its day blocks the same way.
        if vorige_datum is not None and datum != vorige_datum:
            ws.append([""] * len(_CONTINU_HEADERS))
        vorige_datum = datum

        # "Saté" marker above the first sate production of each block, exactly
        # as the source planning marks it. A block is consecutive sate on one
        # line of one day, so a second sate straight after gets no second
        # marker.
        if is_sate_order(po):
            if vorige_sate != (datum, lijn):
                ws.append(["", "", "", _SATE_LABEL] + [""] * (len(_CONTINU_HEADERS) - 4))
                for cell in ws[ws.max_row]:
                    cell.fill = mf(_SATE_FILL)
                    cell.font = Font(bold=True)
            vorige_sate = (datum, lijn)
        else:
            vorige_sate = None

        ws.append([
            _plan_nr(po),
            s.datum.strftime("%d/%m/%Y"),
            po.order.art_nr,
            po.product.naam,
            po.order.aantal,
            po.order.lever_datum.strftime("%d/%m/%Y"),
            "",                             # CSB
            f"{po.man:.1f}",
            po.lijn,
            po.start.strftime("%H:%M"),
            po.eind.strftime("%H:%M"),
            # The source workbook's own unnamed turnover column (its row 2
            # carries a bare "20"), so the changeover goes here rather than in
            # a separate row.
            po.ombouw_voor if po.ombouw_voor > 0 else "",
            po.product.snelheid,
            allergie_label,
            "",                             # V-A
            "",                             # max st/min
            "",                             # # weegschalen
            po.product.schaal,
            _hulp_materiaal(po.product),
            po.product.kapvorm,
            po.product.type_karton,
            krt,
            # These two are the ORDER's own remarks, straight from the
            # orderverwerking tab — which is what the headers have always
            # promised. The product-level remark and this run's own warnings
            # stay on the day sheets, in their "opmerking" column.
            po.order.opmerking,
            po.order.opmerking_analyse,
            "",                             # uren steamer
        ])

    ws.freeze_panes = "A2"


def _write_day_to_sheet(ws, schedule: DaySchedule, openpyxl_module) -> None:
    """Schrijft een DaySchedule naar een bestaand openpyxl-werkblad."""
    from openpyxl.styles import Alignment, Font, PatternFill

    def mf(hex_color):
        return PatternFill("solid", fgColor=hex_color)

    bold = Font(bold=True)
    center = Alignment(horizontal="center", vertical="center")

    for col_letter, width in _COL_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width

    ws.append(_HEADERS)
    for cell in ws[ws.max_row]:
        cell.fill = mf(_LIJN1_FILL)
        cell.font = bold
        cell.alignment = center

    def write_lijn_header(lijn_nr):
        color = _LIJN1_FILL if lijn_nr == 1 else _LIJN2_FILL
        ws.append([f"LIJN {lijn_nr}"] + [""] * (len(_HEADERS) - 1))
        for cell in ws[ws.max_row]:
            cell.fill = mf(color)
            cell.font = bold

    def write_sate_label():
        ws.append(["", "", "", _SATE_LABEL] + [""] * (len(_HEADERS) - 4))
        for cell in ws[ws.max_row]:
            cell.fill = mf(_SATE_FILL)
            cell.font = bold

    def write_order_row(po: PlannedOrder):
        allergie_label = _ALLERGIE_LABEL.get(po.product.allergie_niveau, "")
        krt = (po.order.aantal // po.product.stuks_per_karton
               if po.product.stuks_per_karton else 0)
        opmerking = po.product.opmerking
        if po.warnings:
            suffix = " | ".join(po.warnings)
            opmerking = f"{opmerking} | {suffix}" if opmerking else suffix

        ws.append([
            _plan_nr(po),
            schedule.datum.strftime("%d/%m/%Y"),
            po.order.art_nr, po.product.naam, po.order.aantal,
            po.order.lever_datum.strftime("%d/%m/%Y"),
            "", f"{po.man:.1f}", po.lijn,
            po.start.strftime("%H:%M"), po.eind.strftime("%H:%M"),
            po.product.snelheid,
            # Turnover: the changeover this order needs BEFORE it can start.
            # It used to be a separate grey "OMBOUW n min" row with its own
            # start/end times; the floor reads it as a property of the order,
            # so it lives in the order's own turnover cell. Blank, not 0, when
            # there is none — the first order of a line has nothing to change
            # over from.
            po.ombouw_voor if po.ombouw_voor > 0 else "",
            allergie_label,
            "", "", "",
            po.product.schaal,
            _hulp_materiaal(po.product),
            po.product.kapvorm,
            po.product.type_karton, krt, opmerking,
            po.order.opmerking, po.order.opmerking_analyse,
        ])

    def write_lijn(lijn_nr: int, planned: list[PlannedOrder]):
        write_lijn_header(lijn_nr)
        vorige_sate = False
        for po in planned:
            if is_sate_order(po):
                if not vorige_sate:
                    write_sate_label()
                vorige_sate = True
            else:
                vorige_sate = False
            write_order_row(po)

    write_lijn(1, schedule.lijn1)

    ws.append([""] * len(_HEADERS))

    write_lijn(2, schedule.lijn2)

    if schedule.warnings:
        ws.append([""] * len(_HEADERS))
        ws.append(["WARNINGS / MELDINGEN"] + [""] * (len(_HEADERS) - 1))
        for cell in ws[ws.max_row]:
            cell.fill = mf(_WARNING_FILL)
            cell.font = bold
        for w in schedule.warnings:
            ws.append(["", w] + [""] * (len(_HEADERS) - 2))

    kpis = schedule.kpis
    ws.append([""] * len(_HEADERS))
    ws.append([
        "KPI",
        f"L1: {kpis.get('lijn1_orders',0)} orders "
        f"({kpis.get('lijn1_bezetting_pct',0):.1f}% bezet)",
        f"L2: {kpis.get('lijn2_orders',0)} orders "
        f"({kpis.get('lijn2_bezetting_pct',0):.1f}% bezet)",
    ] + [""] * (len(_HEADERS) - 3))


def export_multi_to_excel(
    schedules: list[DaySchedule],
    output_path: str,
    assignment_warnings: list[str] | None = None,
    product_warnings: list[str] | None = None,
    rejected_orders: list = None,
    rejection_reasons: dict | None = None,
) -> None:
    """
    Exporteer een lijst van DaySchedule objecten naar een multi-tab Excel werkboek.

    Structuur:
      - "Overzicht"  : samenvatting per dag (orders, bezetting, errors)
      - Per dag      : gedetailleerde planning per lijn
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as e:
        raise ImportError("openpyxl is vereist: pip install openpyxl") from e

    wb = Workbook()

    # ── Overzicht-tab ─────────────────────────────────────────────────────────
    ws_ov = wb.active
    ws_ov.title = "Overzicht"

    # ── Order Fulfillment Summary ──────────────────────────────────────────────
    # Counted by src/totals.py so this agrees with the dashboard and stdout.
    # It used to sum lijn1_orders + lijn2_orders, i.e. production RUNS, which
    # counts a split order twice and made the Excel disagree with every other
    # report of the same plan.
    planned_chunks = [po.order for s in schedules for po in (s.lijn1 + s.lijn2)]
    t = totals.compute(
        planned_chunks + list(rejected_orders or []),
        planned_chunks,
        list(rejected_orders or []),
    )

    # Add summary header
    ws_ov.append(["ORDER FULFILLMENT SUMMARY"])
    for cell in ws_ov[ws_ov.max_row]:
        cell.fill = PatternFill("solid", fgColor="E8F5E9")
        cell.font = Font(bold=True, size=12)

    ws_ov.append([
        f"Orders: {t.scheduled_orders + t.rejected_orders} | Ingepland: {t.scheduled_orders} "
        f"({t.fulfillment_pct:.1f}%) | Afgewezen: {t.rejected_orders}"
    ])
    ws_ov.append([
        f"Stuks: {t.scheduled_units + t.rejected_units:,} | Ingepland: {t.scheduled_units:,} "
        f"({t.units_pct:.1f}%) | Afgewezen: {t.rejected_units:,}"
    ])
    ws_ov.append([
        f"Productieruns: {t.scheduled_chunks} "
        f"({t.orders_split} order(s) gesplitst over meerdere dagen)"
    ])
    ws_ov.append([])  # Blank line

    ov_headers = [
        "Datum", "Dag", "Week", "L1 orders", "L1 %", "L2 orders", "L2 %",
        "Errors", "Warnings", "MEP items",
    ]
    ws_ov.append(ov_headers)
    for cell in ws_ov[ws_ov.max_row]:   # was ws_ov[1] — the title row, not this one
        cell.fill = PatternFill("solid", fgColor=_LIJN1_FILL)
        cell.font = Font(bold=True)

    for s in schedules:
        weekdag = _NL_DAG.get(s.datum.weekday(), "")
        week_nr = s.datum.isocalendar()[1]
        errors = sum(1 for w in s.warnings if w.startswith("[ERROR]"))
        warns  = sum(1 for w in s.warnings if "[WARN" in w)
        mep_n  = len(s.kpis.get("mep_items", []))
        ws_ov.append([
            s.datum.strftime("%d/%m/%Y"),
            weekdag,
            week_nr,
            s.kpis.get("lijn1_orders", 0),
            f"{s.kpis.get('lijn1_bezetting_pct', 0):.1f}%",
            s.kpis.get("lijn2_orders", 0),
            f"{s.kpis.get('lijn2_bezetting_pct', 0):.1f}%",
            errors, warns, mep_n,
        ])
        # Kleur rij rood als errors, oranje als bezetting > 90%
        row = ws_ov.max_row
        if errors:
            for cell in ws_ov[row]:
                cell.fill = PatternFill("solid", fgColor="FFC7CE")
        elif (s.kpis.get("lijn1_bezetting_pct", 0) > 90
              or s.kpis.get("lijn2_bezetting_pct", 0) > 90):
            for cell in ws_ov[row]:
                cell.fill = PatternFill("solid", fgColor="FFCC99")

    # Onplanbare orders blok
    if assignment_warnings:
        ws_ov.append([])
        ws_ov.append(["ONPLANBARE / OVERLADEN ORDERS"] + [""] * (len(ov_headers) - 1))
        for cell in ws_ov[ws_ov.max_row]:
            cell.fill = PatternFill("solid", fgColor="FFC7CE")
            cell.font = Font(bold=True)
        for w in assignment_warnings:
            ws_ov.append(["", w])

    # Auto-aangevulde producten blok
    if product_warnings:
        ws_ov.append([])
        ws_ov.append(["AUTO-AANGEVULDE PRODUCTEN (check allergeenprofiel!)"]
                     + [""] * (len(ov_headers) - 1))
        for cell in ws_ov[ws_ov.max_row]:
            cell.fill = PatternFill("solid", fgColor=_WARNING_FILL)
            cell.font = Font(bold=True)
        for w in product_warnings:
            ws_ov.append(["", w])

    # Kolombreedtes overzicht
    ws_ov.column_dimensions["A"].width = 13
    ws_ov.column_dimensions["B"].width = 6
    ws_ov.column_dimensions["C"].width = 7
    for col in "DEFGHIJ":
        ws_ov.column_dimensions[col].width = 12

    # ── Continue planning-tab (zelfde kolomformaat als de bron: "orderverwerking
    # en planning.xlsm" -> "planning maaltijden") — alle dagen in één blad ──────
    ws_continu = wb.create_sheet(title="planning maaltijden")
    _write_continuous_planning_sheet(ws_continu, schedules)

    # ── Een tab per dag ───────────────────────────────────────────────────────
    for s in schedules:
        dag_label = _NL_DAG.get(s.datum.weekday(), "")
        sheet_name = f"{s.datum.strftime('%d-%m')} {dag_label}"
        ws_day = wb.create_sheet(title=sheet_name)

        # Datum-header bovenaan blad
        ws_day.append([
            f"Planning  {dag_label.upper()}  {s.datum.strftime('%d/%m/%Y')}",
        ])
        for cell in ws_day[1]:
            cell.font = Font(bold=True, size=12)
            cell.fill = PatternFill(
                "solid",
                fgColor=_LIJN1_FILL if s.datum.weekday() < 4 else "E8F5E9",
            )

        ws_day.append([""] * len(_HEADERS))
        _write_day_to_sheet(ws_day, s, None)

    # ── Afgewezen Orders tab ──────────────────────────────────────────────────────
    if rejected_orders:
        from openpyxl.styles import Alignment, Font, PatternFill
        ws_rejected = wb.create_sheet(title="Afgewezen Orders")

        # Headers. "plan nr." first for the same reason the planning tabs now
        # lead with it: it is the number the office looks an order up by. The
        # old first column was headed "Order Nr" but held `order_nr`, which is
        # the order's placement DATE — that date keeps its place under a name
        # that says so rather than being dropped.
        headers = ["plan nr.", "Art.Nr", "Klant", "Aantal", "Lever Datum",
                   "Besteld op", "Reden"]
        ws_rejected.append(headers)
        for cell in ws_rejected[1]:
            cell.fill = PatternFill("solid", fgColor="FFC7CE")
            cell.font = Font(bold=True, color="FFFFFF")

        # Data rows
        if rejection_reasons is None:
            rejection_reasons = {}

        for order in rejected_orders:
            # Identity must match scheduler_ortools_v3.py's rejection_reasons keys
            key = order_identity(order)
            reason = rejection_reasons.get(key, "Onbekend")
            ws_rejected.append([
                int(order.plan_nr) if order.plan_nr.isdigit() else order.plan_nr,
                order.art_nr,
                order.klant,
                order.aantal,
                order.lever_datum.isoformat() if order.lever_datum else "",
                order.order_nr,
                reason,
            ])

        # Column widths
        ws_rejected.column_dimensions["A"].width = 9
        ws_rejected.column_dimensions["B"].width = 15
        ws_rejected.column_dimensions["C"].width = 20
        ws_rejected.column_dimensions["D"].width = 10
        ws_rejected.column_dimensions["E"].width = 15
        ws_rejected.column_dimensions["F"].width = 20
        ws_rejected.column_dimensions["G"].width = 50

    wb.save(output_path)
    print(f"Planning opgeslagen: {output_path}  ({len(schedules)} dagen, {wb.sheetnames[0]} + {len(schedules)} dag-tabs)")
