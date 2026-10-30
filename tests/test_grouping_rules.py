"""
G1 / G2 — which orders belong together.

Column 15 ("identiek product") is a link list, not a group id, and the sheet
does not always state a link from both sides. These tests pin how it is read.
"""
from __future__ import annotations

from frigilunch.domain.constraints.grouping_rules import (
    identiek_groepen,
    kapvorm_sleutel,
    parse_identiek_refs,
    schaal_sleutel,
)

from conftest import make_product


def test_a_link_stated_from_both_sides_is_one_group():
    groups = identiek_groepen({"65107300": {"65107400"}, "65107400": {"65107300"}})
    assert groups == {"65107300": "65107300", "65107400": "65107300"}


def test_a_triple_is_one_group_even_when_one_member_names_only_two_partners():
    groups = identiek_groepen({
        "63319800": parse_identiek_refs("'63562800 - 63963500 '"),
        "63562800": parse_identiek_refs("'63319800 - 63963500 '"),
        "63963500": parse_identiek_refs("'63319800'"),
    })
    assert set(groups.values()) == {"63319800"}
    assert len(groups) == 3


def test_a_link_to_an_unknown_article_makes_no_group():
    assert identiek_groepen({"63551900": {"99999999"}}) == {}


def test_junk_and_seven_digit_typos_are_not_links():
    assert parse_identiek_refs("'63902900 - 6392900'") == {"63902900"}
    assert parse_identiek_refs("'f'") == set()
    assert parse_identiek_refs("0") == set()
    assert parse_identiek_refs(" ") == set()


def test_kapvorm_without_a_real_value_has_no_key():
    for raw in ("NVT", "nvt ", "-", ""):
        assert kapvorm_sleutel(make_product("A", kapvorm=raw)) is None
    assert kapvorm_sleutel(make_product("A", kapvorm=" h22")) == "H22"


# ── the day sort (S1 with G1 and G2 on top) ──────────────────────────────────

from datetime import date  # noqa: E402

from frigilunch.domain.constraints.sequencing import sort_orders_for_lijn  # noqa: E402

from conftest import make_order  # noqa: E402

DAG = date(2026, 11, 3)  # a Tuesday


def _names(seq):
    return [o.order_nr for o, _ in seq]


def test_identical_products_end_up_adjacent_smallest_first():
    a = make_product("A", kapvorm="NVT", identiek_groep="A")
    a_ew = make_product("A_EW", kapvorm="NVT", identiek_groep="A")
    other = make_product("B", kapvorm="NVT")
    seq = [
        (make_order("A", 5000, DAG, order_nr="a-big"), a),
        (make_order("B", 3000, DAG, order_nr="b"), other),
        (make_order("A_EW", 1000, DAG, order_nr="a-small"), a_ew),
    ]
    names = _names(sort_orders_for_lijn(seq))
    i = names.index("a-small")
    assert names[i + 1] == "a-big"


def test_same_kapvorm_ends_up_adjacent():
    h22a = make_product("X", kapvorm="H22", allergie_niveau=1)
    h25 = make_product("Y", kapvorm="H25", allergie_niveau=0)
    h22b = make_product("Z", kapvorm="H22", allergie_niveau=3)
    seq = [
        (make_order("X", 1000, DAG, order_nr="x"), h22a),
        (make_order("Y", 1000, DAG, order_nr="y"), h25),
        (make_order("Z", 1000, DAG, order_nr="z"), h22b),
    ]
    names = _names(sort_orders_for_lijn(seq))
    assert abs(names.index("x") - names.index("z")) == 1


def test_identical_beats_kapvorm():
    p = make_product("P", kapvorm="H22", identiek_groep="P")
    q = make_product("Q", kapvorm="H44", identiek_groep="P")
    r = make_product("R", kapvorm="H22")
    seq = [
        (make_order("P", 2000, DAG, order_nr="p"), p),
        (make_order("R", 2000, DAG, order_nr="r"), r),
        (make_order("Q", 1000, DAG, order_nr="q"), q),
    ]
    names = _names(sort_orders_for_lijn(seq))
    assert names.index("p") == names.index("q") + 1


def test_schaal_key_folds_case_and_whitespace():
    assert schaal_sleutel(make_product("A", schaal=" uk59  kraft")) == "UK59 KRAFT"
    assert schaal_sleutel(make_product("A", schaal="")) is None


def test_same_schaal_ends_up_adjacent_without_a_kapvorm():
    """G3 — L2 is nearly all "NVT", so there schaal is the layer that groups."""
    x = make_product("X", kapvorm="NVT", schaal="C2227-2K", allergie_niveau=1)
    y = make_product("Y", kapvorm="NVT", schaal="C2325-1C", allergie_niveau=2)
    z = make_product("Z", kapvorm="NVT", schaal="C2227-2K", allergie_niveau=3)
    seq = [
        (make_order("X", 1000, DAG, order_nr="x"), x),
        (make_order("Y", 1000, DAG, order_nr="y"), y),
        (make_order("Z", 1000, DAG, order_nr="z"), z),
    ]
    names = _names(sort_orders_for_lijn(seq))
    assert abs(names.index("x") - names.index("z")) == 1


def test_same_schaal_ends_up_adjacent_inside_a_kapvorm():
    """The screenshot of 15/10: UK-59 H22, C2187 H22, UK-59 H22 split the UK-59s."""
    a = make_product("A", kapvorm="H22", schaal="UK-59", allergie_niveau=1)
    b = make_product("B", kapvorm="H22", schaal="C2187-1F", allergie_niveau=2)
    c = make_product("C", kapvorm="H22", schaal="UK-59", allergie_niveau=3)
    seq = [
        (make_order("A", 1000, DAG, order_nr="a"), a),
        (make_order("B", 1000, DAG, order_nr="b"), b),
        (make_order("C", 1000, DAG, order_nr="c"), c),
    ]
    names = _names(sort_orders_for_lijn(seq))
    assert abs(names.index("a") - names.index("c")) == 1


def test_without_kapvorm_or_groups_the_order_is_allergen_ascending():
    prods = [make_product(n, kapvorm="NVT", allergie_niveau=lvl)
             for n, lvl in (("L3", 3), ("L1", 1), ("L2", 2))]
    seq = [(make_order(p.art_nr, 1000, DAG, order_nr=p.art_nr), p) for p in prods]
    assert _names(sort_orders_for_lijn(seq)) == ["L1", "L2", "L3"]



# ── the assignment model's regrouping neighbourhood ──────────────────────────

from ortools.sat.python import cp_model  # noqa: E402

from frigilunch.application.date_optimizer import _regroup_identical  # noqa: E402


class _Incumbent:
    """Stands in for the main solve's solver: a fixed, deliberately split solution."""

    def __init__(self, values, presence, objective):
        self._by_index = {presence[k].Index(): v for k, v in values.items()}
        self._objective = objective

    def Value(self, var):
        return self._by_index[var.Index()]

    def ObjectiveValue(self):
        return self._objective


def _toy_model():
    """Units 0 and 1 form a group; unit 2 is a bystander that prefers day 1.
    Each day the group occupies costs 40."""
    model = cp_model.CpModel()
    presence = {(u, d, 2): model.NewBoolVar(f"p{u}{d}") for u in range(3) for d in range(2)}
    for u in range(3):
        model.AddExactlyOne(presence[(u, 0, 2)], presence[(u, 1, 2)])
    active = [model.NewBoolVar(f"g{d}") for d in range(2)]
    for d in range(2):
        model.AddMaxEquality(active[d], [presence[(0, d, 2)], presence[(1, d, 2)]])
    model.Minimize(40 * sum(active) + presence[(2, 0, 2)])
    return model, presence


def test_regroup_joins_the_group_and_leaves_everyone_else_alone():
    model, presence = _toy_model()
    split = {k: 0 for k in presence}
    split.update({(0, 0, 2): 1, (1, 1, 2): 1, (2, 0, 2): 1})   # objective 81

    result = _regroup_identical(model, _Incumbent(split, presence, 81), presence, {"G": [0, 1]})

    day_of = {u: next(d for d in range(2) if result.Value(presence[(u, d, 2)])) for u in range(3)}
    assert day_of[0] == day_of[1]
    assert day_of[2] == 0          # held where the incumbent had it, not moved to its better day


def test_regroup_keeps_the_incumbent_when_nothing_is_split():
    model, presence = _toy_model()
    together = {k: 0 for k in presence}
    together.update({(0, 1, 2): 1, (1, 1, 2): 1, (2, 1, 2): 1})
    incumbent = _Incumbent(together, presence, 40)

    assert _regroup_identical(model, incumbent, presence, {"G": [0, 1]}) is incumbent
