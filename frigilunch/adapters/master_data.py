"""
master_data.py — Load product master data and run pre-flight checks.

Public API:
    read_products(path) -> dict[str, Product]       native, no src bridge
    load_src_products(path) -> (dict, dict)         src bridge (for solver/scheduler)
    load_setup_times(path) -> dict
    validate_master(products, orders) -> list[str]
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from frigilunch.domain.constraints.day_rules import allowed_days_for
from frigilunch.domain.constraints.grouping_rules import identiek_groepen, parse_identiek_refs
from frigilunch.domain.constraints.line_rules import derive_line_info
from frigilunch.domain.models import Order, Product

_INFERRED_SPEED_SENTINEL = 1000


# ── schaal config (from setup_times.json) ────────────────────────────────────

def _load_schaal_config() -> dict[str, dict[str, int]]:
    try:
        setup_file = Path(__file__).parent.parent.parent / "data" / "setup_times.json"
        with open(setup_file, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("setup_times", {}).get("schaal_configuratie", {})
    except Exception:
        return {}


_SCHAAL_CONFIG = _load_schaal_config()


# ── helpers ───────────────────────────────────────────────────────────────────

def _calculate_fte_from_personeel(
    personeel: dict, allergie_niveau: int = 0
) -> tuple[float, int]:
    if not personeel:
        return 1.0, max(allergie_niveau, 1)
    avg = (
        personeel.get("assemblage_l1", 1) + personeel.get("assemblage_l2", 1)
        + personeel.get("kartonnage", 1) + personeel.get("keuken", 1)
    ) / 4.0
    if avg <= 1.2:
        fte = 0.5
    elif avg <= 1.8:
        fte = 0.75
    elif avg <= 2.2:
        fte = 1.0
    elif avg <= 2.5:
        fte = 1.25
    elif avg <= 2.8:
        fte = 1.5
    else:
        fte = 2.0
    skill = max(allergie_niveau, 1)
    if avg >= 2.5 and skill < 2:
        skill = 2
    return fte, skill


def _choose_speed(
    speed_l1: int | None, speed_l2: int | None, line_info: dict[str, bool]
) -> int:
    if (line_info["prio_l1"] or line_info["alleen_l1"]) and speed_l1:
        return speed_l1
    if (line_info["prio_l2"] or line_info["alleen_l2"]) and speed_l2:
        return speed_l2
    return speed_l1 or speed_l2 or _INFERRED_SPEED_SENTINEL


def _scheduler_toestellen(keuken: dict, assemblage: dict) -> dict[str, bool]:
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


def _load_flat_product(p: dict[str, Any]) -> Product:
    from frigilunch.domain.constraints.calendar_rules import PRODUCTIEDAGEN
    schaal = p["schaal"]
    toegelaten_dagen = p["toegelaten_dagen"] or list(PRODUCTIEDAGEN)
    return Product(
        art_nr=p["art_nr"],
        naam=p["naam"],
        schaal=schaal,
        kapvorm=p["kapvorm"],
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
        allergenen=p["allergenen"],
        allergie_niveau=p["allergie_niveau"],
        stuks_per_karton=p.get("stuks_per_karton", 0),
        type_karton=p.get("type_karton", p.get("karton", "")),
        assemblage_toestellen=p.get("toestellen_assemblage", {}),
        opmerking=p.get("opmerking", ""),
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
    line_source = p.get("spiraal", {}) if prio_lijn.get("cfs") == "voorkeur" else prio_lijn
    line_info = derive_line_info(
        line_source.get("lijn1"), line_source.get("lijn2"), schaal, merk,
    )
    allergenen = {
        name: _allergen_symbol(value)
        for name, value in kruiscontaminatie["allergenen"].items()
    }
    fte, _ = _calculate_fte_from_personeel(personeel, kruiscontaminatie["allergie_niveau"])

    return Product(
        art_nr=art_nr,
        naam=naam,
        schaal=schaal,
        kapvorm=info["kapvorm"],
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
        allergenen=allergenen,
        allergie_niveau=kruiscontaminatie["allergie_niveau"],
        fte_base=fte,
        personeel_assemblage_l1=personeel.get("assemblage_l1", 1),
        personeel_assemblage_l2=personeel.get("assemblage_l2", 1),
        stuks_per_karton=info.get("stuks_per_karton", 0),
        type_karton=info.get("karton", ""),
        assemblage_toestellen=dict(assemblage),
        opmerking=info["opmerking"],
        constraint_provenance=dict(p.get("constraint_provenance", {})),
    )


def _load_products_from_json(path: Path) -> tuple[dict[str, Product], dict[str, int]]:
    """Native loader — returns frigilunch domain Products, no src dependency."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    products: dict[str, Product] = {}
    for p in data["products"]:
        grouped = "product_informatie" in p
        product = _load_grouped_product(p) if grouped else _load_flat_product(p)
        products[product.art_nr] = product

    groups = identiek_groepen({
        art_nr: parse_identiek_refs(product.identiek_groep or "")
        for art_nr, product in products.items()
    })
    for art_nr, group in groups.items():
        products[art_nr] = replace(products[art_nr], identiek_groep=group)

    toestellen_beschikbaarheid: dict[str, int] = data.get("toestellen_beschikbaarheid", {})
    return products, toestellen_beschikbaarheid


# ── public API ────────────────────────────────────────────────────────────────

def read_products(path: Path) -> dict[str, Product]:
    """Read products from products.json; returns frigilunch domain Products."""
    products, _ = _load_products_from_json(path)
    return products


def load_setup_times(path: Path) -> dict:
    """Read setup_times.json and return the setup_times sub-dict."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["setup_times"]


def load_product_warnings(path: Path) -> list[str]:
    """Read source-quality warnings emitted with the generated product data."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("warnings", []))


# ── validation ────────────────────────────────────────────────────────────────

def _warn_unknown(orders: list[Order], products: dict[str, Product]) -> list[str]:
    unknown = {o.art_nr for o in orders if o.art_nr not in products}
    return [
        f"WARN: unknown product '{art_nr}' — order(s) will be rejected"
        for art_nr in sorted(unknown)
    ]


def _warn_missing_speed(orders: list[Order], products: dict[str, Product]) -> list[str]:
    active = {o.art_nr for o in orders if o.art_nr in products}
    return [
        f"WARN: speed not configured for '{art_nr}' (snelheid={products[art_nr].snelheid}) "
        "— order(s) will be rejected"
        for art_nr in sorted(active)
        if products[art_nr].snelheid == _INFERRED_SPEED_SENTINEL
    ]


def validate_master(products: dict[str, Product], orders: list[Order]) -> list[str]:
    """Return warning strings for data-quality issues that cause order rejections."""
    return _warn_unknown(orders, products) + _warn_missing_speed(orders, products)
