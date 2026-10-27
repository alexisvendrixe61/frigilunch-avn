"""
day_sequencer.py — CP-SAT "second opinion" for one line's daily order sequence.

`scheduler._sort_orders_for_lijn` picks ONE fixed heuristic order (group by
kapvorm, then ascending allergen level) and `_schedule_lijn` walks it once,
dropping whatever runs past the shift end. That's fast but it only ever tries
one sequence — an order can get dropped not because it genuinely can't fit
anywhere, but because this specific guessed order happened to strand it late.

This module answers the sharper question directly: "does SOME sequence of
these orders fit in the shift window?" It's a prize-collecting TSP with time
windows — each order is a node, edges carry the real sequence-dependent setup
cost (`calculate_setup_time`), and nodes can be optionally excluded (a self
loop) when the window genuinely can't fit everyone. CP-SAT's `AddCircuit`
with self-loops-as-skip is the standard formulation for "routing with drops".

Beyond fitting, the objective also shapes the day the planner asked for
(meeting 02/10): identical products back to back and smallest first (G1),
same kapvorm back to back (G2), and no idle holes — a day built from the
line start without gaps, so a line runs until its work is done instead of
waiting hours on a machine the other line holds (O6).

The caller always re-verifies any sequence this module proposes through the
real `_schedule_lijn` walker before accepting it, so any modeling gap here
can only cost a missed recovery, never produce a wrong schedule.
"""
from __future__ import annotations

import os as _os

from ortools.sat.python import cp_model

from .models import Order, Product
from .constraints import (
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
from .constraints.device_rules import blocked_devices
from .constraints.grouping_rules import (
    GEEN_VOLGENDE, Volgende, kapvorm_sleutel, schaal_sleutel,
)
from .constraints.staffing_rules import MAX_BEZETTING_DELTA as _MAX_BEZETTING_DELTA

# S2 bonus per satisfied "moet" placement. Far below the 100_000 weight on
# including an order, so the solver can never buy a first/last placement by
# dropping an order, but well above any realistic sum of setup minutes, so it
# always outranks the changeover tiebreaker.
_POSITIE_BONUS = 1_000

# The day's shape, in setup-minute units so every term below is comparable.
# Order of the whole objective: included orders (100 000) > same kapvorm back
# to back ON L1 (2 000 per arc) > positie (1 000) > identical products back to
# back (400 per arc) > same kapvorm back to back on L2 (120 per arc) > same
# schaal back to back (90 per arc) > idle minute (1) ~ changeover minute (1). The S2 requirements are no longer bought with the
# positie bonus alone: the arcs that would break one do not exist (see below).
#
# IDENTIEK_BONUS — G1. Paid per arc between two orders of the same identical
#   group, smaller (or equal) order first. Large enough to outweigh the setup
#   differences a sequence can buy elsewhere and a gap of several hours, small
#   enough that a first/last-of-day requirement still wins.
# KAPVORM_BONUS — G2. Paid per arc between two orders with the same real
#   kapvorm. Was 60 — "above any single changeover difference" — and that lost
#   to idle time and cleaning: the planner still got H22 H44 H11 H22 H22 on one
#   line-day (15/10). At 120 a kapvorm break is worth two hours of idle line,
#   so grouping wins everything except a real order or a position rule.
# SCHAAL_BONUS — G3. Same, per arc between two orders on the same schaal
#   (`schaal_sleutel`, the exact tray). Just below kapvorm: the kapvorm is the
#   heavier swap on the floor, and the two usually agree anyway.
#   Both bonuses are also paid once for OPENING the day with the kapvorm /
#   schaal the line closed the previous workday on (`carry_bonus`), which is
#   what "after each other over days" means for the sequence.
# IDLE_WEIGHT — O6. Per minute the line stands idle between its start and its
#   last order — the hole a device block or a badly ordered day leaves.
IDENTIEK_BONUS = int(_os.environ.get("FRIGI_SEQ_IDENTIEK_BONUS", "400"))
KAPVORM_BONUS = int(_os.environ.get("FRIGI_SEQ_KAPVORM_BONUS", "120"))
SCHAAL_BONUS = int(_os.environ.get("FRIGI_SEQ_SCHAAL_BONUS", "90"))
IDLE_WEIGHT = int(_os.environ.get("FRIGI_SEQ_IDLE_WEIGHT", "1"))

# L1_KAPVORM_BONUS — G2 on L1, the automatic packing line, where every kapvorm
#   change is a changeover the planner wants gone (22/10: "this rule takes
#   priority over every other setting"). Replaces KAPVORM_BONUS on L1. At 2000
#   one same-kapvorm arc outweighs an identical pair (400) plus a whole day of
#   idle and changeover minutes (< 1000), so nothing but a real order or a hard
#   rule (S2 arcs, sate last) can buy a kapvorm break on L1. Ten arcs are
#   still 20k, far under the 100k one included order is worth.
L1_KAPVORM_BONUS = int(_os.environ.get("FRIGI_SEQ_L1_KAPVORM_BONUS", "2000"))


def kapvorm_bonus(lijn_nr: int | None) -> int:
    """G2 per same-kapvorm arc on this line — L1 outranks everything else."""
    return L1_KAPVORM_BONUS if lijn_nr == 1 else KAPVORM_BONUS


def arc_bonus(
    prev: tuple[Order, Product], nxt: tuple[Order, Product], lijn_nr: int | None = None,
) -> int:
    """G1 + G2 + G3 bonus for running `nxt` straight after `prev`."""
    (o1, p1), (o2, p2) = prev, nxt
    bonus = 0
    if p1.identiek_groep and p1.identiek_groep == p2.identiek_groep and o1.aantal <= o2.aantal:
        bonus += IDENTIEK_BONUS
    return bonus + carry_bonus(p1, p2, lijn_nr)


def carry_bonus(prev: Product | None, nxt: Product, lijn_nr: int | None = None) -> int:
    """G2 + G3: same kapvorm and/or same schaal as the product before it."""
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
    """G2 + G3 look-ahead: closing on a kapvorm / schaal the next day runs."""
    next_kap, next_sch = volgende
    bonus = 0
    kap = kapvorm_sleutel(last)
    if kap is not None and kap in next_kap:
        bonus += kapvorm_bonus(lijn_nr)
    sch = schaal_sleutel(last)
    if sch is not None and sch in next_sch:
        bonus += SCHAAL_BONUS
    return bonus


# Per-call safety cap, not a target — a single day's order count is small
# (a handful per line in practice), so real solves finish in well under a
# second; this only bounds the worst case. Now called once per committed-
# window day/line at the final render (see create_plan.py), not per feedback
# iteration, so total added time is roughly (committed days × 2 lines ×
# this cap) in the worst case.
_SEQUENCE_TIME_LIMIT = float(_os.environ.get("FRIGI_SEQUENCE_TIME_LIMIT", "2.0"))


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
    Search for a subset + sequence of `orders_products` that fits within
    [start_time_min, max_end_min] (minutes-of-day), maximizing how many
    orders are included.

    `blocking` carries the other line's claims on single-unit machines, as
    minute-of-day intervals per device. Without it the model optimizes against
    a window it believes is entirely free, and the greedy walker that verifies
    the result then has to stagger the proposal around the real device block
    and drop whatever no longer fits — the sequence was optimal only in a
    fiction. Passing it makes the two agree on the same day.

    `vorige` is the product this line closed its previous workday with; the
    day earns `carry_bonus` for opening on the same kapvorm / schaal.
    `volgende` is the look-ahead twin: the kapvorm / schaal keys this line runs
    on its next workday, and `close_bonus` for closing on one of them.

    Returns the chosen orders in production sequence, or None if there's
    nothing to gain (0-1 orders) or the solver found no useful result —
    callers should keep their existing result in that case.
    """
    n = len(orders_products)
    if n <= 1:
        return None

    products = [p for _, p in orders_products]
    durations = [calculate_duration(o, p) for o, p in orders_products]
    man = [personeel_for_lijn(p, lijn_nr) for p in products]

    # Real, asymmetric, sequence-dependent setup cost for every ordered pair.
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

    # An order-to-order arc simply doesn't exist if it would jump per-line
    # headcount up by more than _MAX_BEZETTING_DELTA — the solver can never
    # propose that transition at all, rather than being told about it after
    # the fact (matches check_bezetting_continuity's rule: only increases are
    # capped, a drop of any size is always fine, and a jump is allowed anyway
    # if the incoming order's own production period exceeds 4h).
    order_arc: dict[tuple[int, int], object] = {}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if man[j] - man[i] > _MAX_BEZETTING_DELTA and durations[j] <= 4 * 60:
                continue
            # A5: the only way out of a sate node is more sate or the depot
            # (planner 22/10: after sate nothing else can run on the line).
            # Removing the other arcs is "sate closes the day" stated in a
            # circuit model. Without it the search happily opens the day with
            # the peanut, which is legal on this line and shuts the other one
            # down from six in the morning.
            if SATE_EXCLUSIVE and is_sate(products[i]) and not is_sate(products[j]):
                continue
            # S2 requirements, hard (planner 15/10: "set in stone"). Nothing
            # but the depot or another order of the SAME article may run in
            # front of a "moet eerst" product, and nothing but the depot or the
            # same closing run (`sluit_samen`: same article, or sate behind
            # sate) may follow a "moet laatst" one (unless it also has to open —
            # the head claim wins, as in apply_positie_regels). Two different
            # products that must open can then never both be in the day: the
            # search keeps the one that fits best and skips the other.
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
    depot_in = [model.NewBoolVar(f"depot_in_{i}") for i in range(n)]
    for i in range(n):
        arcs.append((DEPOT, i, depot_out[i]))
        arcs.append((i, DEPOT, depot_in[i]))
    depot_skip = model.NewBoolVar("depot_skip")  # whole day empty
    arcs.append((DEPOT, DEPOT, depot_skip))

    model.AddCircuit(arcs)

    # ── S2: first / last of day ──
    # depot_out[i] means "i opens the day", depot_in[i] means "i closes it".
    # A prohibition is hard, so the search cannot hand back a sequence the
    # greedy walker would then have to undo. Requirements are hard too, by the
    # missing arcs above; the positie bonus below only prefers the depot arc
    # over a same-article predecessor.
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
    finish = [model.NewIntVar(start_time_min, max_end_min, f"fin_{i}") for i in range(n)]
    for i in range(n):
        model.Add(finish[i] == arrival[i] + durations[i])
        model.Add(finish[i] <= max_end_min).OnlyEnforceIf(skip[i].Not())
        # The day's opening order normally starts exactly at the line's start.
        # Once the other line holds a shared machine across that moment, the
        # walker's `stagger_around` pushes the opener later, so pinning it here
        # would make a schedulable day look infeasible. Relax to a lower bound
        # for the whole model as soon as any order is device-blocked; the walker
        # packs tight regardless and the caller re-verifies the result.
        if dev_blocked:
            model.Add(arrival[i] >= start_time_min).OnlyEnforceIf(depot_out[i])
        else:
            model.Add(arrival[i] == start_time_min).OnlyEnforceIf(depot_out[i])

    for (i, j), b in order_arc.items():
        model.Add(arrival[j] >= finish[i] + cost[i][j]).OnlyEnforceIf(b)

    # ── M1: the other line's hold on a single-unit machine ──
    # An order needing a device the other line already claimed must run wholly
    # before or wholly after each of that line's busy windows. This mirrors
    # `stagger_around` in the walker, except the solver can also place the
    # order EARLIER than the block instead of only being shoved past it.
    for i, intervals in dev_blocked.items():
        for busy_start, busy_end in intervals:
            before = model.NewBoolVar(f"dev_{i}_{busy_start}")
            model.Add(finish[i] <= busy_start).OnlyEnforceIf([before, skip[i].Not()])
            model.Add(arrival[i] >= busy_end).OnlyEnforceIf([before.Not(), skip[i].Not()])

    included = [skip[i].Not() for i in range(n)]
    total_cost = sum(b * cost[i][j] for (i, j), b in order_arc.items())
    adj_bonus = sum(
        b * arc_bonus(orders_products[i], orders_products[j], lijn_nr)
        for (i, j), b in order_arc.items()
    )

    # O6 — idle minutes: from the line start to the last order's end, whatever
    # is neither production nor changeover. Zero for a day packed from the
    # start; a gernal order shoved past the other line's block to 21:50 pays
    # for the whole hole.
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
    # Maximize included count first (weight dominates any possible cost sum),
    # then the day's shape: identical products, same kapvorm and same schaal
    # back to back, opening on what yesterday closed with, no idle holes,
    # least setup time. See the weights at the top.
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
    # Single worker + fixed seed: CP-SAT only guarantees a reproducible search
    # under these settings. With multiple parallel workers racing under a
    # tight time limit, which one reports first (and thus what gets returned)
    # can vary run to run for the exact same input — which let a day's
    # recovered order set differ between the feedback loop's internal oracle
    # check and the later `schedule_all_days` re-run, silently losing orders
    # that had already been confirmed to fit (see scheduler.py's
    # CAPACITY_DROP warning, which is what finally made this visible).
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
