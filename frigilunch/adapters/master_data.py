"""
master_data.py — Load product master data and run pre-flight checks.

Public API:
    read_products(path: Path) -> dict[str, Product]
    validate_master(products, orders) -> list[str]

Bridges to src.loader.load_products for the complex product-format parsing.
validate_master returns warning strings for unknown articles and inferred speeds.
"""
from __future__ import annotations

from pathlib import Path

from frigilunch.domain.models import Order, Product


_INFERRED_SPEED_SENTINEL = 1000  # _choose_speed fallback in src/loader.py


def _to_domain(p: object) -> Product:
    """Convert a src.models.Product to frigilunch.domain.models.Product."""
    return Product(
        art_nr=p.art_nr,
        naam=p.naam,
        schaal=p.schaal,
        kapvorm=p.kapvorm,
        snelheid=p.snelheid,
        prio_l1=p.prio_l1,
        prio_l2=p.prio_l2,
        alleen_l1=p.alleen_l1,
        alleen_l2=p.alleen_l2,
        l1_uitgesloten=p.l1_uitgesloten,
        l2_uitgesloten=p.l2_uitgesloten,
        toegelaten_dagen=list(p.toegelaten_dagen),
        mep_d1=p.mep_d1,
        mep_dplus=p.mep_dplus,
        allergie_niveau=p.allergie_niveau,
        allergenen=dict(p.allergenen),
        toestellen=dict(p.toestellen),
        opstart_kan=p.opstart_kan,
        afsluiten_kan=p.afsluiten_kan,
        opstart_moet=p.opstart_moet,
        opstart_kan_niet=p.opstart_kan_niet,
        afsluit_moet=p.afsluit_moet,
        afsluit_kan_niet=p.afsluit_kan_niet,
        fte_base=p.fte_base,
        personeel_assemblage_l1=p.personeel_assemblage_l1,
        personeel_assemblage_l2=p.personeel_assemblage_l2,
        identiek_groep=p.identiek_groep,
        mep_lead_days=p.mep_lead_days,
        maximum_per_dag=p.maximum_per_dag,
        restricted_days=p.restricted_days,
        opmerking=p.opmerking,
    )


def _ensure_src_on_path() -> None:
    import sys
    from pathlib import Path as _Path
    repo_root = _Path(__file__).parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def read_products(path: Path) -> dict[str, Product]:
    """Read products from products.json; returns frigilunch domain Products."""
    _ensure_src_on_path()
    from src.loader import load_products as _load
    src_products, _ = _load(path)
    return {art_nr: _to_domain(p) for art_nr, p in src_products.items()}


def load_src_products(path: Path) -> tuple[dict, dict]:
    """Return (src_products, toestellen) in the src-native format.

    Used by stages that call src-native functions (solver, scheduler, dashboard)
    which expect src.models.Product objects. Bridge lives here, not in pipeline.
    """
    _ensure_src_on_path()
    from src.loader import load_products as _load
    return _load(path)


def load_setup_times(path: Path) -> dict:
    """Read setup_times.json and return the setup_times sub-dict."""
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["setup_times"]


def _warn_unknown(orders: list[Order], products: dict[str, Product]) -> list[str]:
    """Return one warning per order whose art_nr is not in products."""
    unknown = {o.art_nr for o in orders if o.art_nr not in products}
    return [f"WARN: unknown product '{art_nr}' — order(s) will be rejected" for art_nr in sorted(unknown)]


def _warn_missing_speed(orders: list[Order], products: dict[str, Product]) -> list[str]:
    """Return one warning per active product with an inferred (sentinel) speed."""
    active = {o.art_nr for o in orders if o.art_nr in products}
    return [
        f"WARN: speed not configured for '{art_nr}' (snelheid={products[art_nr].snelheid}) "
        "— order(s) will be rejected"
        for art_nr in sorted(active)
        if products[art_nr].snelheid == _INFERRED_SPEED_SENTINEL
    ]


def validate_master(
    products: dict[str, Product],
    orders: list[Order],
) -> list[str]:
    """Return warning strings for data-quality issues that cause order rejections.

    Checks:
      - orders whose art_nr is not in the product master
      - active products whose speed was never configured (sentinel = 1000)
    """
    return _warn_unknown(orders, products) + _warn_missing_speed(orders, products)
