from __future__ import annotations

from datetime import date

from src.models import SolveOutcome
from src.scheduler_ortools_v3 import assign_production_dates_feedback
from tests.conftest import make_order, make_product


MONDAY = date(2026, 9, 28)


def _solve(orders, products, setup_matrix):
    return assign_production_dates_feedback(
        orders,
        products,
        MONDAY,
        weeks=1,
        setup_matrix=setup_matrix,
        toestellen={},
    )


def test_solver_returns_structured_complete_outcome(setup_matrix):
    products = {"A": make_product("A", snelheid=1000, alleen_l1=True)}
    order = make_order("A", 100, MONDAY, lever=MONDAY, order_nr="one")

    outcome = _solve([order], products, setup_matrix)

    assert isinstance(outcome, SolveOutcome)
    assert outcome.status == "complete"
    assert [o.order_nr for o in outcome.scheduled] == ["one"]
    assert outcome.rejected == ()
    # Existing callers may continue to unpack the result during migration.
    scheduled, rejected, stats = outcome
    assert scheduled == list(outcome.scheduled)
    assert rejected == []
    assert stats["outcome_status"] == "complete"


def test_unknown_product_is_structured_invalid_master_data(setup_matrix):
    order = make_order("MISSING", 100, MONDAY, lever=MONDAY, order_nr="bad")

    outcome = _solve([order], {}, setup_matrix)

    assert outcome.status == "infeasible"
    assert outcome.diagnostics[0].category == "invalid_master_data"


def test_inferred_speed_is_rejected_as_invalid_master_data(setup_matrix):
    product = make_product("A", snelheid=1000)
    product.constraint_provenance["speed"] = "inferred"
    order = make_order("A", 100, MONDAY, lever=MONDAY, order_nr="no-speed")

    outcome = _solve([order], {"A": product}, setup_matrix)

    assert outcome.status == "infeasible"
    assert outcome.diagnostics[0].category == "invalid_master_data"
    assert next(iter(outcome.stats["rejection_reasons"].values())).startswith("MISSING-SPEED")


def test_proven_mandatory_model_infeasibility_runs_diagnostic_relaxation(setup_matrix):
    products = {
        "A": make_product("A", snelheid=100, alleen_l1=True, dagen=["ma"]),
        "B": make_product("B", snelheid=100, alleen_l1=True, dagen=["ma"]),
    }
    orders = [
        make_order("A", 1000, MONDAY, lever=MONDAY, order_nr="a"),
        make_order("B", 1000, MONDAY, lever=MONDAY, order_nr="b"),
    ]

    outcome = _solve(orders, products, setup_matrix)

    assert outcome.status == "infeasible"
    assert outcome.stats["primary_solver_status"] == "INFEASIBLE"
    assert len(outcome.scheduled) + len(outcome.rejected) == 2
    assert outcome.rejected
