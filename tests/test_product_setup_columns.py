"""
The product database is read by header, not by column number.

October 2026: the client inserted "Kliklok L2" at column 52 and every column
after it moved one place right. With fixed indices that silently gave 560 of
627 products the neighbouring allergen and dropped Monday/Thursday for 101 of
them. These tests pin that an inserted column changes nothing.
"""
from __future__ import annotations

import openpyxl
import pytest

from src.product_setup_loader import load_product_setup_from_excel

SECTIONS = {
    1: "Product Informatie", 8: "Personeel", 16: "Prio Lijn", 19: "Spiraal ",
    21: "Planning", 33: "Toestellen Keuken", 39: "Toestellen Assemblage",
    49: "Kartonnage", 55: "Kruiscontaminatie (enkel keuken)",
}
NAMES = [
    "Product     nummer", "Product naam", "schaal", "kapvorm", "karton", "flowpack",
    "Allergeen", "Assemblage L1", "Assemblage L2", "Kartonnage", "Keuken",
    "Snelheid L1", "Snelheid L2", "Opmerking", "identiek product", "L1", "L2", "CFS",
    "L1", "L2", "Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "MEP D-1",
    "MEP > D-1", "opstart moet", "opstart kan niet", "afsluit moet", "afsluit kan niet",
    "vis verzagen", "Gernal (1)", "Terlet (4)", "Berief (3)", "KS menger (1)",
    "Stephan mixer kl", "Stephan mixer gr", "Leonard (1)", "weegschalen (16)",
    "Multifill (1)", "kaasstrooi(1)", "Turbo(8 enkel -3 dubbel)", "Handtmann (5)",
    "Multipond (L1)", "Isihda (L2)", "Grote (2)", "CFS pomp (1)", "Kliklok", "Shrink",
    "CPS", "Hand nipak", "oude Kliklok (kooien)", "kooien inpak kart", "Aardnoten",
    "Eieren", "Gluten", "Lupine", "Melk", "Mosterd", "Noten (schaalvruchten)",
    "Schaaldieren", "Selderij", "Sesamzaad", "Soja", "Vis", "Weekdieren",
]
VALUES = {
    "Product     nummer": 64224100, "Product naam": "BOF FR WITLOOF HAM KAAS 320G",
    "schaal": "UK-59", "kapvorm": "H22", "karton": "Shrink H22 X 6", "Allergeen": "! !",
    "Snelheid L1": 1500, "Snelheid L2": 1500, "Dinsdag": 1, "Woensdag": 1,
    "Donderdag": 1, "Vrijdag": 1, "opstart kan niet": 1, "Terlet (4)": 1,
    "kaasstrooi(1)": 1, "oude Kliklok (kooien)": 1, "Gluten": "+", "Lupine": "X",
    "Melk": "+", "Mosterd": "X", "Soja": "X", "Vis": "-",
}


def _write(path, names, sections, values):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "product database"
    for col, name in enumerate(names, start=1):
        if col in sections:
            ws.cell(row=1, column=col, value=sections[col])
        ws.cell(row=2, column=col, value=name)
        if name in values:
            ws.cell(row=3, column=col, value=values[name])
    wb.save(path)
    return path


def _with_kliklok_l2(names):
    """The client's October layout: 'Kliklok L2' before 'Hand nipak'."""
    out = list(names)
    out[out.index("Kliklok")] = "Kliklok L1"
    out.insert(out.index("Hand nipak"), "Kliklok L2")
    return out


def _shift_sections(sections, inserted_at):
    return {c + 1 if c >= inserted_at else c: s for c, s in sections.items()}


def test_an_inserted_column_changes_nothing(tmp_path):
    old = load_product_setup_from_excel(_write(tmp_path / "old.xlsx", NAMES, SECTIONS, VALUES))
    names = _with_kliklok_l2(NAMES)
    new = load_product_setup_from_excel(_write(
        tmp_path / "new.xlsx", names,
        _shift_sections(SECTIONS, names.index("Kliklok L2") + 1), VALUES,
    ))
    assert old["products"] == new["products"]
    allergenen = new["products"][0]["kruiscontaminatie"]["allergenen"]
    assert allergenen["soja"] == "kruiscontaminatie"
    assert allergenen["vis"] == "niet_aanwezig"
    assert new["products"][0]["product_informatie"]["oude_kliklok_beschikbaar"] is True


def test_a_missing_header_fails_loudly(tmp_path):
    names = [n for n in NAMES if n != "Vis"]
    path = _write(tmp_path / "broken.xlsx", names, SECTIONS, VALUES)
    with pytest.raises(ValueError, match="vis"):
        load_product_setup_from_excel(path)
