from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from dataclasses import replace

from .constraints.calendar_rules import PRODUCTIEDAGEN
from .constraints.day_rules import allowed_days_for
from .constraints.grouping_rules import identiek_groepen, parse_identiek_refs
from .constraints.line_rules import derive_line_info
from .models import Product



def _load_schaal_config() -> dict[str, dict[str, int]]:
    """Load schaal configuration (kop and bovenplaten mapping) from setup_times.json"""
    try:
        setup_file = Path(__file__).parent.parent / "data" / "setup_times.json"
        with open(setup_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get("setup_times", {}).get("schaal_configuratie", {})
    except Exception:
        return {}


_SCHAAL_CONFIG = _load_schaal_config()


def _calculate_fte_from_personeel(personeel: dict, allergie_niveau: int = 0) -> tuple[float, int]:
    """
    Convert Excel personeel complexity ratings to FTE & skill requirements.

    Personeel ratings (1-3):
      - 1 = simple/quick
      - 2 = medium
      - 3 = complex/slow

    Allergie niveau (0-6) also affects skill requirement.

    Returns: (fte_base, skill_required)
    """
    if not personeel:
        return 1.0, max(allergie_niveau, 1)

    # Average personeel complexity
    avg_complexity = (
        personeel.get("assemblage_l1", 1) +
        personeel.get("assemblage_l2", 1) +
        personeel.get("kartonnage", 1) +
        personeel.get("keuken", 1)
    ) / 4.0

    # Map complexity to FTE
    if avg_complexity <= 1.2:
        fte = 0.5
    elif avg_complexity <= 1.8:
        fte = 0.75
    elif avg_complexity <= 2.2:
        fte = 1.0
    elif avg_complexity <= 2.5:
        fte = 1.25
    elif avg_complexity <= 2.8:
        fte = 1.5
    else:
        fte = 2.0

    # Skill requirement: allergie niveau + complexity boost
    skill = max(allergie_niveau, 1)  # Start with allergie niveau
    if avg_complexity >= 2.5 and skill < 2:  # Complex products need at least skill 2
        skill = 2

    return fte, skill


def load_products(path: str | Path) -> tuple[dict[str, Product], dict[str, int]]:
    # products.json is GENERATED — create_plan.py rewrites it from
    # "Planning info.xlsx" (via product_setup_loader.write_product_setup_json)
    # at the start of every run. Never hand-edit it; make product corrections
    # in the Excel. See data/generated/.products_protected.
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    products: dict[str, Product] = {}
    for p in data["products"]:
        grouped = "product_informatie" in p
        product = _load_grouped_product(p) if grouped else _load_flat_product(p)
        if grouped:
            # FTE / skill are derived from the personeel complexity ratings.
            fte, skill = _calculate_fte_from_personeel(
                p.get("personeel", {}), product.allergie_niveau
            )
            product = replace(product, fte_base=fte, skill_required=skill)
        products[product.art_nr] = product

    # G1 — a group is a property of the whole catalogue, not of one row: a
    # product's partners may only name it from their side of the link.
    groups = identiek_groepen({
        art_nr: parse_identiek_refs(product.identiek_product)
        for art_nr, product in products.items()
    })
    for art_nr, group in groups.items():
        products[art_nr] = replace(products[art_nr], identiek_groep=group)

    toestellen_beschikbaarheid: dict[str, int] = data.get("toestellen_beschikbaarheid", {})
    return products, toestellen_beschikbaarheid


def load_product_warnings(path: str | Path) -> list[str]:
    """Read source-quality warnings emitted with the generated product data."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("warnings", []))


def _load_flat_product(p: dict[str, Any]) -> Product:

    schaal = p["schaal"]
    schaal_config = _SCHAAL_CONFIG.get(schaal, {})

    # Use provided days or apply average to empty days list
    toegelaten_dagen = p["toegelaten_dagen"] or list(PRODUCTIEDAGEN)

    return Product(
        art_nr=p["art_nr"],
        naam=p["naam"],
        schaal=schaal,
        kapvorm=p["kapvorm"],
        type_karton=p["type_karton"],
        stuks_per_karton=p["stuks_per_karton"],
        snelheid=p["snelheid"],
        prio_l1=p["prio_l1"],
        prio_l2=p["prio_l2"],
        alleen_l1=p["alleen_l1"],
        alleen_l2=p["alleen_l2"],
        l1_uitgesloten=p.get("l1_uitgesloten", False),
        l2_uitgesloten=p.get("l2_uitgesloten", False),
        toegelaten_dagen=toegelaten_dagen,
        mep_d1=p["mep_d1"],
        mep_dplus=p["mep_dplus"],
        opstart_kan=p["opstart_kan"],
        afsluiten_kan=p["afsluiten_kan"],
        toestellen=p["toestellen"],
        assemblage_toestellen=dict(p.get("toestellen_assemblage", {})),
        allergenen=p["allergenen"],
        allergie_niveau=p["allergie_niveau"],
        kliklok=p.get("kliklok", False),
        etui=p.get("etui", False),
        merk=p.get("merk", ""),
        opmerking=p.get("opmerking", ""),
        kop=schaal_config.get("kop", 0),
        bovenplaten=schaal_config.get("bovenplaten", 0),
        constraint_provenance=dict(p.get("constraint_provenance") or {
            key: "inferred" for key in (
                "line", "weekdays", "speed", "position", "allergens", "devices"
            )
        }),
    )


def _load_grouped_product(p: dict[str, Any]) -> Product:

    info = p["product_informatie"]
    planning = p["planning"]
    snelheid = p["snelheid"]
    prio_lijn = p["prio_lijn"]
    keuken = p["toestellen_keuken"]
    assemblage = p["toestellen_assemblage"]
    kruiscontaminatie = p["kruiscontaminatie"]
    personeel = p.get("personeel", {})

    art_nr = info["product_nummer"]
    naam = info["product_naam"]
    merk = _infer_merk(naam)
    schaal = info["schaal"]
    schaal_config = _SCHAAL_CONFIG.get(schaal, {})
    line_source = p.get("spiraal", {}) if prio_lijn.get("cfs") == "voorkeur" else prio_lijn
    line_info = derive_line_info(
        line_source.get("lijn1"),
        line_source.get("lijn2"),
        schaal,
        merk,
    )
    allergenen = {
        name: _allergen_symbol(value)
        for name, value in kruiscontaminatie["allergenen"].items()
    }

    return Product(
        art_nr=art_nr,
        naam=naam,
        schaal=schaal,
        kapvorm=info["kapvorm"],
        type_karton=info["karton"],
        stuks_per_karton=info["stuks_per_karton"],
        snelheid=_choose_speed(snelheid.get("lijn1"), snelheid.get("lijn2"), line_info),
        prio_l1=line_info["prio_l1"],
        prio_l2=line_info["prio_l2"],
        alleen_l1=line_info["alleen_l1"],
        alleen_l2=line_info["alleen_l2"],
        l1_uitgesloten=line_info["l1_uitgesloten"],
        l2_uitgesloten=line_info["l2_uitgesloten"],
        toegelaten_dagen=allowed_days_for(planning, naam, allergenen),
        mep_d1=planning["mep"]["d1_nodig"],
        mep_dplus=planning["mep"]["dplus_nodig"],
        opstart_kan=planning["opstart"]["kan"],
        afsluiten_kan=planning["afsluit"]["kan"],
        opstart_moet=planning["opstart"]["moet"],
        opstart_kan_niet=not planning["opstart"]["kan"],
        afsluit_moet=planning["afsluit"]["moet"],
        afsluit_kan_niet=not planning["afsluit"]["kan"],
        toestellen=_scheduler_toestellen(keuken, assemblage),
        assemblage_toestellen=dict(assemblage),
        allergenen=allergenen,
        allergie_niveau=kruiscontaminatie["allergie_niveau"],
        kliklok=info["oude_kliklok_beschikbaar"] or line_info["prio_l1"] or line_info["alleen_l1"],
        etui=info["kapvorm"].upper() != "NVT" or line_info["prio_l1"] or line_info["alleen_l1"],
        merk=merk,
        opmerking=info["opmerking"],
        identiek_product=info.get("identiek_product", ""),
        kop=schaal_config.get("kop", 0),
        bovenplaten=schaal_config.get("bovenplaten", 0),
        personeel_assemblage_l1=personeel.get("assemblage_l1", 1),
        personeel_assemblage_l2=personeel.get("assemblage_l2", 1),
        personeel_kartonnage=personeel.get("kartonnage", 1),
        personeel_keuken=personeel.get("keuken", 1),
        constraint_provenance=dict(p.get("constraint_provenance") or {
            "line": "explicit" if prio_lijn.get("lijn1") is not None or prio_lijn.get("lijn2") is not None else "inferred",
            "weekdays": "explicit" if any(planning["productiedagen"].values()) else "inferred",
            "speed": "explicit" if snelheid.get("lijn1") or snelheid.get("lijn2") else "inferred",
            "position": "explicit" if any((planning["opstart"]["moet"], not planning["opstart"]["kan"], planning["afsluit"]["moet"], not planning["afsluit"]["kan"])) else "inferred",
            "allergens": "explicit",
            "devices": "explicit",
        }),
    )


def _choose_speed(
    speed_l1: int | None,
    speed_l2: int | None,
    line_info: dict[str, bool],
) -> int:
    if (line_info["prio_l1"] or line_info["alleen_l1"]) and speed_l1:
        return speed_l1
    if (line_info["prio_l2"] or line_info["alleen_l2"]) and speed_l2:
        return speed_l2
    return speed_l1 or speed_l2 or 1000


def _scheduler_toestellen(
    keuken: dict[str, int],
    assemblage: dict[str, int],
) -> dict[str, bool]:
    return {
        "gernal": keuken["gernal_nodig"],
        "teriet": keuken["terlet_aantal"] > 0,
        "berief": keuken["berief_aantal"] > 0,
        "ks_menger": keuken["ks_menger_nodig"],
        "stephan_mixer_klein": keuken["stephan_mixer_klein_nodig"],
        "stephan_mixer_groot": keuken["stephan_mixer_groot_nodig"],
        "leonard": assemblage["leonard_nodig"],
        "weegschalen": assemblage["weegschalen_aantal"] > 0,
        "multifill": assemblage["multifill_nodig"],
        "kaastrooi": assemblage["kaasstrooier_nodig"],
        "turbo": assemblage["turbo_aantal"] > 0,
        "handtmann": assemblage["handtmann_aantal"] > 0,
        "multipond": assemblage["multipond_l1_nodig"],
        "isihda_l1": False,
        "isihda_l2": assemblage["isihda_l2_nodig"],
        "grote_cfs_pomp": assemblage["grote_slicer_nodig"] or assemblage["cfs_pomp_nodig"],
    }


def _infer_merk(naam: str) -> str:
    upper = naam.upper()
    for marker in ("DMB", "DMC", "VAT", "BOF", "IGLO", "LIDL", "APETITO", "PICARD", "DMH"):
        if marker in upper:
            return marker
    if "BLUE" in upper and "GREEN" in upper:
        return "BLUE GREEN"
    return "ONBEKEND"


def _allergen_symbol(value: str) -> str:
    if value == "niet_aanwezig":
        return "-"
    if value == "aanwezig":
        return "+"
    if value == "kruiscontaminatie":
        return "X"
    return "-"


def load_setup_times(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["setup_times"]
