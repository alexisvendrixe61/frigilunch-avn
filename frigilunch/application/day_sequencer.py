"""
day_sequencer.py — CP-SAT "second opinion" for one line's daily order sequence.

Ported from src.day_sequencer; all imports now point to frigilunch.
"""
from __future__ import annotations

import os as _os

from ortools.sat.python import cp_model

from frigilunch.domain.models import Order, Product
from frigilunch.domain.constraints import (
    SATE_EXCLUSIVE,
    calculate_duration,
    is_sate,
    mag_eerst,
    mag_laatst,
    moet_eerst,
    moet_laatst,
    personeel_for_lijn,
    sluit_samen,
    transition_cost,
)
from frigilunch.domain.constraints.device import blocked_devices
from frigilunch.domain.constraints.grouping_rules import (
    GEEN_VOLGENDE, Volgende, kapvorm_sleutel, schaal_sleutel,
)
from frigilunch.domain.constraints.staffing import MAX_BEZETTING_DELTA as _MAX_BEZETTING_DELTA

_POSITIE_BONUS = 1_000

IDENTIEK_BONUS    = int(_os.environ.get("FRIGI_SEQ_IDENTIEK_BONUS",    "400"))
KAPVORM_BONUS     = int(_os.environ.get("FRIGI_SEQ_KAPVORM_BONUS",     "120"))
SCHAAL_BONUS      = int(_os.environ.get("FRIGI_SEQ_SCHAAL_BONUS",       "90"))
IDLE_WEIGHT       = int(_os.environ.get("FRIGI_SEQ_IDLE_WEIGHT",          "1"))
L1_KAPVORM_BONUS  = int(_os.environ.get("FRIGI_SEQ_L1_KAPVORM_BONUS", "2000"))
_SEQUENCE_TIME_LIMIT = float(_os.environ.get("FRIGI_SEQUENCE_TIME_LIMIT", "2.0"))


def kapvorm_bonus(lijn_nr: int | None) -> int:
    return L1_KAPVORM_BONUS if lijn_nr == 1 else KAPVORM_BONUS


def arc_bonus(
    prev: tuple[Order, Product], nxt: tuple[Order, Product], lijn_nr: int | None = None,
) -> int:
    (o1, p1), (o2, p2) = prev, nxt
    bonus = 0
    if p1.identiek_groep and p1.identiek_groep == p2.identiek_groep and o1.aantal <= o2.aantal:
        bonus += IDENTIEK_BONUS
    return bonus + carry_bonus(p1, p2, lijn_nr)


def carry_bonus(prev: Product | None, nxt: Product, lijn_nr: int | None = None) -> int:
    if prev is None:
        return 0
    bonus = 0
    kap = kapvorm_sleutel(prev)
    if kap is not None and kap == kapvorm_sleutel(nxt):
        bonus += kapvorm_bonus(lijn_nr)
    sch = schaal_sleutel(prev)
    if sch is not None and sch == schaal_sleutel(nxt):
        bonus += SCHAAL_BONUS
    return bonus


def close_bonus(last: Product, volgende: Volgende, lijn_nr: int | None = None) -> int:
    next_kap, next_sch = volgende
    bonus = 0
    kap = kapvorm_sleutel(last)
    if kap is not None and kap in next_kap:
        bonus += kapvorm_bonus(lijn_nr)
    sch = schaal_sleutel(last)
    if sch is not None and sch in next_sch:
        bonus += SCHAAL_BONUS
    return bonus


def solve_optimal_sequence(
    orders_products: list[tuple[Order, Product]],
    setup_matrix: dict,
    start_time_min: int,
    max_end_min: int,
    lijn_nr: int,
    time_limit_seconds: float = _SEQUENCE_TIME_LIMIT,
    blocking: dict[str, list[tuple[int, int]]] | None = None,
    toestellen_avail: dict[str, int] | None = None,
    vorige: Product | None = None,
    volgende: Volgende = GEEN_VOLGENDE,
) -> list[tuple[Order, Product]] | None:
    """
    Search for a subset + sequence that fits within [start_time_min, max_end_min],
    maximizing orders included. Returns the sequence or None.
    """
    n = len(orders_products)
    if n <= 1:
        return None

    products = [p for _, p in orders_products]
    durations = [calculate_duration(o, p) for o, p in orders_products]
    man = [personeel_for_lijn(p, lijn_nr) for p in products]

    cost = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            cost[i][j] = transition_cost(products[i], products[j], setup_matrix)

    model = cp_model.CpModel()
    DEPOT = n

    arcs: list[tuple[int, int, object]] = []
    skip = [model.NewBoolVar(f"skip_{i}") for i in range(n)]
    for i in range(n):
        arcs.append((i, i, skip[i]))

    order_arc: dict[tuple[int, int], object] = {}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if man[j] - man[i] > _MAX_BEZETTING_DELTA and durations[j] <= 4 * 60:
                continue
            if SATE_EXCLUSIVE and is_sate(products[i]) and not is_sate(products[j]):
                continue
            other_art = products[i].art_nr != products[j].art_nr
            if other_art and moet_eerst(products[j]):
                continue
            if (
                moet_laatst(products[i]) and not moet_eerst(products[i])
                and not sluit_samen(products[i], products[j])
            ):
                continue
            b = model.NewBoolVar(f"arc_{i}_{j}")
            order_arc[(i, j)] = b
            arcs.append((i, j, b))

    depot_out = [model.NewBoolVar(f"depot_out_{i}") for i in range(n)]
    depot_in  = [model.NewBoolVar(f"depot_in_{i}")  for i in range(n)]
    for i in range(n):
        arcs.append((DEPOT, i, depot_out[i]))
        arcs.append((i, DEPOT, depot_in[i]))
    depot_skip = model.NewBoolVar("depot_skip")
    arcs.append((DEPOT, DEPOT, depot_skip))

    model.AddCircuit(arcs)

    for i, (_, product) in enumerate(orders_products):
        if not mag_eerst(product):
            model.Add(depot_out[i] == 0)
        if not mag_laatst(product):
            model.Add(depot_in[i] == 0)

    dev_blocked = {
        i: [
            iv for dev in blocked_devices(product, blocking, toestellen_avail)
            for iv in blocking[dev]
        ]
        for i, (_, product) in enumerate(orders_products)
    }
    dev_blocked = {i: ivs for i, ivs in dev_blocked.items() if ivs}

    arrival = [model.NewIntVar(start_time_min, max_end_min, f"arr_{i}") for i in range(n)]
    finish  = [model.NewIntVar(start_time_min, max_end_min, f"fin_{i}") for i in range(n)]
    for i in range(n):
        model.Add(finish[i] == arrival[i] + durations[i])
        model.Add(finish[i] <= max_end_min).OnlyEnforceIf(skip[i].Not())
        if dev_blocked:
            model.Add(arrival[i] >= start_time_min).OnlyEnforceIf(depot_out[i])
        else:
            model.Add(arrival[i] == start_time_min).OnlyEnforceIf(depot_out[i])

    for (i, j), b in order_arc.items():
        model.Add(arrival[j] >= finish[i] + cost[i][j]).OnlyEnforceIf(b)

    for i, intervals in dev_blocked.items():
        for busy_start, busy_end in intervals:
            before = model.NewBoolVar(f"dev_{i}_{busy_start}")
            model.Add(finish[i] <= busy_start).OnlyEnforceIf([before, skip[i].Not()])
            model.Add(arrival[i] >= busy_end).OnlyEnforceIf([before.Not(), skip[i].Not()])

    included  = [skip[i].Not() for i in range(n)]
    total_cost = sum(b * cost[i][j] for (i, j), b in order_arc.items())
    adj_bonus  = sum(
        b * arc_bonus(orders_products[i], orders_products[j], lijn_nr)
        for (i, j), b in order_arc.items()
    )

    day_end = model.NewIntVar(start_time_min, max_end_min, "day_end")
    for i in range(n):
        model.Add(day_end >= finish[i]).OnlyEnforceIf(skip[i].Not())
    idle = (
        day_end - start_time_min
        - sum(durations[i] * (1 - skip[i]) for i in range(n))
        - total_cost
    )
    positie_bonus = sum(
        depot_out[i] for i, (_, p) in enumerate(orders_products) if moet_eerst(p)
    ) + sum(
        depot_in[i] for i, (_, p) in enumerate(orders_products) if moet_laatst(p)
    )
    open_bonus = sum(
        depot_out[i] * carry_bonus(vorige, products[i], lijn_nr) for i in range(n)
    ) + sum(
        depot_in[i] * close_bonus(products[i], volgende, lijn_nr) for i in range(n)
    )

    model.Maximize(
        sum(included) * 100_000
        + positie_bonus * _POSITIE_BONUS
        + adj_bonus
        + open_bonus
        - total_cost
        - IDLE_WEIGHT * idle
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    included_idx = [i for i in range(n) if solver.Value(skip[i]) == 0]
    if not included_idx:
        return None

    included_idx.sort(key=lambda i: solver.Value(arrival[i]))
    return [orders_products[i] for i in included_idx]
