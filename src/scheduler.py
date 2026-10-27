from __future__ import annotations

import os as _os
from datetime import date, datetime, time, timedelta
from typing import NamedTuple

from . import config
from .models import DaySchedule, Order, PlannedOrder, Product
from .constraints import (
    SATE_EXCLUSIVE,
    SATE_PARALLEL,
    WEEKDAG_MAP,
    calculate_duration,
    check_dag_toegelaten,
    check_kruiscontaminatie,
    get_earliest_start,
    get_mep_date,
    is_sate,
    overlaps_sate_window,
    personeel_for_lijn,
    resolve_lijn,
    sate_tail_index,
    sate_window,
    transition_cost,
)
from .constraints.staffing_rules import check_bezetting_continuity
from .constraints.device_rules import (
    blocked_devices,
    busy_intervals,
    scarce_devices_needed,
    stagger_around,
)
from .day_quality import line_quality, line_score
from .constraints.grouping_rules import GEEN_VOLGENDE, Volgende
from .constraints.sequence_rules import (
    apply_positie_regels,
    mag_eerst,
    mag_laatst,
    moet_eerst,
    moet_laatst,
    positie_ok,
    sate_last,
    sort_orders_for_lijn,
)

# ── helpers ──────────────────────────────────────────────────────────────────

def _positie_ok(planned: list[PlannedOrder]) -> bool:
    """S2 on a walked line-day — see `sequence_rules.positie_ok`."""
    return positie_ok([po.product for po in planned])


def _add_minutes(t: time, minutes: int) -> time:
    dt = datetime.combine(date.today(), t) + timedelta(minutes=minutes)
    return dt.time()


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute



# ── day KPIs ─────────────────────────────────────────────────────────────────

def werk_minuten(planned: list[PlannedOrder]) -> int:
    """
    Minutes of the day this line is actually WORKING: production plus the
    changeover in front of each order.

    This is deliberately not the elapsed span (`max(eind) - min(start)`), which
    is what this used to report. The span counts idle gaps as if they were
    load — including the gaps `_schedule_lijn` deliberately inserts when it
    staggers L2 around L1's use of a shared single-unit machine. A line that
    runs 90 minutes, waits four hours for the Leonard, then runs 90 more is
    150 minutes of work, not 390.

    That mattered beyond reporting: this number is handed between solver
    phases as `_reserved_capacity`, where the model's own constraint is
    `sum(duration + buffer) <= cap`. Feeding it a span meant reserving idle
    time as if it were booked, which could clamp a line's remaining capacity
    to zero while real room was still there.
    """
    return sum(po.ombouw_voor + _order_duur(po) for po in planned)


def _order_duur(po: PlannedOrder) -> int:
    d = _time_to_minutes(po.eind) - _time_to_minutes(po.start)
    return d if d >= 0 else d + 24 * 60   # defensive; schedule_day rejects these


def span_minuten(planned: list[PlannedOrder]) -> int:
    """First changeover start to last order end — how long the line is TIED UP."""
    if not planned:
        return 0
    start = min(_time_to_minutes(p.start) - p.ombouw_voor for p in planned)
    eind = max(_time_to_minutes(p.eind) for p in planned)
    return max(0, eind - start)


def day_kpis(
    datum: date,
    lijn1: list[PlannedOrder],
    lijn2: list[PlannedOrder],
    mep_items: list[dict],
    warnings: list[str] | None = None,
) -> dict:
    """
    One KPI block for a day, used by both schedule_day and replay_locked_days
    so a frozen day and a freshly-planned day are always measured identically.
    (They weren't: this divided by 16.5h in one and 16h in the other.)

    Utilization is work minutes over the line's real shift window for that day
    (config.window_minutes), so it can no longer exceed 100%.
    """
    weekdag = WEEKDAG_MAP.get(datum.weekday(), "")
    out: dict = {
        "datum": datum.isoformat(),
        "weekdag": weekdag,
        "mep_items": mep_items,
    }
    for lijn_nr, planned in ((1, lijn1), (2, lijn2)):
        werk = werk_minuten(planned)
        span = span_minuten(planned)
        window = config.window_minutes(datum, lijn_nr)
        pct = round(werk / window * 100, 1) if window else 0.0
        out[f"lijn{lijn_nr}_orders"] = len(planned)
        out[f"lijn{lijn_nr}_werk_min"] = werk
        out[f"lijn{lijn_nr}_span_min"] = span
        out[f"lijn{lijn_nr}_window_min"] = window
        out[f"lijn{lijn_nr}_bezetting_pct"] = pct
        if warnings is not None and pct > 90:
            warnings.append(
                f"Lijn {lijn_nr} bezetting {pct:.0f}% van het {window}min venster "
                f"> 90% - risico bij uitval"
            )
    return out


# ── core scheduler ───────────────────────────────────────────────────────────

def _schedule_lijn(
    orders_products: list[tuple[Order, Product]],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None = None,
    toestellen_avail: dict[str, int] | None = None,
    sate_cap: SateCap | None = None,
    _repair_ends: bool = True,
) -> list[PlannedOrder]:
    """
    Schedule orders for a line, respecting STRICT capacity limits.

    Key: NEVER schedule an order if it exceeds max_end time!
    If order doesn't fit, SKIP it (don't add warning, don't add to plan).

    Cross-line device staggering:
      `blocking` maps a scarce device -> list of (start_min, end_min) busy
      intervals already claimed by the OTHER line. If this order needs such a
      device during a busy interval, its start is pushed forward until the
      device is free (they run sequentially on the one shared machine instead of
      colliding). If pushing forward runs past max_end, the order is dropped.

    Cross-line sate exclusivity (A5):
      `sate_cap.free_end` is the last moment an ORDINARY order may finish
      here, because from then on the hall belongs to the day's sate. This
      line's own sate is bounded by `sate_cap.sate_end` instead. See SateCap.
    """
    # A5 — whatever route this sequence came in by (the fixed sort, a CP-SAT
    # proposal, a position-rule repair), sate closes the day. See sate_last.
    if _SATE_EXCLUSIVE:
        orders_products = sate_last(orders_products)

    planned: list[PlannedOrder] = []
    current_time = get_earliest_start(lijn_nr)
    prev_po: PlannedOrder | None = None
    skipped: list[str] = []

    max_end_min = _time_to_minutes(max_end)
    cap = sate_cap or _NO_SATE_CAP
    ordinary_end_min = min(max_end_min, _time_to_minutes(cap.free_end))         if cap.free_end is not None else max_end_min
    sate_end_min = min(max_end_min, _time_to_minutes(cap.sate_end))         if cap.sate_end is not None else max_end_min

    for order, product in orders_products:
        order_warnings: list[str] = []

        if prev_po is None:
            ombouw = 0
        else:
            allergie_daling = product.allergie_niveau < prev_po.product.allergie_niveau
            kruiscontam = check_kruiscontaminatie(prev_po.product, product)
            ombouw = transition_cost(prev_po.product, product, setup_matrix)

            if allergie_daling:
                order_warnings.append(
                    f"Allergie-daling niv.{prev_po.product.allergie_niveau}"
                    f"->{product.allergie_niveau}: reiniging {ombouw} min"
                )
            elif kruiscontam:
                order_warnings.append(
                    f"Kruiscontaminatie ({', '.join(kruiscontam)}): reiniging {ombouw} min"
                )

        start = _add_minutes(current_time, ombouw)
        duration = calculate_duration(order, product)
        eind = _add_minutes(start, duration)

        eind_min = _time_to_minutes(eind)
        start_min = _time_to_minutes(start)

        # ── M1: cross-line staggering on shared single-unit machines ──
        scarce_devs = blocked_devices(product, blocking, toestellen_avail)
        if scarce_devs:
            shifted = stagger_around(start_min, eind_min, scarce_devs, blocking)
            if shifted != start_min:
                start = _add_minutes(start, shifted - start_min)
                eind = _add_minutes(eind, shifted - start_min)
                start_min = _time_to_minutes(start)
                eind_min = _time_to_minutes(eind)
                order_warnings.append(
                    f"Gestaggerd voor gedeeld toestel ({', '.join(scarce_devs)}): "
                    f"start {start.strftime('%H:%M')}"
                )

        # STRICT CAPACITY CHECK: Orderde MOET passen binnen max_end
        # Check 1: End time after max_end (normal case)
        # Check 2: End time before start time = crossed midnight (NOT allowed)
        # Check 3: A5 — ordinary production stops when the day's sate starts;
        #          this line's sate stops where the other line's block begins.
        limit_min = sate_end_min if is_sate(product) else ordinary_end_min
        crosses_midnight = eind_min < start_min
        exceeds_max = eind_min > limit_min

        if exceeds_max or crosses_midnight:
            # REJECT ORDER - doesn't fit in time window or crosses midnight
            limit_t = _add_minutes(time(0, 0), limit_min)
            reason = "crosses midnight" if crosses_midnight else f"eindtijd {eind.strftime('%H:%M')} overschrijdt {limit_t.strftime('%H:%M')}"
            skipped.append(
                f"Order {order.order_nr} ({product.naam}, {order.aantal} st): {reason}"
            )
            continue  # ← SKIP this order entirely

        if eind_min > _time_to_minutes(time(14, 0)):
            order_warnings.append("Shift 2 actief")

        man_value = personeel_for_lijn(product, lijn_nr)

        po = PlannedOrder(
            order=order,
            product=product,
            lijn=lijn_nr,
            start=start,
            eind=eind,
            ombouw_voor=ombouw,
            warnings=order_warnings,
            man=man_value,  # Number of people needed for this task
        )

        # Check bezetting continuity
        if prev_po:
            bez_warning = check_bezetting_continuity(prev_po, po)
            if bez_warning:
                order_warnings.append(f"Bezetting: {bez_warning}")

        planned.append(po)
        prev_po = po
        current_time = eind

    return _repair_dag_uiteinden(
        planned, lijn_nr, datum, setup_matrix, max_end,
        blocking, toestellen_avail, sate_cap,
    ) if _repair_ends else planned


def _repair_dag_uiteinden(
    planned: list[PlannedOrder],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None,
    toestellen_avail: dict[str, int] | None,
    sate_cap: SateCap | None = None,
) -> list[PlannedOrder]:
    """
    S2, second pass — put the day's ends right AFTER the walk has dropped
    whatever did not fit.

    `sort_orders_for_lijn` already pinned the ends of the CANDIDATE list, but
    the walk above schedules a prefix of that list and drops the rest, so the
    order that actually closes the day is usually not the one that was pinned
    there. That is why the tail rule needs a second look and the head rule
    almost never does: the head is never the thing that gets dropped.

    Re-walks the survivors — a set already proven to fit — in an order whose
    ends are legal. The retry is accepted only if it keeps every one of them:
    the changeover costs shift when the sequence does, and losing a real order
    to satisfy a sequencing rule would be a bad trade. When no legal
    arrangement exists (every survivor barred from the end it needs), the
    original stands and the validator reports it, which is the honest outcome
    for a day that genuinely cannot be sequenced.
    """
    if len(planned) < 2:
        return planned
    if _positie_ok(planned):
        return planned

    survivors = [(po.order, po.product) for po in planned]
    reordered = apply_positie_regels(survivors)
    if all(a is b for a, b in zip(reordered, survivors)):
        return planned

    retry = _schedule_lijn(
        reordered, lijn_nr, datum, setup_matrix, max_end,
        blocking=blocking, toestellen_avail=toestellen_avail,
        sate_cap=sate_cap, _repair_ends=False,
    )
    if len(retry) != len(planned):
        return planned
    if not _positie_ok(retry):
        return planned
    for po in retry:
        po.warnings.append("Sequentie herschikt voor opstart/afsluit-regel")
    return retry


def _enforce_positie_regels(
    planned: list[PlannedOrder],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None,
    toestellen_avail: dict[str, int] | None,
    sate_cap: SateCap | None = None,
) -> tuple[list[PlannedOrder], list[Order]]:
    """
    S2 is absolute: a line-day may NEVER be handed to the floor opening or
    closing with a product barred from that position.

    `_repair_dag_uiteinden` above already tries to reorder the survivors, and
    gives up when that would cost an order — it used to leave the violation
    standing and let the validator report it. Measured on a real wk36 plan
    that produced 13 line-days the floor cannot run as written, and on 12 of
    them not one assigned order was allowed to close at all, so no reordering
    could ever have fixed it.

    This is the guarantee, so the trade flips: give up orders rather than the
    rule. Removes the smallest survivor at a time — the same "small orders die
    first" policy `sort_orders_for_lijn` already applies to an overfull day —
    until an arrangement exists whose ends are both legal, or the line is
    empty. Returns (kept, removed); the caller reports the removals and the
    feedback loop gets a chance to place them on another day.
    """
    if not planned:
        return planned, []
    if _positie_ok(planned):
        return planned, []

    def _removal_rank(candidate, survivors) -> tuple[int, int]:
        """
        Who to give up first. Smallest-within-tier, but the TIER comes first:

          0  takes neither end - pure burden on this day, and the reason the
             day is illegal in the first place
          1  can take an end, but someone else can too
          2  the ONLY survivor able to open (or close) the day

        Removing on size alone loses days it did not have to. Two equal-sized
        orders, one barred from both ends: the tie could drop the only order
        able to close, which leaves the barred one alone and forces a second
        removal - both orders lost where one would have survived. Same trap
        whenever the sole end-provider merely happens to be the smallest.
        """
        order, product = candidate
        opens, closes = mag_eerst(product), mag_laatst(product)
        # A "moet" claim another article on this line-day also makes: one of
        # the two has to go whatever else is removed, so go there first.
        if any(
            want(product) and want(q) and q.art_nr != product.art_nr
            for want in (moet_eerst, moet_laatst)
            for _, q in survivors
        ):
            return -1, order.aantal
        if not opens and not closes:
            tier = 0
        else:
            sole = (
                (opens and sum(1 for _, p in survivors if mag_eerst(p)) == 1)
                or (closes and sum(1 for _, p in survivors if mag_laatst(p)) == 1)
            )
            tier = 2 if sole else 1
        return tier, order.aantal

    survivors = [(po.order, po.product) for po in planned]

    # Ask the sequencer first. Its S2 prohibitions are hard — a product barred
    # from an end simply has no depot arc to it — so every sequence it can
    # return already has legal ends, and it maximizes how many orders are in
    # one. The removal loop below can only ever ask "does THIS subset happen to
    # arrange legally", one greedily-chosen subset at a time, which gives up
    # orders the search would have kept. Only reached on a line-day that is
    # already illegal, so this costs a solve on the days that were going to
    # lose an order anyway.
    solved = _positie_via_sequencer(
        survivors, lijn_nr, datum, setup_matrix, max_end, blocking, toestellen_avail,
        sate_cap,
    )
    if solved is not None and len(solved) == len(survivors):
        return solved, []

    removed: list[Order] = []
    kept_greedy: list[PlannedOrder] = []
    while survivors:
        # A legal day needs both an order allowed to open it and one allowed
        # to close it — with a single survivor that has to be the same order.
        if any(mag_laatst(p) for _, p in survivors) and any(mag_eerst(p) for _, p in survivors):
            retry = _schedule_lijn(
                apply_positie_regels(survivors), lijn_nr, datum, setup_matrix, max_end,
                blocking=blocking, toestellen_avail=toestellen_avail,
                sate_cap=sate_cap, _repair_ends=False,
            )
            if retry and _positie_ok(retry):
                kept = {id(po.order) for po in retry}
                removed += [o for o, _ in survivors if id(o) not in kept]
                kept_greedy = retry
                break
        victim = min(survivors, key=lambda op: _removal_rank(op, survivors))
        survivors.remove(victim)
        removed.append(victim[0])

    if solved is not None and len(solved) > len(kept_greedy):
        kept = {id(po.order) for po in solved}
        return solved, [po.order for po in planned if id(po.order) not in kept]
    return kept_greedy, removed


def _positie_via_sequencer(
    survivors: list[tuple[Order, Product]],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None,
    toestellen_avail: dict[str, int] | None,
    sate_cap: SateCap | None = None,
) -> list[PlannedOrder] | None:
    """
    The largest S2-legal arrangement of `survivors` the CP-SAT sequencer can
    find, re-verified through the real walker. None when it has nothing to
    offer — no solver, no solution, or a proposal the walker could not realize
    (a modelling gap can then only cost a recovery, never produce a bad day).
    """
    if not _OPTIMAL_SEQUENCE or not _S2_SEQUENCER_REPAIR or len(survivors) < 2:
        return None

    from .day_sequencer import solve_optimal_sequence

    proposal = solve_optimal_sequence(
        survivors, setup_matrix,
        _time_to_minutes(get_earliest_start(lijn_nr)), _time_to_minutes(max_end),
        lijn_nr, blocking=blocking, toestellen_avail=toestellen_avail,
    )
    if not proposal:
        return None

    walked = _schedule_lijn(
        list(proposal), lijn_nr, datum, setup_matrix, max_end,
        blocking=blocking, toestellen_avail=toestellen_avail,
        sate_cap=sate_cap, _repair_ends=False,
    )
    if len(walked) != len(proposal):
        return None
    if not _positie_ok(walked):
        return None
    for po in walked:
        po.warnings.append("Sequentie herschikt voor opstart/afsluit-regel")
    return walked


# When the fixed greedy sort drops an order, ask the CP-SAT sequencer
# (day_sequencer.py) whether SOME sequence of the same candidates fits
# instead. ON by default; set FRIGI_OPTIMAL_SEQUENCE=0 to get the pure greedy
# sort production originally used, for A/B comparison.
#
# NOTE this fires inside the feedback loop's oracle too (schedule_day ->
# _maybe_recover_drops), on exactly the days the oracle cares about — the
# ones that dropped something. _oracle_cache is what keeps that affordable.
_OPTIMAL_SEQUENCE = _os.environ.get("FRIGI_OPTIMAL_SEQUENCE", "1") == "1"

# Let the CP-SAT sequencer answer the position rule on a line-day whose ends
# came out illegal, instead of greedily removing the smallest order until some
# subset happens to arrange legally. Its S2 prohibitions are hard, so anything
# it returns already has legal ends, and it maximizes how many orders are in
# one. ON by default; set FRIGI_S2_SEQUENCER_REPAIR=0 for the removal loop
# alone.
_S2_SEQUENCER_REPAIR = _os.environ.get("FRIGI_S2_SEQUENCER_REPAIR", "1") == "1"


def _maybe_recover_drops(
    candidates: list[tuple[Order, Product]],
    greedy_result: list[PlannedOrder],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None = None,
    toestellen_avail: dict[str, int] | None = None,
    sate_cap: SateCap | None = None,
    force: bool = False,
    vorige: Product | None = None,
    volgende: Volgende = GEEN_VOLGENDE,
) -> list[PlannedOrder]:
    """
    Ask the CP-SAT sequencer (day_sequencer.py) for a better order and
    re-verify it through the real `_schedule_lijn` walker.

    By default only runs when the greedy sort/walk dropped something — this
    is the fast path used by every internal oracle/iteration call in
    scheduler_ortools_v3.py, which cannot afford a CP-SAT solve per line per
    day on top of its own search budget. Pass `force=True` to run it
    unconditionally (even when nothing was dropped) to also minimize total
    changeover time, not just recover drops — intended for the single final
    schedule_all_days call that produces the actual output (Excel/dashboard),
    not the search loop.

    Replaces the greedy result if the CP-SAT proposal fits MORE orders, or
    fits the SAME number with a better `day_quality.line_score` — identical
    products and same kapvorm back to back, fewer idle minutes, less ombouw,
    weighed exactly as the sequencer weighs them. Comparing on ombouw alone
    used to throw away every proposal that grouped or closed a gap at the cost
    of a few setup minutes. Can never make a day carry fewer orders.
    """
    if not _OPTIMAL_SEQUENCE:
        return greedy_result
    if not force and len(greedy_result) >= len(candidates):
        return greedy_result

    from .day_sequencer import solve_optimal_sequence

    start_min = _time_to_minutes(get_earliest_start(lijn_nr))
    end_min = _time_to_minutes(max_end)
    proposal = solve_optimal_sequence(
        candidates, setup_matrix, start_min, end_min, lijn_nr,
        blocking=blocking, toestellen_avail=toestellen_avail, vorige=vorige,
        volgende=volgende,
    )
    if proposal is None:
        return greedy_result

    retried = _schedule_lijn(
        proposal, lijn_nr, datum, setup_matrix, max_end,
        blocking=blocking, toestellen_avail=toestellen_avail,
        sate_cap=sate_cap,
    )
    better = (
        line_quality(retried, lijn_nr, vorige, volgende)
        > line_quality(greedy_result, lijn_nr, vorige, volgende)
    )
    if better:
        for po in retried:
            po.warnings.append("Sequentie herschikt door optimal-sequence solver")
        return retried
    return greedy_result


# Build the day both ways round on any day where the two lines compete for the
# same single-unit machine, and keep whichever fits more orders. ON by default;
# set FRIGI_SYMMETRIC_DEVICE_PRIORITY=0 for the old L1-always-first behaviour.
#
# The mirror wins on 1 to 4 of the 20 committed days (counted across runs from
# the "kreeg voorrang" day warning), because it only has something to offer
# when the second line loses MORE than the first line would. When it does win
# it is worth a whole order, and the check costs a second day build only on
# days where both lines actually want the same machine. Measured as part of the
# arm documented on FRIGI_S2_DISTINCT_ENDS in scheduler_ortools_v3.py; too
# small to separate from the rest of that arm on its own, so do not credit it
# with that arm's numbers.
_SYMMETRIC_DEVICE_PRIORITY = _os.environ.get("FRIGI_SYMMETRIC_DEVICE_PRIORITY", "1") == "1"


# ── A5: sate ('X') stops the other line ──────────────────────────────────────
#
# Peanut does not stay on its own line, so while sate runs the other line may
# not produce anything else. Enforced as a WINDOW, the way M1 is: the sate
# line names a stretch of the day, and the other line has to be finished by
# the time it opens.
#
# Which is why the sate block is slid towards closing time. The rule costs the
# other line exactly the length of that block wherever it sits — but put the
# block in the middle of the day and everything the other line still had to
# run after it is lost too, because a line that has stopped does not start
# again. Against the shift end it costs only the last hours, and the other
# line runs a full day up to them. That is the whole optimisation here, and it
# matches how the sheet already reads: 11 of the 15 sate products carry
# "afsluit moet".
#
# But only as late as the other line needs, and no later (planner, 02/10). When
# the other line runs out of work before closing time, the sate starts the
# moment it stops — sliding further would only leave both lines standing still
# (2026-10-02: L1 done at 13:05, sate window opening at 13:50). See
# `_handover_target`.
#
# Sate on BOTH lines (planner 22/10, second Gernal): the two blocks run side
# by side and open at the same moment — as late as the longer block allows,
# as early as both lines' ordinary work allows. See `_synchronise_sate`.
# With SATE_PARALLEL off they queue instead, one closing the day and the
# other running in front of it.
#
# The switch lives in constraints/allergen_rules.py, with the rule itself:
# the closing position and the sequencer's arcs hang off the same flag, so an
# A/B turns off all of A5 or none of it.
_SATE_EXCLUSIVE = SATE_EXCLUSIVE
_SATE_PARALLEL = SATE_PARALLEL


class SateCap(NamedTuple):
    """
    A5, the two bounds one line's day is built against.

    `free_end`  — last moment ORDINARY production may finish here, because
                  from then on the hall belongs to the day's sate.
    `sate_end`  — last moment THIS line's sate may finish. Closing time
                  normally (and always when blocks run side by side); in the
                  old queue, earlier when the other line's block closes the
                  day and this one has to run in front of it.

    None on either means unbounded — fall back to the shift end.
    """
    free_end: time | None = None
    sate_end: time | None = None


_NO_SATE_CAP = SateCap()


def _sate_start(planned: list[PlannedOrder]) -> time | None:
    """When this line's sate window opens, as a time. None if it runs no sate."""
    window = sate_window(planned)
    if window is None:
        return None
    return _add_minutes(time(0, 0), max(0, window[0]))


def _sate_cap_from(other: list[PlannedOrder]) -> SateCap | None:
    """
    A5 — the bounds one line inherits from the other line's finished day.

    Once the other line's sate opens, this line runs nothing ordinary. Its own
    sate may run beside it (SATE_PARALLEL); in the old queue it has to be done
    by then too. Used for the re-walks that happen after the day is built
    (S2), where the real window is known and there is no probe left to
    estimate from.
    """
    opens = _sate_start(other)
    if opens is None:
        return None
    return SateCap(free_end=opens, sate_end=None if _SATE_PARALLEL else opens)


def _align_sate_tail(
    planned: list[PlannedOrder],
    target_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None = None,
    toestellen_avail: dict[str, int] | None = None,
    allow_earlier: bool = False,
) -> list[PlannedOrder]:
    """
    A5 — slide the day's closing sate block up against `target_end`, so the
    window it costs the other line is the last stretch of the day rather than
    a hole in the middle of it.

    `allow_earlier` lets the block move BACK towards `target_end` as well —
    the handover, where the sate starts the moment the other line is done
    (see `_handover_target`). It never moves in front of the order that
    precedes it on its own line, nor before the line start.

    `target_end` is closing time for the line whose sate closes the day, and
    the start of THAT line's window for the other one: the two blocks queue up
    behind each other, since the hall stops for either of them.

    Only a TRAILING block moves. With something scheduled behind it — a
    product the sheet says must close the day — there is nowhere to slide to,
    and the block stays where the walk put it. The other line is still barred
    from it: the window is where the sate is, not where we would have liked it.

    Refuses to move a block onto a single-unit machine the other line is
    holding (M1).
    """
    i = sate_tail_index(planned)
    if i is None:
        return planned
    shift = _time_to_minutes(target_end) - _time_to_minutes(planned[-1].eind)
    if shift < 0 and allow_earlier:
        window_start = _time_to_minutes(planned[i].start) - planned[i].ombouw_voor
        floor = (
            _time_to_minutes(planned[i - 1].eind) if i > 0
            else _time_to_minutes(get_earliest_start(planned[i].lijn))
        )
        shift = max(shift, -max(0, window_start - floor))
    if shift == 0 or (shift < 0 and not allow_earlier):
        return planned

    if blocking:
        for po in planned[i:]:
            devs = blocked_devices(po.product, blocking, toestellen_avail)
            start_min = _time_to_minutes(po.start) + shift
            eind_min = _time_to_minutes(po.eind) + shift
            if stagger_around(start_min, eind_min, devs, blocking) != start_min:
                return planned

    for po in planned[i:]:
        po.start = _add_minutes(po.start, shift)
        po.eind = _add_minutes(po.eind, shift)
    opent = _sate_start(planned)
    if shift < 0:
        planned[i].warnings.append(
            f"Sate vervroegd naar {planned[i].start.strftime('%H:%M')} ({shift} min) — "
            f"start direct na de andere lijn"
        )
    else:
        planned[i].warnings.append(
            f"Sate naar {target_end.strftime('%H:%M')} geschoven (+{shift} min) — "
            f"de rest van de fabriek kan tot {opent.strftime('%H:%M')} doorwerken"
        )
    return planned


def _handover_target(
    closer: list[PlannedOrder],
    other: list[PlannedOrder],
    max_end: time,
) -> time:
    """
    A5 — where the closing sate block should END so its window opens exactly
    when the other line's last order is done: never later than closing time,
    never earlier than where its own line's walk already put it.

    With nothing on the other line there is nobody to wait for, so the block
    stays where its own walk put it.
    """
    i = sate_tail_index(closer)
    if i is None:
        return max_end
    current_end = _time_to_minutes(closer[-1].eind)
    if not other:
        return closer[-1].eind
    window_len = current_end - (_time_to_minutes(closer[i].start) - closer[i].ombouw_voor)
    other_end = max(_time_to_minutes(po.eind) for po in other)
    target = min(_time_to_minutes(max_end), other_end + window_len)
    return _add_minutes(time(0, 0), target)


def _sate_probe(
    sorted_orders: list[tuple[Order, Product]],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
) -> int:
    """
    A5 — how many minutes at the END of the day this line's sate takes away
    from the other one, worked out before either line is built, because both
    lines' deadlines depend on it. Zero when the line runs no sate.

    The block's own length, changeover in front included: sate always closes
    its line (sate_last), so the block can always be slid against closing
    time and the other line loses exactly that. This used to be measured from
    where the probe's window opened to closing time — wherever the walk had
    put it — which on a line with little in front of its sate charged the
    other line nearly the whole day: sate alone on L2, six orders on L1, and
    L1 came out empty while the sate ran 20:30-22:30.

    A throwaway build of this line alone: no cross-line blocking (nothing has
    been placed yet) and no CP-SAT recovery pass (a probe is not worth a
    solve). The real build that follows can still come out slightly different
    — one order fewer changes the changeover in front of the block — so
    schedule_day re-measures the finished day and trims whatever the estimate
    missed.
    """
    if not any(is_sate(p) for _, p in sorted_orders):
        return 0
    probe = _schedule_lijn(sorted_orders, lijn_nr, datum, setup_matrix, max_end)
    window = sate_window(probe)
    if window is None:
        return 0                       # every sate order got dropped
    return max(0, window[1] - window[0])


def _trim_sate_conflicts(
    planned: list[PlannedOrder],
    other: list[PlannedOrder],
) -> tuple[list[PlannedOrder], list[Order]]:
    """
    A5, last word — remove anything on this line still running inside the
    other line's sate window (`overlaps_sate_window`): everything ordinary,
    and in the old queue this line's own sate too.

    The window each line was BUILT against is an estimate from `_sate_probe`;
    this is measured on the finished day, so it is the one that decides. In
    practice it only ever reaches the tail — that is where the window is —
    which leaves the surviving sequence and its changeovers exactly as built.

    Direction matters only in the old queue: there the caller trims the line
    whose sate does NOT close the day against the line whose sate does, since
    run symmetrically on two blocks that overlap this would delete both. Side
    by side, sate never trims sate, so the order is irrelevant.
    """
    window = sate_window(other)
    if window is None:
        return planned, []
    kept, removed = [], []
    for po in planned:
        if overlaps_sate_window(po, window):
            removed.append(po.order)
        else:
            kept.append(po)
    return kept, removed


def _sate_floor(planned: list[PlannedOrder]) -> int | None:
    """
    A5 — the earliest minute this line's closing sate window could open: when
    the order in front of it ends, or the line start. None without a sate tail.
    """
    i = sate_tail_index(planned)
    if i is None:
        return None
    if i > 0:
        return _time_to_minutes(planned[i - 1].eind)
    return _time_to_minutes(get_earliest_start(planned[i].lijn))


def _window_len(planned: list[PlannedOrder]) -> int:
    window = sate_window(planned)
    return 0 if window is None else window[1] - window[0]


def _synchronise_sate(
    by_line: dict[int, list[PlannedOrder]],
    max_end: time,
    toestellen: dict[str, int] | None,
) -> dict[int, list[PlannedOrder]]:
    """
    A5, sate on both lines (SATE_PARALLEL) — open both windows at the SAME
    minute (planner 22/10: "beide satéproducties moeten ongeveer gelijktijdig
    opstarten"). Both changeovers start together, so the hall closes once and
    nobody waits; the two first packs come off within the difference of the
    two changeovers.

    That minute is the handover, applied to both lines at once: as soon as
    BOTH lines are done with ordinary work (any later only leaves both
    standing), but never so late that the longer block runs past closing
    time. Each line's ordinary work was built against that last bound, so the
    two normally agree.

    Each slide is checked against the other line's single-unit machines (M1)
    as they stand at that moment — for the line built first as well, since
    neither block is where the build left it any more. A slide that would
    collide is not made; the windows then open apart, the validator's sync
    check says so, and the trim in schedule_day keeps the day legal.
    """
    floors = {n: _sate_floor(p) for n, p in by_line.items()}
    if None in floors.values():
        return by_line
    latest = _time_to_minutes(max_end) - max(_window_len(p) for p in by_line.values())
    target = min(max(floors.values()), latest)
    out = dict(by_line)
    for n in (1, 2):
        other = 2 if n == 1 else 1
        block = busy_intervals(out[other], toestellen) if toestellen else None
        out[n] = _align_sate_tail(
            out[n], _add_minutes(time(0, 0), target + _window_len(out[n])),
            block, toestellen, allow_earlier=True,
        )
    return out


def _contended_devices(
    lijn1: list[tuple[Order, Product]],
    lijn2: list[tuple[Order, Product]],
    toestellen: dict[str, int] | None,
) -> set[str]:
    """
    Single-unit machines BOTH lines want today. Empty means the line order is
    irrelevant — nothing staggers — and the mirror build would be wasted work.
    """
    if not toestellen or not lijn1 or not lijn2:
        return set()
    need1 = {d for _, p in lijn1 for d in scarce_devices_needed(p, toestellen)}
    need2 = {d for _, p in lijn2 for d in scarce_devices_needed(p, toestellen)}
    return need1 & need2


def _build_line_pair(
    first: int,
    orders_by_line: dict[int, list[tuple[Order, Product]]],
    sorted_by_line: dict[int, list[tuple[Order, Product]]],
    datum: date,
    setup_matrix: dict,
    max_end: time,
    toestellen: dict[str, int] | None,
    force_optimal_sequence: bool,
    vorige: dict[int, Product] | None = None,
    volgende: dict[int, Volgende] | None = None,
) -> dict[int, tuple[list[PlannedOrder], dict[str, list[tuple[int, int]]] | None]]:
    """
    Build both lines with `first` claiming the shared machines and the other
    line staggering around it (M1).

    Returns, per line, its planned orders AND the blocking it was built
    against — the first line's is None, since nothing was holding a machine
    when it was placed. Later passes that re-walk a line need that same
    blocking; giving a line its own busy intervals makes `stagger_around`
    push every order past its own earlier claim on the machine, which is how
    a perfectly schedulable line ends up looking impossible.

    A5 runs alongside M1 here, and it is the reason the day is built back to
    front. The hall stops for sate on either line, so each line's ordinary
    work is bounded by the OTHER line's window and by nothing else — where its
    own sate sits is a sequencing question its own walk already answers. With
    sate on both lines the two blocks run side by side and open together
    (SATE_PARALLEL; `_synchronise_sate`), so the first line loses the longer
    block; in the old queue `first`'s block closes the day and `second`'s runs
    in front of it, so it loses both. Both lengths are probed up front because
    the first line is built before the second one exists.
    """
    second = 2 if first == 1 else 1
    caps: dict[int, SateCap | None] = {first: None, second: None}
    both_sate = False
    if _SATE_EXCLUSIVE:
        # Both blocks are probed because the FIRST line is built before the
        # second one exists, and what bounds it is where the SECOND line's
        # block will open.
        span_first = _sate_probe(sorted_by_line[first], first, datum, setup_matrix, max_end)
        span_second = _sate_probe(sorted_by_line[second], second, datum, setup_matrix, max_end)
        both_sate = bool(span_first and span_second)
        if span_second:
            lost = (
                max(span_first, span_second) if _SATE_PARALLEL
                else span_first + span_second
            )
            caps[first] = SateCap(free_end=_add_minutes(max_end, -lost))

    planned_first = _schedule_lijn(
        sorted_by_line[first], first, datum, setup_matrix, max_end,
        sate_cap=caps[first],
    )
    vorige = vorige or {}
    volgende = volgende or {}
    planned_first = _maybe_recover_drops(
        orders_by_line[first], planned_first, first, datum, setup_matrix, max_end,
        sate_cap=caps[first], force=force_optimal_sequence,
        vorige=vorige.get(first), volgende=volgende.get(first, GEEN_VOLGENDE),
    )
    if _SATE_EXCLUSIVE:
        if both_sate and _SATE_PARALLEL:
            # Opened where the longer block has to open, which is where the
            # first line's ordinary work was capped; `_synchronise_sate` below
            # moves both blocks to their real common opening.
            planned_first = _align_sate_tail(
                planned_first,
                _add_minutes(caps[first].free_end, _window_len(planned_first)),
                allow_earlier=True,
            )
        else:
            planned_first = _align_sate_tail(planned_first, max_end)
        # The second line is bounded by what the first line actually claimed,
        # not by the probe's estimate of it. Nothing ordinary over there may
        # run past the moment this window opens — and in the old queue, that
        # line's own sate neither.
        caps[second] = _sate_cap_from(planned_first)

    blocking = busy_intervals(planned_first, toestellen) if toestellen else None

    planned_second = _schedule_lijn(
        sorted_by_line[second], second, datum, setup_matrix, max_end,
        blocking=blocking, toestellen_avail=toestellen, sate_cap=caps[second],
    )
    planned_second = _maybe_recover_drops(
        orders_by_line[second], planned_second, second, datum, setup_matrix, max_end,
        blocking=blocking, toestellen_avail=toestellen, sate_cap=caps[second],
        force=force_optimal_sequence, vorige=vorige.get(second),
        volgende=volgende.get(second, GEEN_VOLGENDE),
    )
    if _SATE_EXCLUSIVE:
        if both_sate and _SATE_PARALLEL:
            synced = _synchronise_sate(
                {first: planned_first, second: planned_second}, max_end, toestellen,
            )
            planned_first, planned_second = synced[first], synced[second]
            # The first line's block moved; later re-walks of the second line
            # have to stagger around where it is now.
            blocking = busy_intervals(planned_first, toestellen) if toestellen else None
        else:
            # Against closing time, or against the first line's block when
            # there is one to queue behind.
            cap2 = caps[second]
            planned_second = _align_sate_tail(
                planned_second,
                cap2.sate_end if cap2 and cap2.sate_end else max_end,
                blocking, toestellen,
            )
    return {first: (planned_first, None), second: (planned_second, blocking)}


# Tell a drop caused by the other line's hold on a shared machine apart from a
# drop caused by the day simply being full, and hand the pair to the solver.
# ON by default; set FRIGI_DEVICE_DROP_CUTS=0 to report every drop as capacity,
# which is what the plan did before. See docs/toestel-cut-gap.md.
#
# Week 36, full warm-up chain, --reset, 2 runs per arm, whole-horizon totals,
# measured on top of everything else already shipped:
#
#                     scheduled   real rejections   ORACLE-DROPPED   rejected units
#     off               425, 427       11, 12            3, 3          481k, 499k
#     on                427, 428        8,  7            0, 0          389k, 439k
#
# ORACLE-DROPPED goes to zero in both measured runs (a third run with the flag
# on landed on 1, so the bucket is largely gone rather than provably always
# empty), and unlike the earlier S2/phase
# work this one also moves VOLUME: rejected units drop 60-110k with no overlap
# between the arms. That fits what the bucket means — "capacity looked
# available in the proxy model but wasn't once real changeovers applied" was
# largely the machine, and the solver could not learn it because the reason
# never survived the day builder. NEVER-PLACED is unchanged (2-3 either way);
# ombouw is ~1% higher. The `toestelconflicten` counter goes from a flat 0 to
# firing every iteration, which is the mechanism finally having evidence.
_DEVICE_DROP_CUTS = _os.environ.get("FRIGI_DEVICE_DROP_CUTS", "1") == "1"


def _device_caused_drops(
    candidates: list[tuple[Order, Product]],
    planned: list[PlannedOrder],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None,
    toestellen: dict[str, int] | None,
    force_optimal_sequence: bool,
) -> list[tuple[Order, int, list[str]]]:
    """
    Which of this line's drops are the shared machine's fault, and which
    machine's.

    The test is the counterfactual, not a guess from the order's device list:
    build the same line again with the other line's claim REMOVED and see who
    survives that but not the real build. An order that dies either way died of
    capacity, and saying otherwise would forbid a pairing that is in fact
    perfectly schedulable — the failure mode this whole idea has to avoid.

    Only reached when the real build actually lost something on a line that was
    staggered at all, so the extra build is paid on the days that already have
    a problem.
    """
    if not _DEVICE_DROP_CUTS or not blocking or not toestellen:
        return []
    survived = {id(po.order) for po in planned}
    lost = [(o, p) for o, p in candidates if id(o) not in survived]
    if not lost:
        return []
    # Nothing to explain unless a lost order actually wanted a blocked machine.
    if not any(blocked_devices(p, blocking, toestellen) for _, p in lost):
        return []

    unblocked = _schedule_lijn(
        sort_orders_for_lijn(candidates), lijn_nr, datum, setup_matrix, max_end
    )
    unblocked = _maybe_recover_drops(
        candidates, unblocked, lijn_nr, datum, setup_matrix, max_end,
        force=force_optimal_sequence,
    )
    fits_without_block = {id(po.order) for po in unblocked}

    out: list[tuple[Order, int, list[str]]] = []
    for order, product in lost:
        if id(order) not in fits_without_block:
            continue                      # would have died anyway: capacity
        devs = blocked_devices(product, blocking, toestellen)
        if devs:
            out.append((order, lijn_nr, sorted(devs)))
    return out


def _sate_caused_drops(
    candidates: list[tuple[Order, Product]],
    planned: list[PlannedOrder],
    lijn_nr: int,
    datum: date,
    setup_matrix: dict,
    max_end: time,
    blocking: dict[str, list[tuple[int, int]]] | None,
    toestellen: dict[str, int] | None,
    sate_cap: SateCap | None,
    force_optimal_sequence: bool,
) -> list[Order]:
    """
    A5 — which of this line's drops the other line's sate cost it.

    Same counterfactual `_device_caused_drops` uses, for the same reason: walk
    the line again with the cut-off removed and see who survives that but not
    the real build. An order that dies either way ran out of day, and calling
    it a sate drop would send the planner looking for a sate-free day it never
    needed.

    Most sate drops arrive here rather than through `_trim_sate_conflicts` —
    the walker refuses them at the cut-off, long before there is a timeline to
    trim — and without this they read as ordinary CAPACITY_DROP.
    """
    if sate_cap is None or sate_cap.free_end is None:
        return []
    if _time_to_minutes(sate_cap.free_end) >= _time_to_minutes(max_end):
        return []
    survived = {id(po.order) for po in planned}
    lost = [(o, p) for o, p in candidates if id(o) not in survived]
    if not lost:
        return []

    uncapped = _schedule_lijn(
        sort_orders_for_lijn(candidates), lijn_nr, datum, setup_matrix, max_end,
        blocking=blocking, toestellen_avail=toestellen,
    )
    uncapped = _maybe_recover_drops(
        candidates, uncapped, lijn_nr, datum, setup_matrix, max_end,
        blocking=blocking, toestellen_avail=toestellen, force=force_optimal_sequence,
    )
    fits_without_cap = {id(po.order) for po in uncapped}
    return [o for o, _ in lost if id(o) in fits_without_cap]


def _pair_quality(
    built: dict[int, tuple[list[PlannedOrder], dict | None]],
    vorige: dict[int, Product] | None = None,
    volgende: dict[int, Volgende] | None = None,
) -> tuple[int, int]:
    """
    How good a whole day is: orders planned first, then both lines'
    `day_quality.line_score` (grouping, idle time, changeover). Strictly
    ordered, so the mirror build replaces the incumbent only when it is
    genuinely better and a tie always keeps L1 first.
    """
    return (
        len(built[1][0]) + len(built[2][0]),
        line_score(built[1][0], 1, (vorige or {}).get(1),
                   (volgende or {}).get(1, GEEN_VOLGENDE))
        + line_score(built[2][0], 2, (vorige or {}).get(2),
                     (volgende or {}).get(2, GEEN_VOLGENDE)),
    )


def schedule_day(
    orders: list[Order],
    products: dict[str, Product],
    setup_matrix: dict,
    datum: date,
    toestellen_beschikbaarheid: dict[str, int] | None = None,
    force_optimal_sequence: bool = False,
    vorige: dict[int, Product] | None = None,
    volgende: dict[int, Volgende] | None = None,
) -> DaySchedule:
    """
    Fase 1 heuristische scheduler.

    Stappen:
      1. Filter orders op productie_datum == datum
      2. Lijn-toewijzing (alleen_l1/l2 -> hard; prio_l1/l2 -> voorkeur; anders load-balance)
      3. Per lijn: groepeer op kapvorm en schaal, sorteer op allergie_niveau oplopend
      4. Tijdsberekening met ombouwtijden

    `vorige` maps a line to the product it closed its previous workday with;
    the sequencer prefers opening on the same kapvorm / schaal (carry).
    `volgende` maps a line to the kapvorm / schaal keys it runs on its next
    workday; the day prefers closing on one of them (look-ahead).
      5. Shift-check (14:00 = shift 2, 22:00/20:00 vr = max)
      6. MEP-generatie
      7. Validatie via validator.validate_schedule
    """
    warnings: list[str] = []
    weekdag = WEEKDAG_MAP.get(datum.weekday(), "")
    max_end = config.shift_end(datum)

    # 1. Filter
    day_orders = [o for o in orders if o.productie_datum == datum]

    # 2. Lijn-toewijzing
    lijn1: list[tuple[Order, Product]] = []
    lijn2: list[tuple[Order, Product]] = []
    unknown_products: list[str] = []

    for order in day_orders:
        product = products.get(order.art_nr)
        if product is None:
            unknown_products.append(order.art_nr)
            warnings.append(f"Order {order.order_nr}: product {order.art_nr} niet gevonden — overgeslagen")
            continue

        # D1 is a HARD rule, so this REFUSES rather than warns. It used to only
        # append a warning here while validator raised an ERROR nobody acted
        # on — meaning the only thing actually preventing an illegal day was
        # the solver's slot filter, and anything that bypassed the solver
        # (a hand-set date, a changed spreadsheet) could render a plan that
        # violates it. Now a forbidden day is refused wherever the order
        # came from.
        dag_err = check_dag_toegelaten(product, datum)
        if dag_err:
            warnings.append(
                f"[ERROR] DAG_RESTRICTIE_DROP: Order {order.order_nr}: {dag_err} "
                f"— geweigerd, niet ingepland"
            )
            continue

        # L1/L2/L3 — see constraints/line_rules.resolve_lijn. An explicit
        # 'kan_niet' beats every other signal including the line the optimizer
        # already picked.
        lijn = resolve_lijn(
            order, product,
            lijn1_minutes=sum(calculate_duration(o, p) for o, p in lijn1),
            lijn2_minutes=sum(calculate_duration(o, p) for o, p in lijn2),
        )
        (lijn1 if lijn == 1 else lijn2).append((order, product))

    # 3. Sortering per lijn
    volgende = volgende or {}
    lijn1_sorted = sort_orders_for_lijn(
        lijn1, (vorige or {}).get(1), volgende.get(1, GEEN_VOLGENDE))
    lijn2_sorted = sort_orders_for_lijn(
        lijn2, (vorige or {}).get(2), volgende.get(2, GEEN_VOLGENDE))

    # 4+5. Tijdsberekening per lijn
    # One line claims the shared single-unit machines; the other staggers
    # around it (M1), so the two can never hold one machine at the same time.
    # Which line goes first is not a fact about the floor — it decides which
    # line absorbs the whole cost of sharing, so on a day where both lines
    # want the same machine, both orderings are built and the better day wins.
    sorted_by_line = {1: lijn1_sorted, 2: lijn2_sorted}
    orders_by_line = {1: lijn1, 2: lijn2}
    built = _build_line_pair(
        1, orders_by_line, sorted_by_line, datum, setup_matrix, max_end,
        toestellen_beschikbaarheid, force_optimal_sequence, vorige, volgende,
    )
    if _SYMMETRIC_DEVICE_PRIORITY and _contended_devices(
        lijn1, lijn2, toestellen_beschikbaarheid
    ):
        mirror = _build_line_pair(
            2, orders_by_line, sorted_by_line, datum, setup_matrix, max_end,
            toestellen_beschikbaarheid, force_optimal_sequence, vorige, volgende,
        )
        if _pair_quality(mirror, vorige, volgende) > _pair_quality(built, vorige, volgende):
            gained = len(mirror[1][0] + mirror[2][0]) - len(built[1][0] + built[2][0])
            built = mirror
            warnings.append(
                f"L2 kreeg voorrang op de gedeelde toestellen "
                f"({', '.join(sorted(_contended_devices(lijn1, lijn2, toestellen_beschikbaarheid)))}) "
                f"— {gained:+d} order(s) t.o.v. L1 eerst"
            )

    (planned_lijn1, blocking1), (planned_lijn2, blocking2) = built[1], built[2]

    # M1, the part the plan used to throw away. Whichever line was built second
    # is the one that had to stagger, so it is the only one whose drops can be
    # the machine's fault. Established here, while the blocking that caused
    # them is still in hand — after this the drops are recovered by set
    # difference and every one of them looks like plain capacity.
    device_dropped = (
        _device_caused_drops(
            orders_by_line[1], planned_lijn1, 1, datum, setup_matrix, max_end,
            blocking1, toestellen_beschikbaarheid, force_optimal_sequence,
        )
        + _device_caused_drops(
            orders_by_line[2], planned_lijn2, 2, datum, setup_matrix, max_end,
            blocking2, toestellen_beschikbaarheid, force_optimal_sequence,
        )
    )

    # A5, measured on the finished day rather than probed from it: whatever is
    # still running inside the other line's sate window comes off, and the
    # windows the lines are re-walked against below are the real ones. Before
    # S2, so the position rule gets the final survivor set and its guarantee
    # about the day's ends still holds.
    #
    # The line whose sate ENDS LAST is the day's closer and keeps its block;
    # the other line is trimmed against it, then the closer is trimmed against
    # whatever the other line has left. In the old queue, trimming both
    # against each other at once would delete two overlapping sate blocks
    # instead of queueing them; side by side, sate never trims sate.
    sate_removed: dict[int, list[Order]] = {1: [], 2: []}
    closer, other = 1, 2
    if _SATE_EXCLUSIVE:
        by_line = {1: planned_lijn1, 2: planned_lijn2}
        ends = {n: (sate_window(p) or (0, 0))[1] for n, p in by_line.items()}
        closer = 1 if ends[1] >= ends[2] else 2
        other = 2 if closer == 1 else 1
        by_line[other], sate_removed[other] = _trim_sate_conflicts(
            by_line[other], by_line[closer]
        )
        by_line[closer], sate_removed[closer] = _trim_sate_conflicts(
            by_line[closer], by_line[other]
        )
        planned_lijn1, planned_lijn2 = by_line[1], by_line[2]
    sate_removed1, sate_removed2 = sate_removed[1], sate_removed[2]
    sate_cap1 = _sate_cap_from(planned_lijn2) if _SATE_EXCLUSIVE else None
    sate_cap2 = _sate_cap_from(planned_lijn1) if _SATE_EXCLUSIVE else None

    # S2 is not negotiable — see _enforce_positie_regels. Runs last, after the
    # recovery pass, because that is the only point where the day's real ends
    # are known. Each line is re-walked against the blocking IT was built with:
    # handing a line its own busy intervals would make it stagger around
    # itself and refuse a sequence that fits perfectly well.
    planned_lijn1, s2_removed1 = _enforce_positie_regels(
        planned_lijn1, 1, datum, setup_matrix, max_end,
        blocking1, toestellen_beschikbaarheid, sate_cap1,
    )
    planned_lijn2, s2_removed2 = _enforce_positie_regels(
        planned_lijn2, 2, datum, setup_matrix, max_end,
        blocking2, toestellen_beschikbaarheid, sate_cap2,
    )
    if _SATE_EXCLUSIVE:
        # A re-walk above rebuilds the timeline from the morning, so a sate
        # block it touched is no longer against the end it was placed at. Put
        # the queue back: the closer against closing time, the other line's
        # block against where that one opens. Sliding can only move a window
        # later, never earlier, so it cannot cost anything already given away.
        #
        # One exception, the handover: when only the closer carries sate, its
        # block ends where the other line's last order lets it start — moved
        # EARLIER when that line stopped before closing time. Nothing on the
        # other line runs after that moment, so nothing can overlap it.
        #
        # Sate on both lines, side by side: both blocks back to one common
        # opening (`_synchronise_sate`), then the same trims.
        by_line = {1: planned_lijn1, 2: planned_lijn2}
        blocking_of = {1: blocking1, 2: blocking2}
        both_sate = all(sate_window(p) is not None for p in by_line.values())
        if both_sate and _SATE_PARALLEL:
            by_line = _synchronise_sate(by_line, max_end, toestellen_beschikbaarheid)
        elif sate_window(by_line[other]) is None:
            by_line[closer] = _align_sate_tail(
                by_line[closer],
                _handover_target(by_line[closer], by_line[other], max_end),
                blocking_of[closer], toestellen_beschikbaarheid, allow_earlier=True,
            )
        else:
            by_line[closer] = _align_sate_tail(
                by_line[closer], max_end, blocking_of[closer], toestellen_beschikbaarheid
            )
            opens = _sate_start(by_line[closer])
            if opens is not None:
                by_line[other] = _align_sate_tail(
                    by_line[other], opens, blocking_of[other], toestellen_beschikbaarheid
                )
        by_line[other], extra_other = _trim_sate_conflicts(by_line[other], by_line[closer])
        by_line[closer], extra_closer = _trim_sate_conflicts(by_line[closer], by_line[other])
        sate_removed[other] += extra_other
        sate_removed[closer] += extra_closer
        planned_lijn1, planned_lijn2 = by_line[1], by_line[2]
        sate_removed1, sate_removed2 = sate_removed[1], sate_removed[2]

    if _SATE_EXCLUSIVE:
        sate_removed1 += _sate_caused_drops(
            orders_by_line[1], planned_lijn1, 1, datum, setup_matrix, max_end,
            blocking1, toestellen_beschikbaarheid, sate_cap1, force_optimal_sequence,
        )
        sate_removed2 += _sate_caused_drops(
            orders_by_line[2], planned_lijn2, 2, datum, setup_matrix, max_end,
            blocking2, toestellen_beschikbaarheid, sate_cap2, force_optimal_sequence,
        )

    all_planned = planned_lijn1 + planned_lijn2

    # _schedule_lijn silently drops any candidate that doesn't fit before
    # max_end (capacity overflow, midnight-cross, or a device-stagger push
    # past close) — it never raises or returns those drops. Without this
    # check they vanish from the plan entirely: not scheduled, not rejected,
    # not visible anywhere (Excel, dashboard) — real demand quietly lost.
    planned_order_ids = {id(po.order) for po in all_planned}
    s2_removed_ids = {id(o) for o in s2_removed1 + s2_removed2}
    sate_dropped: list[tuple[Order, int]] = []
    sate_removed_ids: set[int] = set()
    for lijn_nr, removed in ((1, sate_removed1), (2, sate_removed2)):
        for o in removed:
            if id(o) in planned_order_ids or id(o) in sate_removed_ids:
                continue
            sate_dropped.append((o, lijn_nr))
            sate_removed_ids.add(id(o))
    # The S2 pass runs after the device check and can remove an order the
    # device check had already claimed; S2 is the later and more specific
    # verdict, so it wins and the device entry is dropped.
    device_dropped = [
        entry for entry in device_dropped
        if id(entry[0]) not in planned_order_ids
        and id(entry[0]) not in s2_removed_ids
        and id(entry[0]) not in sate_removed_ids
    ]
    device_dropped_devs = {id(o): devs for o, _lijn, devs in device_dropped}
    dropped: list[Order] = []
    positie_dropped: list[Order] = []
    for order, product in lijn1 + lijn2:
        if id(order) in planned_order_ids:
            continue
        dropped.append(order)
        if id(order) in s2_removed_ids:
            positie_dropped.append(order)
            # Not a capacity problem — it fit, but keeping it would have left
            # the day opening or closing on a product barred from that end.
            warnings.append(
                f"[ERROR] POSITIE_REGEL_DROP: Order {order.order_nr} ({product.naam}, "
                f"{order.aantal} st) verwijderd van {datum}: geen enkele volgorde van "
                f"deze dag laat een toegelaten product openen en afsluiten."
            )
            continue
        if id(order) in sate_removed_ids:
            # A5. Not a capacity problem either: the day had the minutes, but
            # the other line was running peanut and the hall was closed.
            sate_lijn = next(l for o, l in sate_dropped if id(o) == id(order))
            other = 2 if sate_lijn == 1 else 1
            window = sate_window(planned_lijn2 if other == 2 else planned_lijn1)
            venster = (
                f" ({window[0] // 60:02d}:{window[0] % 60:02d}-"
                f"{window[1] // 60:02d}:{window[1] % 60:02d})" if window else ""
            )
            warnings.append(
                f"[ERROR] SATE_DROP: Order {order.order_nr} ({product.naam}, "
                f"{order.aantal} st) paste op {datum} maar liep door tijdens de "
                f"sate-productie op L{other}{venster} — L{sate_lijn} moet dan stilliggen"
            )
            continue
        devs = device_dropped_devs.get(id(order))
        if devs:
            # It fit the day. What it did not fit around was the other line's
            # hold on a machine there is only one of.
            warnings.append(
                f"[ERROR] TOESTEL_DROP: Order {order.order_nr} ({product.naam}, "
                f"{order.aantal} st) paste op {datum} maar niet naast het gebruik van "
                f"{', '.join(devs)} op de andere lijn — zonder die blokkade past hij wel"
            )
            continue
        warnings.append(
            f"[ERROR] CAPACITY_DROP: Order {order.order_nr} ({product.naam}, "
            f"{order.aantal} st) paste niet binnen {max_end.strftime('%H:%M')} — "
            f"niet ingepland op {datum}"
        )

    # 6. MEP-generatie
    mep_items: list[dict] = []
    for po in all_planned:
        mep_date = get_mep_date(po.order, po.product)
        if mep_date is None:
            continue
        mep_weekdag = WEEKDAG_MAP.get(mep_date.weekday(), "")
        if mep_weekdag in ("za", "zo"):
            warnings.append(
                f"MEP datum {mep_date.isoformat()} voor order {po.order.order_nr} "
                f"({po.product.naam}) valt op weekend - eerder klaarzetten"
            )
        mep_items.append({
            "order_nr": po.order.order_nr,
            "art_nr": po.order.art_nr,
            "naam": po.product.naam,
            "mep_datum": mep_date.isoformat(),
            "type": "D-2" if po.product.mep_dplus else "D-1",
        })

    kpis = day_kpis(datum, planned_lijn1, planned_lijn2, mep_items, warnings)

    schedule = DaySchedule(
        datum=datum,
        lijn1=planned_lijn1,
        lijn2=planned_lijn2,
        warnings=warnings,
        kpis=kpis,
        dropped=dropped,
        positie_dropped=positie_dropped,
        device_dropped=device_dropped,
        sate_dropped=sate_dropped,
    )

    # 7. Validatie
    from .validator import validate_schedule  # late import om cirkel te vermijden

    errors = validate_schedule(schedule, products, toestellen_beschikbaarheid)
    for err in errors:
        prefix = f"[{err.severity}] {err.code}"
        line = f"{err.message} (order {err.order_nr})" if err.order_nr else err.message
        schedule.warnings.append(f"{prefix}: {line}")

    return schedule


def replay_locked_days(
    locked_days: list,
    products: dict[str, Product],
) -> tuple[list[DaySchedule], list[str]]:
    """
    Rebuild DaySchedule objects from committed days — never re-run
    schedule_day on them.

    Why this exists: re-deriving a frozen day from scratch by calling
    schedule_day again on just its locked orders is NOT guaranteed to
    reproduce the exact same result it originally had, because the greedy
    day-sequencer's walk depends on the FULL set of orders that were on that
    day when it was first computed — including ones that didn't end up
    committed. Checking a locked order in isolation (or alongside a smaller
    set of day-mates than it originally had) can occasionally fail to fit
    it, even though nothing about the order or its capacity actually
    changed. Replaying the stored slots sidesteps that class of bug
    entirely: a frozen day is copied, not recomputed, so it is identical
    every time by construction.

    `locked_days` are plan_state.LockedDay objects, whose line lists are
    already in running order — so the committed SEQUENCE is carried too
    rather than re-derived from start times, and the day's warnings come
    back with it instead of being silently reset to empty.

    Returns (schedules, warnings) — a committed run whose product has since
    left the product database cannot be rendered, so it is reported rather
    than quietly kept or dropped.
    """
    schedules: list[DaySchedule] = []
    warnings: list[str] = []

    for day in locked_days:
        lijn1: list[PlannedOrder] = []
        lijn2: list[PlannedOrder] = []
        mep_items: list[dict] = []

        for lijn, runs in ((1, day.lijn1), (2, day.lijn2)):
            for run in runs:
                o = run.order
                product = products.get(o.art_nr)
                if product is None:
                    warnings.append(
                        f"plan_state: product {o.art_nr} van vastgezette order {o.order_nr} "
                        f"niet meer in de productdatabase — niet teruggeplaatst op {day.datum}"
                    )
                    continue
                po = PlannedOrder(
                    order=o, product=product, lijn=lijn,
                    start=datetime.strptime(run.start, "%H:%M").time(),
                    eind=datetime.strptime(run.eind, "%H:%M").time(),
                    ombouw_voor=run.ombouw_voor, warnings=[],
                    man=personeel_for_lijn(product, lijn),
                )
                (lijn1 if lijn == 1 else lijn2).append(po)

                mep_date = get_mep_date(o, product)
                if mep_date is not None:
                    mep_items.append({
                        "order_nr": o.order_nr,
                        "art_nr": o.art_nr,
                        "naam": product.naam,
                        "mep_datum": mep_date.isoformat(),
                        "type": "D-2" if product.mep_dplus else "D-1",
                    })

        kpis = day_kpis(day.datum, lijn1, lijn2, mep_items)
        schedules.append(DaySchedule(
            datum=day.datum, lijn1=lijn1, lijn2=lijn2,
            warnings=list(day.warnings), kpis=kpis,
        ))

    return schedules, warnings
