"""
tests/integration/test_benders_smoke.py

First vertical slice: run_benders with a stub CP solver and the real oracle
on a 3-order synthetic fixture.  Asserts:
  - all 3 orders are placed (no rejected orders)
  - convergence in ≤3 iterations
  - no solver OR-Tools dependency required
"""
from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path

import pytest

from frigilunch.domain.benders import run_benders, Solvers
from frigilunch.domain.models import Order, Product


# ── fixture loading ───────────────────────────────────────────────────────────

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tiny_plan.json"


def _load_fixture() -> tuple[list[Order], dict[str, Product]]:
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

    orders = [
        Order(
            order_nr=o["order_nr"],
            art_nr=o["art_nr"],
            klant=o["klant"],
            aantal=o["aantal"],
            lever_datum=date.fromisoformat(o["lever_datum"]),
            productie_datum=date.fromisoformat(o["productie_datum"]),
            lijn=o.get("lijn", 0),
        )
        for o in data["orders"]
    ]

    products = {
        art_nr: Product(
            art_nr=p["art_nr"],
            naam=p["naam"],
            schaal=p["schaal"],
            kapvorm=p["kapvorm"],
            snelheid=p["snelheid"],
        )
        for art_nr, p in data["products"].items()
    }

    return orders, products


# ── stub CP solver ────────────────────────────────────────────────────────────

def _stub_cp_solver(
    orders: list[Order],
    frozen_dates: list[date],
    cuts: list[dict],
    config: dict,
) -> dict:
    """Deterministic stub: assign each order to its already-set productie_datum and lijn."""
    assignments = {
        o.order_nr: {"date": o.productie_datum, "lijn": o.lijn}
        for o in orders
    }
    return {"status": "feasible", "assignments": assignments}


# ── config ────────────────────────────────────────────────────────────────────

def _make_config() -> dict:
    return {
        "setup_matrix": {
            "ombouw_standaard": {"zelfde_formaat": 20, "ander_formaat": 30},
            "kapvorm_wissel_extra": {"andere_kapvorm": 15},
            "reiniging": {"allergie_daling": 30},
            "zelfde_machine_wissel": {},
            "daesang_uitzondering": {"art_nr_combinaties": []},
        },
        "line_start": {1: time(6, 0), 2: time(6, 30)},
        "shift_end": time(22, 30),
        "frozen_dates": [],
    }


# ── tests ─────────────────────────────────────────────────────────────────────

def test_benders_smoke_all_orders_placed():
    """All 3 fixture orders must be placed with no rejections."""
    from frigilunch.domain.oracle import schedule_day

    orders, products = _load_fixture()
    config = {**_make_config(), "max_iterations": 3}

    outcome = run_benders(
        orders=orders,
        products=products,
        solvers=Solvers(cp=_stub_cp_solver, oracle=schedule_day),
        config=config,
    )

    assert outcome.status == "complete", f"Expected complete, got {outcome.status}"
    assert len(outcome.rejected) == 0, (
        f"Expected 0 rejections, got {len(outcome.rejected)}: "
        f"{[o.order_nr for o in outcome.rejected]}"
    )
    assert len(outcome.scheduled) == 3, (
        f"Expected 3 scheduled, got {len(outcome.scheduled)}"
    )


def test_benders_smoke_converges_quickly():
    """Benders loop must converge in ≤3 iterations on the 3-order fixture."""
    from frigilunch.domain.oracle import schedule_day

    orders, products = _load_fixture()
    config = {**_make_config(), "max_iterations": 3}

    outcome = run_benders(
        orders=orders,
        products=products,
        solvers=Solvers(cp=_stub_cp_solver, oracle=schedule_day),
        config=config,
    )

    assert outcome.stats["iterations"] <= 3, (
        f"Expected ≤3 iterations, got {outcome.stats['iterations']}"
    )
