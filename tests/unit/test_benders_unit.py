"""
tests/unit/test_benders_unit.py

Unit tests for benders.py using stub callables.
Tests the infeasible path, max-iterations limit, and score computation.
"""
from __future__ import annotations

from datetime import date, time

import pytest

from frigilunch.domain.benders import run_benders, Solvers
from frigilunch.domain.models import Order, Product


def _make_order(order_nr: str, prod_date: date, lijn: int = 1) -> Order:
    return Order(
        order_nr=order_nr, art_nr="P001", klant="K",
        aantal=100, lever_datum=prod_date, productie_datum=prod_date, lijn=lijn,
    )


def _make_product() -> Product:
    return Product(art_nr="P001", naam="Test", schaal="UK59", kapvorm="A", snelheid=200)


def _base_config() -> dict:
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


# ── infeasible solver ─────────────────────────────────────────────────────────

def test_benders_infeasible_status():
    """When the CP solver returns infeasible, all orders are rejected."""
    datum = date(2026, 10, 7)
    orders = [_make_order("O001", datum)]
    products = {"P001": _make_product()}

    def infeasible_solver(orders, frozen_dates, cuts, config):
        return {"status": "infeasible", "assignments": {}}

    def dummy_oracle(orders, products, datum, config):
        from frigilunch.domain.models import DaySchedule
        return DaySchedule(datum=datum)

    outcome = run_benders(
        orders=orders, products=products,
        solvers=Solvers(cp=infeasible_solver, oracle=dummy_oracle),
        config={**_base_config(), "max_iterations": 5},
    )

    assert outcome.status == "infeasible"
    assert len(outcome.rejected) == 1
    assert len(outcome.scheduled) == 0


def test_benders_unknown_status_from_solver():
    """When the solver returns 'unknown', all orders are rejected."""
    datum = date(2026, 10, 7)
    orders = [_make_order("O001", datum)]
    products = {"P001": _make_product()}

    def unknown_solver(orders, frozen_dates, cuts, config):
        return {"status": "unknown", "assignments": {}}

    def dummy_oracle(orders, products, datum, config):
        from frigilunch.domain.models import DaySchedule
        return DaySchedule(datum=datum)

    outcome = run_benders(
        orders=orders, products=products,
        solvers=Solvers(cp=unknown_solver, oracle=dummy_oracle),
        config={**_base_config(), "max_iterations": 5},
    )

    assert outcome.status == "unknown"
    assert outcome.score.rejected_orders == 1


def test_benders_score_reflects_rejections():
    """PlanScore.rejected_orders matches outcome.rejected length."""
    datum = date(2026, 10, 7)
    orders = [_make_order("O001", datum), _make_order("O002", datum)]
    products = {"P001": _make_product()}

    def always_feasible(orders, frozen_dates, cuts, config):
        return {"status": "feasible", "assignments": {
            o.order_nr: {"date": datum, "lijn": 1} for o in orders
        }}

    from frigilunch.domain.oracle import schedule_day

    outcome = run_benders(
        orders=orders, products=products,
        solvers=Solvers(cp=always_feasible, oracle=schedule_day),
        config={**_base_config(), "max_iterations": 3},
    )

    assert outcome.score.rejected_orders == len(outcome.rejected)
    assert outcome.score.rejected_units == sum(o.aantal for o in outcome.rejected)


def test_benders_empty_orders():
    """Empty order list → complete with no scheduled and no rejected."""
    def noop_solver(orders, frozen_dates, cuts, config):
        return {"status": "feasible", "assignments": {}}

    def noop_oracle(orders, products, datum, config):
        from frigilunch.domain.models import DaySchedule
        return DaySchedule(datum=datum)

    outcome = run_benders(
        orders=[], products={},
        solvers=Solvers(cp=noop_solver, oracle=noop_oracle),
        config={**_base_config(), "max_iterations": 1},
    )

    assert outcome.status == "complete"
    assert len(outcome.scheduled) == 0
    assert len(outcome.rejected) == 0
