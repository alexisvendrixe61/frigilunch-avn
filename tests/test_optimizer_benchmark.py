from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.scheduler_ortools_v3 import assign_production_dates_feedback
from tests.conftest import make_order, make_product


FIXTURE = Path(__file__).parent / "fixtures" / "rolling_wk36_wk40" / "week40_targets.json"


def _case():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _orders_and_products():
    case = _case()
    delivery = date.fromisoformat(case["delivery_date"])
    orders = []
    products = {}
    for item in case["orders"]:
        article = item["article"]
        speed = round(item["units"] * 60 / item["minutes"])
        product = make_product(
            article,
            snelheid=speed,
            dagen=item["days"],
            devices=item["devices"],
            opstart_kan_niet=not item["may_open"],
            afsluit_kan_niet=not item["may_close"],
        )
        product.schaal = "UK-59"
        products[article] = product
        order = make_order(
            article,
            item["units"],
            date.fromisoformat(case["run_start"]),
            lever=delivery,
            order_nr=f"fixture-{item['plan_nr']}",
        )
        order.plan_nr = item["plan_nr"]
        orders.append(order)
    return case, orders, products


@pytest.mark.parametrize("run_start", _case()["run_starts"])
def test_known_week40_targets_fit_in_each_unfrozen_week36_to_40_snapshot(
    setup_matrix, run_start
):
    case, orders, products = _orders_and_products()
    start = date.fromisoformat(run_start)
    known_plan_numbers = {
        item["plan_nr"] for item in case["orders"]
        if date.fromisoformat(item["known_date"]) <= start
    }
    known_orders = [order for order in orders if order.plan_nr in known_plan_numbers]
    weeks = max(1, ((date.fromisoformat(case["delivery_date"]) - start).days // 7) + 1)

    outcome = assign_production_dates_feedback(
        known_orders,
        products,
        start,
        weeks=weeks,
        setup_matrix=setup_matrix,
        toestellen={"gernal": 1, "multifill": 1},
    )

    assert outcome.status == "complete", (run_start, outcome.diagnostics)
    assert {order.plan_nr for order in outcome.scheduled} == known_plan_numbers


def test_week40_target_book_fits_when_known_before_freeze(setup_matrix):
    case, orders, products = _orders_and_products()
    start = date.fromisoformat(case["run_start"])

    outcome = assign_production_dates_feedback(
        orders,
        products,
        start,
        weeks=4,
        setup_matrix=setup_matrix,
        toestellen={"gernal": 1, "multifill": 1},
    )

    assert outcome.status == "complete"
    assert len({order.plan_nr for order in outcome.scheduled}) == 7
    assert outcome.score.rejected_orders == 0
    assert all(order.productie_datum <= order.lever_datum for order in outcome.scheduled)


def test_same_book_is_never_inserted_into_an_already_frozen_past(setup_matrix):
    _case_data, orders, products = _orders_and_products()
    frozen_until = date(2026, 10, 19)

    outcome = assign_production_dates_feedback(
        orders,
        products,
        frozen_until,
        weeks=4,
        setup_matrix=setup_matrix,
        toestellen={"gernal": 1, "multifill": 1},
        frozen_until=frozen_until,
    )

    assert outcome.status == "infeasible"
    assert outcome.scheduled == ()
    assert len(outcome.rejected) == 7
    assert {item.category for item in outcome.diagnostics} == {"late_after_freeze"}
