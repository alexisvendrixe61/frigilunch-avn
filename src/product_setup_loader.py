from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


# Logical column keys. The sheet is read by HEADER, never by position: the
# client inserts columns ("Kliklok L2" landed at column 52 in October 2026 and
# shifted every allergen one place to the right), and a fixed index then reads
# the neighbouring column without any error. See _COLUMN_SPEC / _resolve_columns.
DAY_COLUMNS: tuple[str, ...] = ("ma", "di", "wo", "do", "vr")

ALLERGEN_COLUMNS: tuple[str, ...] = (
    "aardnoten",
    "eieren",
    "gluten",
    "lupine",
    "melk",
    "mosterd",
    "noten",
    "schaaldieren",
    "selderij",
    "sesamzaad",
    "soja",
    "vis",
    "weekdieren",
)

TOESTEL_COLUMNS: tuple[str, ...] = (
    "gernal",
    "teriet",
    "berief",
    "ks_menger",
    "stephan_mixer_klein",
    "stephan_mixer_groot",
    "leonard",
    "weegschalen",
    "multifill",
    "kaastrooi",
    "turbo",
    "handtmann",
    "multipond",
    "isihda_l2",
    "grote",
    "cfs_pomp",
)

POSITIE_COLUMNS: tuple[str, ...] = (
    "opstart_moet", "opstart_kan_niet", "afsluit_moet", "afsluit_kan_niet",
)

# key -> (section prefix or None, normalised header name, match on prefix?)
#
# Names are matched after _normalise_header: lower case, whitespace collapsed,
# and a trailing "(...)" dropped — so "Gernal (2)" and "Gernal (1)" are both
# "gernal" and a changed machine count in the header does not break the read.
# The section (row 1, forward-filled) is only needed where a name repeats:
# "L1"/"L2" exist under both "Prio Lijn" and "Spiraal", "Kartonnage"/"Keuken"
# are both a staff column and (part of) a section title.
_COLUMN_SPEC: dict[str, tuple[str | None, str, bool]] = {
    "art_nr": (None, "product nummer", False),
    "naam": (None, "product naam", False),
    "schaal": (None, "schaal", False),
    "kapvorm": (None, "kapvorm", False),
    "karton": (None, "karton", False),
    "flowpack": (None, "flowpack", False),
    "allergeen": (None, "allergeen", False),
    "assemblage_l1": (None, "assemblage l1", False),
    "assemblage_l2": (None, "assemblage l2", False),
    "kartonnage": ("personeel", "kartonnage", False),
    "keuken": ("personeel", "keuken", False),
    "snelheid_l1": (None, "snelheid l1", False),
    "snelheid_l2": (None, "snelheid l2", False),
    "opmerking": (None, "opmerking", False),
    "identiek": (None, "identiek product", False),
    "prio_l1": ("prio lijn", "l1", False),
    "prio_l2": ("prio lijn", "l2", False),
    "prio_cfs": ("prio lijn", "cfs", False),
    "spiraal_l1": ("spiraal", "l1", False),
    "spiraal_l2": ("spiraal", "l2", False),
    "ma": (None, "maandag", False),
    "di": (None, "dinsdag", False),
    "wo": (None, "woensdag", False),
    "do": (None, "donderdag", False),
    "vr": (None, "vrijdag", False),
    "mep_d1": (None, "mep d-1", False),
    "mep_dplus": (None, "mep > d-1", False),
    "opstart_moet": (None, "opstart moet", False),
    "opstart_kan_niet": (None, "opstart kan niet", False),
    "afsluit_moet": (None, "afsluit moet", False),
    "afsluit_kan_niet": (None, "afsluit kan niet", False),
    "vis_verzagen": (None, "vis verzagen", False),
    "gernal": (None, "gernal", False),
    "teriet": (None, "terlet", False),
    "berief": (None, "berief", False),
    "ks_menger": (None, "ks menger", False),
    "stephan_mixer_klein": (None, "stephan mixer kl", True),
    "stephan_mixer_groot": (None, "stephan mixer gr", True),
    "leonard": (None, "leonard", False),
    "weegschalen": (None, "weegschalen", False),
    "multifill": (None, "multifill", False),
    "kaastrooi": (None, "kaasstrooi", False),
    "turbo": (None, "turbo", False),
    "handtmann": (None, "handtmann", False),
    "multipond": (None, "multipond", False),
    "isihda_l2": (None, "isihda", False),
    "grote": (None, "grote", False),
    "cfs_pomp": (None, "cfs pomp", False),
    "oude_kliklok": (None, "oude kliklok", False),
    **{name: ("kruiscontaminatie", name, False) for name in ALLERGEN_COLUMNS},
}


def _normalise_header(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return re.sub(r"\s*\(.*\)\s*$", "", text)


def _resolve_columns(section_row: tuple[Any, ...], name_row: tuple[Any, ...]) -> dict[str, int]:
    """
    Logical key -> 1-based column index, from the sheet's two header rows.

    Fails loudly when a key matches no column or more than one: a plan built on
    a guessed column is wrong in a way nothing downstream can notice.
    """
    headers: list[tuple[int, str, str]] = []
    section = ""
    for idx in range(max(len(section_row), len(name_row))):
        sec = section_row[idx] if idx < len(section_row) else None
        if sec not in (None, ""):
            section = _normalise_header(sec)
        name = _normalise_header(name_row[idx] if idx < len(name_row) else None)
        if name:
            headers.append((idx + 1, section, name))

    cols: dict[str, int] = {}
    for key, (sec_prefix, wanted, prefix) in _COLUMN_SPEC.items():
        hits = [
            col for col, sec, name in headers
            if (name.startswith(wanted) if prefix else name == wanted)
            and (sec_prefix is None or sec.startswith(sec_prefix))
        ]
        if len(hits) != 1:
            seen = ", ".join(f"{col}:{sec}/{name}" for col, sec, name in headers)
            problem = "not found" if not hits else f"ambiguous (columns {hits})"
            raise ValueError(
                f"product database: column for '{key}' ({wanted!r}) {problem}. "
                f"Headers seen: {seen}"
            )
        cols[key] = hits[0]
    return cols


class _Row:
    """One data row, addressed by logical key instead of by column number."""

    def __init__(self, values: tuple[Any, ...], cols: dict[str, int]):
        self.values = values
        self.cols = cols

    def get(self, key: str) -> Any:
        idx = self.cols[key] - 1
        return self.values[idx] if idx < len(self.values) else None


TOESTELLEN_BESCHIKBAARHEID: dict[str, int] = {
    "gernal": 2,
    "teriet": 4,
    "berief": 3,
    "ks_menger": 1,
    "stephan_mixer_klein": 1,
    "stephan_mixer_groot": 1,
    "leonard": 2,
    "weegschalen": 16,
    "multifill": 1,
    "kaastrooi": 1,
    "turbo": 11,
    "handtmann": 5,
    "multipond": 1,
    "isihda_l1": 1,
    "isihda_l2": 1,
    "grote_cfs_pomp": 1,
}


def load_product_setup_from_excel(path: str | Path) -> dict[str, Any]:
    """Load the workbook product database as grouped source-of-truth JSON."""
    products, warnings, workbook_path = _load_products_from_workbook(path)

    return {
        "metadata": {
            "source": str(workbook_path),
            "sheet": "product database",
            "product_count": len(products),
            "warning_count": len(warnings),
            "notes": (
                "Generated from Planning info.xlsx. Product fields are grouped by the "
                "Excel section headers with semantic values instead of Excel codes."
            ),
        },
        "products": products,
        "toestellen_beschikbaarheid": TOESTELLEN_BESCHIKBAARHEID,
        "warnings": warnings,
    }


def _load_products_from_workbook(
    path: str | Path,
) -> tuple[list[dict[str, Any]], list[str], Path]:
    try:
        import openpyxl
    except ImportError as e:
        raise ImportError("openpyxl is required: pip install openpyxl") from e

    workbook_path = Path(path)
    wb = openpyxl.load_workbook(workbook_path, data_only=True, read_only=True)
    if "product database" not in wb.sheetnames:
        raise ValueError("Workbook is missing required sheet: product database")

    ws = wb["product database"]
    header_rows = list(ws.iter_rows(min_row=1, max_row=2, values_only=True))
    cols = _resolve_columns(header_rows[0], header_rows[1])
    products: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for row_idx, values in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        row = _Row(values, cols)
        art_nr = _format_art_nr(_cell(row, "art_nr"))
        naam = _text(_cell(row, "naam"))
        if not art_nr or not naam:
            continue
        if art_nr in seen:
            warnings.append(f"Duplicate product {art_nr} at row {row_idx}; row skipped")
            continue
        seen.add(art_nr)

        product, product_warnings = _product_from_row(row, row_idx)
        products.append(product)
        warnings.extend(product_warnings)

    products.sort(key=lambda p: p["product_informatie"]["product_nummer"])
    return products, warnings, workbook_path


def write_product_setup_json(input_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    payload = load_product_setup_from_excel(input_path)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return payload


def _product_from_row(row: _Row, row_idx: int) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []

    art_nr = _format_art_nr(_cell(row, "art_nr"))
    naam = _text(_cell(row, "naam"))
    schaal = _normalize_schaal(_text(_cell(row, "schaal")))
    kapvorm = _text(_cell(row, "kapvorm")) or "NVT"
    type_karton = _text(_cell(row, "karton"))
    merk = _infer_merk(naam)

    l1_setting = _priority_value(_cell(row, "prio_l1"))
    l2_setting = _priority_value(_cell(row, "prio_l2"))
    cfs_setting = _priority_value(_cell(row, "prio_cfs"))
    if l1_setting is None and l2_setting is None:
        warnings.append(
            f"Row {row_idx} product {art_nr}: missing L1/L2 setup; loader.py will "
            f"infer it from scale/brand"
        )

    speed_l1 = _int_or_none(_cell(row, "snelheid_l1"))
    speed_l2 = _int_or_none(_cell(row, "snelheid_l2"))
    if not speed_l1 and not speed_l2:
        warnings.append(f"Row {row_idx} product {art_nr}: no speed on either line")

    allergenen = {
        name: _allergen_value(_cell(row, name))
        for name in ALLERGEN_COLUMNS
    }
    allergie_niveau = _allergy_level_from_label(_text(_cell(row, "allergeen")))
    if allergie_niveau is None:
        allergie_niveau = _allergy_level_from_x_count(allergenen)

    toestellen_raw = {
        name: _machine_count(_cell(row, name))
        for name in TOESTEL_COLUMNS
    }
    opstart_moet = _is_true(_cell(row, "opstart_moet"))
    opstart_kan_niet = _is_true(_cell(row, "opstart_kan_niet"))
    afsluit_moet = _is_true(_cell(row, "afsluit_moet"))
    afsluit_kan_niet = _is_true(_cell(row, "afsluit_kan_niet"))
    product = _grouped_setup_from_row(
        row=row,
        row_idx=row_idx,
        art_nr=art_nr,
        naam=naam,
        schaal=schaal,
        kapvorm=kapvorm,
        type_karton=type_karton,
        allergenen=allergenen,
        allergie_niveau=allergie_niveau,
        l1_setting=l1_setting,
        l2_setting=l2_setting,
        cfs_setting=cfs_setting,
        speed_l1=speed_l1,
        speed_l2=speed_l2,
        toestellen_raw=toestellen_raw,
        opstart_moet=opstart_moet,
        opstart_kan_niet=opstart_kan_niet,
        afsluit_moet=afsluit_moet,
        afsluit_kan_niet=afsluit_kan_niet,
    )

    days_explicit = any(
        _text(_cell(row, day)) not in {"", "-"} for day in DAY_COLUMNS
    )
    position_explicit = any(
        _text(_cell(row, key)) not in {"", "-"} for key in POSITIE_COLUMNS
    )
    product["constraint_provenance"] = {
        "line": "explicit" if l1_setting is not None or l2_setting is not None else "inferred",
        "weekdays": "explicit" if days_explicit else "inferred",
        "speed": "explicit" if speed_l1 or speed_l2 else "inferred",
        "position": "explicit" if position_explicit else "inferred",
        "allergens": "explicit",
        "devices": "explicit",
    }

    if not _allowed_days_from_row(row, naam):
        if _requires_restricted_days(naam, allergenen):
            warnings.append(
                f"Row {row_idx} product {art_nr}: missing allowed days; inferred di/wo/vr"
            )
        else:
            warnings.append(
                f"Row {row_idx} product {art_nr}: missing allowed days; defaulted to weekdays"
            )

    return product, warnings


def _grouped_setup_from_row(
    *,
    row: _Row,
    row_idx: int,
    art_nr: str,
    naam: str,
    schaal: str,
    kapvorm: str,
    type_karton: str,
    allergenen: dict[str, str],
    allergie_niveau: int,
    l1_setting: int | None,
    l2_setting: int | None,
    cfs_setting: int | None,
    speed_l1: int | None,
    speed_l2: int | None,
    toestellen_raw: dict[str, int],
    opstart_moet: bool,
    opstart_kan_niet: bool,
    afsluit_moet: bool,
    afsluit_kan_niet: bool,
) -> dict[str, Any]:
    return {
        "source": {
            "sheet": "product database",
            "row": row_idx,
        },
        "product_informatie": {
            "product_nummer": art_nr,
            "product_naam": naam,
            "schaal": schaal,
            "kapvorm": kapvorm,
            "karton": type_karton,
            "stuks_per_karton": _stuks_per_karton(type_karton),
            "flowpack_nodig": _is_true(_cell(row, "flowpack")),
            "allergenen_classificatie": _text(_cell(row, "allergeen")),
            "opmerking": _clean_note(_cell(row, "opmerking")),
            "identiek_product": _text(_cell(row, "identiek")),
            "oude_kliklok_beschikbaar": _is_true(_cell(row, "oude_kliklok")),
        },
        "personeel": {
            "assemblage_l1": _int_or_none(_cell(row, "assemblage_l1")),
            "assemblage_l2": _int_or_none(_cell(row, "assemblage_l2")),
            "kartonnage": _int_or_none(_cell(row, "kartonnage")),
            "keuken": _int_or_none(_cell(row, "keuken")),
        },
        "snelheid": {
            "lijn1": speed_l1,
            "lijn2": speed_l2,
        },
        "prio_lijn": {
            "lijn1": _line_permission_value(l1_setting),
            "lijn2": _line_permission_value(l2_setting),
            "cfs": _line_permission_value(cfs_setting),
        },
        "spiraal": {
            "lijn1": _availability_value(_priority_value(_cell(row, "spiraal_l1"))),
            "lijn2": _availability_value(_priority_value(_cell(row, "spiraal_l2"))),
        },
        "planning": {
            "productiedagen": {
                "maandag_toegelaten": _is_true(_cell(row, "ma")),
                "dinsdag_toegelaten": _is_true(_cell(row, "di")),
                "woensdag_toegelaten": _is_true(_cell(row, "wo")),
                "donderdag_toegelaten": _is_true(_cell(row, "do")),
                "vrijdag_toegelaten": _is_true(_cell(row, "vr")),
            },
            "mep": {
                "d1_nodig": _is_true(_cell(row, "mep_d1")),
                "dplus_nodig": _is_true(_cell(row, "mep_dplus")),
            },
            "opstart": {
                "moet": opstart_moet,
                "kan": not opstart_kan_niet,
            },
            "afsluit": {
                "moet": afsluit_moet,
                "kan": not afsluit_kan_niet,
            },
            "vis_verzagen_nodig": _is_true(_cell(row, "vis_verzagen")),
        },
        "toestellen_keuken": {
            "gernal_nodig": toestellen_raw["gernal"] > 0,
            "terlet_aantal": toestellen_raw["teriet"],
            "berief_aantal": toestellen_raw["berief"],
            "ks_menger_nodig": toestellen_raw["ks_menger"] > 0,
            "stephan_mixer_klein_nodig": toestellen_raw["stephan_mixer_klein"] > 0,
            "stephan_mixer_groot_nodig": toestellen_raw["stephan_mixer_groot"] > 0,
        },
        "toestellen_assemblage": {
            "leonard_nodig": toestellen_raw["leonard"] > 0,
            "weegschalen_aantal": toestellen_raw["weegschalen"],
            "multifill_nodig": toestellen_raw["multifill"] > 0,
            "kaasstrooier_nodig": toestellen_raw["kaastrooi"] > 0,
            "turbo_aantal": toestellen_raw["turbo"],
            "handtmann_aantal": toestellen_raw["handtmann"],
            "multipond_l1_nodig": toestellen_raw["multipond"] > 0,
            "isihda_l2_nodig": toestellen_raw["isihda_l2"] > 0,
            "grote_slicer_nodig": toestellen_raw["grote"] > 0,
            "cfs_pomp_nodig": toestellen_raw["cfs_pomp"] > 0,
        },
        "kruiscontaminatie": {
            "allergenen": {
                name: _semantic_allergen_value(value)
                for name, value in allergenen.items()
            },
            "allergie_niveau": allergie_niveau,
        },
    }


def _cell(row: _Row, key: str) -> Any:
    return row.get(key)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _clean_note(value: Any) -> str:
    text = _text(value)
    return "" if text == "-" else text


def _format_art_nr(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _int_or_none(value: Any) -> int | None:
    if value is None or _text(value) in {"", "-"}:
        return None
    try:
        return int(float(str(value).replace(",", ".")))
    except ValueError:
        return None


def _priority_value(value: Any) -> int | None:
    parsed = _int_or_none(value)
    return parsed if parsed in {0, 1, 2} else None


def _is_true(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = _text(value).lower()
    return text not in {"", "-", "0", "false", "nee", "no", "n"}


def _machine_count(value: Any) -> int:
    parsed = _int_or_none(value)
    if parsed is not None:
        return max(0, parsed)
    return 1 if _is_true(value) else 0


def _scheduler_toestellen(toestellen_raw: dict[str, int]) -> dict[str, bool]:
    return {
        "gernal": toestellen_raw["gernal"] > 0,
        "teriet": toestellen_raw["teriet"] > 0,
        "berief": toestellen_raw["berief"] > 0,
        "ks_menger": toestellen_raw["ks_menger"] > 0,
        "stephan_mixer_klein": toestellen_raw["stephan_mixer_klein"] > 0,
        "stephan_mixer_groot": toestellen_raw["stephan_mixer_groot"] > 0,
        "leonard": toestellen_raw["leonard"] > 0,
        "weegschalen": toestellen_raw["weegschalen"] > 0,
        "multifill": toestellen_raw["multifill"] > 0,
        "kaastrooi": toestellen_raw["kaastrooi"] > 0,
        "turbo": toestellen_raw["turbo"] > 0,
        "handtmann": toestellen_raw["handtmann"] > 0,
        "multipond": toestellen_raw["multipond"] > 0,
        "isihda_l1": False,
        "isihda_l2": toestellen_raw["isihda_l2"] > 0,
        "grote_cfs_pomp": (
            toestellen_raw["grote"] > 0 or toestellen_raw["cfs_pomp"] > 0
        ),
    }


def _allergen_value(value: Any) -> str:
    text = _text(value).upper()
    if text in {"X", "+", "-"}:
        return text
    if text in {"", "0"}:
        return "-"
    return text


def _semantic_allergen_value(value: str) -> str:
    if value == "-":
        return "niet_aanwezig"
    if value == "+":
        return "aanwezig"
    if value == "X":
        return "kruiscontaminatie"
    return "onbekend"


def _line_permission_value(value: int | None) -> str:
    if value == 1:
        return "voorkeur"
    if value == 2:
        return "kan"
    if value == 0:
        return "kan_niet"
    return "onbekend"


def _availability_value(value: int | None) -> str:
    if value == 1:
        return "kan"
    if value == 0:
        return "kan_niet"
    return "onbekend"


def _allergy_level_from_label(label: str) -> int | None:
    """
    Column 7, the assembly allergen classification. Its legend ("legende
    database" row 10) reads:

        "-  laagst  naar  ! ! ! ! !  Hoogst   /   X = sate productie"

    So the scale is 7 wide, not 5: "-" is the clean floor, one to five "!"
    climb from there, and "X" (sate, i.e. aardnoten) sits above all of them
    as the highest risk there is.

    Beware the two unrelated X's in this sheet: this one means "sate", while
    an X in the per-allergen columns 55-67 means "aanwezig door
    kruiscontaminatie". Reading column 7's X as the latter is what used to
    push the sate products back down into the middle of the scale.

    Returns None only when the cell says nothing at all, which lets the
    caller fall back to counting cross-contamination markers.
    """
    text = label.strip().upper()
    if not text or text == "0":
        return None
    if text == "X":
        return 6
    if text == "-":
        return 0
    count = text.count("!")
    if count:
        return min(count, 5)
    return None


def _allergy_level_from_x_count(allergenen: dict[str, str]) -> int:
    """
    Fallback for a blank column 7: infer a level from how many allergens are
    present through cross-contamination.

    Caps at 5, never 6 — level 6 means "sate" specifically, and no count of
    cross-contamination markers can tell you that. Currently dead on real
    data (all 604 rows carry a label); kept for rows that lose theirs.
    """
    x_count = sum(1 for v in allergenen.values() if v == "X")
    if x_count == 0:
        return 0
    if x_count == 1:
        return 1
    if x_count <= 3:
        return 2
    if x_count <= 6:
        return 3
    return 5


def _normalize_schaal(value: str) -> str:
    text = value.strip()
    upper = text.upper().replace(" ", "")
    if upper == "UK59":
        return "UK-59"
    if upper == "KRAFTROND":
        return "KRAFT ROND"
    return text


def _infer_merk(naam: str) -> str:
    upper = naam.upper()
    for marker in ("DMB", "DMC", "VAT", "BOF", "IGLO", "LIDL", "APETITO", "PICARD", "DMH"):
        if marker in upper:
            return marker
    if "BLUE" in upper and "GREEN" in upper:
        return "BLUE GREEN"
    return "ONBEKEND"


def _stuks_per_karton(type_karton: str) -> int:
    if not type_karton:
        return 1
    matches = re.findall(r"X\s*(\d+)\b", type_karton.upper())
    if matches:
        return int(matches[-1])
    return 1


def _allowed_days_from_row(row: _Row, naam: str) -> list[str]:
    days = [day for day in DAY_COLUMNS if _is_true(_cell(row, day))]
    if days:
        return days
    return []


def _requires_restricted_days(naam: str, allergenen: dict[str, str]) -> bool:
    upper_name = naam.upper()
    if "KIPPEBOUT" in upper_name:
        return True
    return allergenen.get("vis", "-") in {"X", "+"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate product setup JSON from Planning info.xlsx"
    )
    parser.add_argument("input", help="Path to Planning info.xlsx")
    parser.add_argument("output", help="Path to write generated product setup JSON")
    args = parser.parse_args()

    payload = write_product_setup_json(args.input, args.output)
    print(
        f"Wrote {payload['metadata']['product_count']} products to {args.output} "
        f"({payload['metadata']['warning_count']} warnings)"
    )


if __name__ == "__main__":
    main()
