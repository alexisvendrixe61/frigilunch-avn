"""
The exception list must contain exactly the rejections a person has to act on
— and nothing else. If ordinary capacity rejections leaked into it, the list
would be long enough to ignore, which defeats the point.
"""
from __future__ import annotations

import csv
from datetime import date

from src import exceptions_report
from src.plan_state import order_identity
from tests.conftest import make_order, make_product

D = date(2026, 7, 31)


def _reasons(*pairs):
    return {order_identity(o): r for o, r in pairs}


def test_only_standing_problems_are_listed():
    """
    NEVER-PLACED and friends are the solver's problem and may resolve
    themselves next week as the order book shifts. They must not appear here.
    """
    frozen = make_order("A", 100, D, order_nr="frozen")
    contested = make_order("A", 100, D, order_nr="contested")
    dropped = make_order("A", 100, D, order_nr="dropped")
    combined = make_order("A", 100, D, order_nr="combined")
    products = {"A": make_product("A")}

    rows = exceptions_report.build(
        [frozen, contested, dropped, combined], products,
        _reasons(
            (frozen, "ARRIVED-AFTER-FREEZE: ..."),
            (contested, "NEVER-PLACED: ..."),
            (dropped, "ORACLE-DROPPED: ..."),
            (combined, "COMBINED-SCHEDULE-DROP: ..."),
        ),
    )
    assert [r["order_nr"] for r in rows] == ["frozen"]


def test_every_standing_cause_is_covered():
    products = {"A": make_product("A")}
    cases = {
        "ARRIVED-AFTER-FREEZE: x": "Leverdatum ligt volledig in een al bevroren week",
        "WINDOW-PASSED: x": "Productievenster al verstreken",
        "LINE-DAY-RESTRICTION: x": "Geen toegelaten productiedag/lijn in het leveringsvenster",
        "Product A unknown": "Artikel niet in de productdatabase",
    }
    for reason, expected in cases.items():
        o = make_order("A", 100, D, order_nr="x")
        rows = exceptions_report.build([o], products, _reasons((o, reason)))
        assert len(rows) == 1, reason
        assert rows[0]["probleem"] == expected
        assert rows[0]["actie"], "every problem needs an action"


def test_an_unknown_product_still_produces_a_usable_row():
    """The product isn't in the database — that's the whole problem — so the
    report must not need it to render the row."""
    o = make_order("MISSING", 500, D, order_nr="x", klant="ALDI BE")
    rows = exceptions_report.build([o], {}, _reasons((o, "Product MISSING unknown")))

    assert len(rows) == 1
    assert rows[0]["art_nr"] == "MISSING"
    assert rows[0]["klant"] == "ALDI BE"
    assert rows[0]["aantal"] == 500
    assert rows[0]["productie_minuten"] is None
    assert rows[0]["naam"] == ""
    assert rows[0]["category"] == "invalid_master_data"


def test_rows_are_sorted_by_delivery_date():
    products = {"A": make_product("A")}
    late = make_order("A", 100, date(2026, 9, 1), order_nr="late")
    early = make_order("A", 100, date(2026, 8, 1), order_nr="early")
    rows = exceptions_report.build(
        [late, early], products,
        _reasons((late, "WINDOW-PASSED: x"), (early, "WINDOW-PASSED: x")),
    )
    assert [r["order_nr"] for r in rows] == ["early", "late"]


def test_nothing_to_decide_produces_nothing(capsys):
    rows = exceptions_report.build([], {}, {})
    assert rows == []
    exceptions_report.print_report(rows)
    assert capsys.readouterr().out == "", "a clean run must stay silent"


def test_csv_round_trips(tmp_path):
    products = {"A": make_product("A", naam="AARDAPPELGRATIN 450G")}
    o = make_order("A", 26810, D, order_nr="x", klant="lidl be")
    rows = exceptions_report.build([o], products, _reasons((o, "ARRIVED-AFTER-FREEZE: x")))

    path = tmp_path / "actie.csv"
    exceptions_report.write_csv(rows, path)

    with open(path, newline="", encoding="utf-8-sig") as f:
        out = list(csv.DictReader(f))
    assert len(out) == 1
    assert out[0]["art_nr"] == "A"
    assert out[0]["aantal"] == "26810"
    assert out[0]["naam"] == "AARDAPPELGRATIN 450G"
    assert "bevroren week" in out[0]["probleem"]
    assert out[0]["category"] == "late_after_freeze"


# ── A frozen-out order reports the cause, not the freeze ─────────────────────
# The freeze is when it became unreachable, never why it failed. Measured on a
# real run chain, all 8 of these were booked months ahead (Apr-Aug for Sep
# deliveries) and lost by earlier runs on their own merits; the freeze only
# closed over them afterwards. Reporting "delivery falls in a frozen week" as
# the problem sends the planner after the wrong thing entirely.

def _frozen_with_history(order, original_reason, *, pogingen=2, eerste=32, laatste=33):
    """A reason built the way create_plan builds it, via plan_state itself."""
    from src.plan_state import explain_from_history

    return "ARRIVED-AFTER-FREEZE: ..." + explain_from_history({
        "pogingen": pogingen, "eerste_week": eerste,
        "laatste_week": laatste, "reason": original_reason,
    })


def test_the_carried_cause_replaces_the_freeze_as_the_problem():
    o = make_order("A", 100, D, order_nr="positie")
    products = {"A": make_product("A", afsluit_kan_niet=True)}
    rows = exceptions_report.build(
        [o], products,
        _reasons((o, _frozen_with_history(o, "POSITIE-REGEL: mag niet afsluiten..."))),
    )
    assert len(rows) == 1
    assert "mag de dag niet openen/afsluiten" in rows[0]["probleem"]
    assert "opstart/afsluit-regel" in rows[0]["actie"]


def test_the_freeze_still_sets_the_deadline():
    """The cause changes the action; the freeze changes how urgent it is."""
    o = make_order("A", 100, D, order_nr="positie")
    rows = exceptions_report.build(
        [o], {"A": make_product("A")},
        _reasons((o, _frozen_with_history(o, "POSITIE-REGEL: ..."))),
    )
    assert "nu ook bevroren" in rows[0]["probleem"]
    assert "bevroren" in rows[0]["actie"]


def test_each_carried_cause_gets_its_own_action():
    """Capacity and position rejections must not share one generic remedy."""
    cases = {
        "POSITIE-REGEL: ...": "opstart/afsluit-regel",
        "NEVER-PLACED: ...": "overboekt",
        "ORACLE-DROPPED: ...": "ombouwtijden",
        "COMBINED-SCHEDULE-DROP: ...": "gecombineerde schema",
    }
    for original, expected in cases.items():
        o = make_order("A", 100, D, order_nr=original[:6])
        rows = exceptions_report.build(
            [o], {"A": make_product("A")},
            _reasons((o, _frozen_with_history(o, original))),
        )
        assert expected in rows[0]["actie"], f"{original} -> {rows[0]['actie']}"


def test_a_freeze_without_history_keeps_the_original_wording():
    """Nothing carried forward means nothing better to say."""
    o = make_order("A", 100, D, order_nr="bare")
    rows = exceptions_report.build(
        [o], {"A": make_product("A")}, _reasons((o, "ARRIVED-AFTER-FREEZE: ...")),
    )
    assert rows[0]["probleem"] == "Leverdatum ligt volledig in een al bevroren week"


def test_an_unrecognised_carried_cause_falls_back_rather_than_crashing():
    o = make_order("A", 100, D, order_nr="weird")
    rows = exceptions_report.build(
        [o], {"A": make_product("A")},
        _reasons((o, _frozen_with_history(o, "SOMETHING-NEW: ..."))),
    )
    assert rows[0]["probleem"] == "Leverdatum ligt volledig in een al bevroren week"
