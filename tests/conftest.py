"""
Shared fixtures. Everything here is synthetic — no Excel, no solver, no clock.

The point is that these tests describe the RUNNING pipeline (which had no
tests at all; the only suite in the repo covered `salvage/constraint_engine.py`,
which nothing imports).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models import Order, Product  # noqa: E402

ALL_DAYS = ["ma", "di", "wo", "do", "vr"]
NO_ALLERGENS = {
    a: "-" for a in (
        "gluten", "melk", "soja", "eieren", "aardnoten", "lupine", "mosterd",
        "noten", "schaaldieren", "selderij", "sesamzaad", "vis", "weekdieren",
    )
}
NO_DEVICES = {
    d: False for d in (
        "gernal", "teriet", "berief", "ks_menger", "stephan_mixer_klein",
        "stephan_mixer_groot", "leonard", "weegschalen", "multifill",
        "kaastrooi", "turbo", "handtmann", "multipond", "isihda_l1",
        "isihda_l2", "grote_cfs_pomp",
    )
}


def make_product(
    art_nr: str,
    *,
    naam: str | None = None,
    schaal: str = "UK-59",
    kapvorm: str = "I-L-2T",
    snelheid: int = 1000,
    allergie_niveau: int = 0,
    allergenen: dict[str, str] | None = None,
    dagen: list[str] | None = None,
    alleen_l1: bool = False,
    alleen_l2: bool = False,
    l1_uitgesloten: bool = False,
    l2_uitgesloten: bool = False,
    devices: list[str] | None = None,
    personeel_l1: int = 1,
    personeel_l2: int = 1,
    mep_d1: bool = False,
    mep_dplus: bool = False,
    opstart_moet: bool = False,
    opstart_kan_niet: bool = False,
    afsluit_moet: bool = False,
    afsluit_kan_niet: bool = False,
    identiek_groep: str | None = None,
) -> Product:
    toestellen = dict(NO_DEVICES)
    for d in devices or []:
        toestellen[d] = True
    return Product(
        art_nr=art_nr,
        naam=naam or f"PRODUCT {art_nr}",
        schaal=schaal,
        kapvorm=kapvorm,
        type_karton="",
        stuks_per_karton=1,
        snelheid=snelheid,
        prio_l1=not alleen_l2,
        prio_l2=not alleen_l1,
        alleen_l1=alleen_l1,
        alleen_l2=alleen_l2,
        l1_uitgesloten=l1_uitgesloten,
        l2_uitgesloten=l2_uitgesloten,
        toegelaten_dagen=list(dagen if dagen is not None else ALL_DAYS),
        mep_d1=mep_d1,
        mep_dplus=mep_dplus,
        opstart_kan=True,
        afsluiten_kan=True,
        toestellen=toestellen,
        allergenen=dict(allergenen or NO_ALLERGENS),
        allergie_niveau=allergie_niveau,
        personeel_assemblage_l1=personeel_l1,
        personeel_assemblage_l2=personeel_l2,
        opstart_moet=opstart_moet,
        opstart_kan_niet=opstart_kan_niet,
        afsluit_moet=afsluit_moet,
        afsluit_kan_niet=afsluit_kan_niet,
        identiek_groep=identiek_groep,
    )


def make_order(
    art_nr: str,
    aantal: int,
    productie: date,
    *,
    lever: date | None = None,
    lijn: int = 0,
    order_nr: str | None = None,
    klant: str = "TESTKLANT",
) -> Order:
    return Order(
        order_nr=order_nr or productie.isoformat() + " 00:00:00",
        art_nr=art_nr,
        klant=klant,
        aantal=aantal,
        lever_datum=lever or productie,
        productie_datum=productie,
        lijn=lijn,
    )


@pytest.fixture
def setup_matrix() -> dict:
    """The real changeover matrix, loaded from data/setup_times.json."""
    from src.loader import load_setup_times

    return load_setup_times(ROOT / "data" / "setup_times.json")


@pytest.fixture
def monday() -> date:
    """A plain Monday well inside the dataset's range."""
    return date(2026, 7, 27)


@pytest.fixture
def friday() -> date:
    """The Friday of the same week — shorter shift, different capacity."""
    return date(2026, 7, 31)
