"""
scheduler.py — Heuristic day builder; ported from src.scheduler.

Bridges still in place (not yet migrated):
  - src.day_sequencer.solve_optimal_sequence (CP-SAT sequencer)
  - src.validator.validate_schedule (validator)
"""
from __future__ import annotations

import os as _os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import NamedTuple

_repo_root = Path(__file__).parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from frigilunch.adapters import config
from frigilunch.domain.models import DaySchedule, Order, PlannedOrder, Product
from frigilunch.domain.constraints import (
    SATE_EXCLUSIVE, SATE_PARALLEL, WEEKDAG_MAP,
    calculate_duration, check_dag_toegelaten, check_kruiscontaminatie,
    get_earliest_start, get_mep_date, is_sate,
    overlaps_sate_window, resolve_lijn, sate_tail_index, sate_window,
    transition_cost,
    blocked_devices, busy_intervals, scarce_devices_needed, stagger_around,
    apply_positie_regels, mag_eerst, mag_laatst, moet_eerst, moet_laatst,
    positie_ok, sate_last, sort_orders_for_lijn,
)
from frigilunch.domain.constraints.grouping_rules import GEEN_VOLGENDE, Volgende
from frigilunch.domain.constraints.staffing import (
    check_bezetting_continuity, personeel_for_lijn,
)
from frigilunch.application.day_quality import line_quality, line_score, closer


# ── helpers ──────────────────────────────────────────────────────────────────

def _positie_ok(planned: list[PlannedOrder]) -> bool:
    return positie_ok([po.product for po in planned])


def _add_minutes(t: time, minutes: int) -> time:
    dt = datetime.combine(date.today(), t) + timedelta(minutes=minutes)
    return dt.time()


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


# ── day KPIs ─────────────────────────────────────────────────────────────────

def werk_minuten(planned: list[PlannedOrder]) -> int:
    return sum(po.ombouw_voor + _order_duur(po) for po in planned)


def _order_duur(po: PlannedOrder) -> int:
    d = _time_to_minutes(po.eind) - _time_to_minutes(po.start)
    return d if d >= 0 else d + 24 * 60


def span_minuten(planned: list[PlannedOrder]) -> int:
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
    sate_cap: "SateCap | None" = None,
    _repair_ends: bool = True,
) -> list[PlannedOrder]:
    if _SATE_EXCLUSIVE:
        orders_products = sate_last(orders_products)

    planned: list[PlannedOrder] = []
    current_time = get_earliest_start(lijn_nr)
    prev_po: PlannedOrder | None = None
    skipped: list[str] = []

    max_end_min = _time_to_minutes(max_end)
    cap = sate_cap or _NO_SATE_CAP
    ordinary_end_min = (
        min(max_end_min, _time_to_minutes(cap.free_end))
        if cap.free_end is not None else max_end_min
    )
    sate_end_min = (
        min(max_end_min, _time_to_minutes(cap.sate_end))
        if cap.sate_end is not None else max_end_min
    )

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

        limit_min = sate_end_min if is_sate(product) else ordinary_end_min
        crosses_midnight = eind_min < start_min
        exceeds_max = eind_min > limit_min

        if exceeds_max or crosses_midnight:
            limit_t = _add_minutes(time(0, 0), limit_min)
            reason = (
                "crosses midnight" if crosses_midnight
                else f"eindtijd {eind.strftime('%H:%M')} overschrijdt {limit_t.strftime('%H:%M')}"
            )
            skipped.append(
                f"Order {order.order_nr} ({product.naam}, {order.aantal} st): {reason}"
            )
            continue

        if eind_min > _time_to_minutes(time(14, 0)):
            order_warnings.append("Shift 2 actief")

        man_value = personeel_for_lijn(product, lijn_nr)

        po = PlannedOrder(
            order=order, product=product, lijn=lijn_nr,
            start=start, eind=eind, ombouw_voor=ombouw,
            warnings=order_warnings, man=man_value,
        )

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
    sate_cap: "SateCap | None" = None,
) -> list[PlannedOrder]:
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
    sate_cap: "SateCap | None" = None,
) -> tuple[list[PlannedOrder], list[Order]]:
    if not planned:
        return planned, []
    if _positie_ok(planned):
        return planned, []

    def _removal_rank(candidate, survivors) -> tuple[int, int]:
        order, product = candidate
        opens, closes = mag_eerst(product), mag_laatst(product)
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

    solved = _positie_via_sequencer(
        survivors, lijn_nr, datum, setup_matrix, max_end, blocking, toestellen_avail,
        sate_cap,
    )
    if solved is not None and len(solved) == len(survivors):
        return solved, []

    removed: list[Order] = []
    kept_greedy: list[PlannedOrder] = []
    while survivors:
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
    sate_cap: "SateCap | None" = None,
) -> list[PlannedOrder] | None:
    if not _OPTIMAL_SEQUENCE or not _S2_SEQUENCER_REPAIR or len(survivors) < 2:
        return None

    from frigilunch.application.day_sequencer import solve_optimal_sequence

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


_OPTIMAL_SEQUENCE = _os.environ.get("FRIGI_OPTIMAL_SEQUENCE", "1") == "1"
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
    sate_cap: "SateCap | None" = None,
    force: bool = False,
    vorige: Product | None = None,
    volgende: Volgende = GEEN_VOLGENDE,
) -> list[PlannedOrder]:
    if not _OPTIMAL_SEQUENCE:
        return greedy_result
    if not force and len(greedy_result) >= len(candidates):
        return greedy_result

    from frigilunch.application.day_sequencer import solve_optimal_sequence

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


_SYMMETRIC_DEVICE_PRIORITY = _os.environ.get("FRIGI_SYMMETRIC_DEVICE_PRIORITY", "1") == "1"

_SATE_EXCLUSIVE = SATE_EXCLUSIVE
_SATE_PARALLEL = SATE_PARALLEL


class SateCap(NamedTuple):
    free_end: time | None = None
    sate_end: time | None = None


_NO_SATE_CAP = SateCap()


def _sate_start(planned: list[PlannedOrder]) -> time | None:
    window = sate_window(planned)
    if window is None:
        return None
    return _add_minutes(time(0, 0), max(0, window[0]))


def _sate_cap_from(other: list[PlannedOrder]) -> SateCap | None:
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
    closer_: list[PlannedOrder],
    other: list[PlannedOrder],
    max_end: time,
) -> time:
    i = sate_tail_index(closer_)
    if i is None:
        return max_end
    current_end = _time_to_minutes(closer_[-1].eind)
    if not other:
        return closer_[-1].eind
    window_len = current_end - (_time_to_minutes(closer_[i].start) - closer_[i].ombouw_voor)
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
    if not any(is_sate(p) for _, p in sorted_orders):
        return 0
    probe = _schedule_lijn(sorted_orders, lijn_nr, datum, setup_matrix, max_end)
    window = sate_window(probe)
    if window is None:
        return 0
    return max(0, window[1] - window[0])


def _trim_sate_conflicts(
    planned: list[PlannedOrder],
    other: list[PlannedOrder],
) -> tuple[list[PlannedOrder], list[Order]]:
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
    second = 2 if first == 1 else 1
    caps: dict[int, SateCap | None] = {first: None, second: None}
    both_sate = False
    if _SATE_EXCLUSIVE:
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
            planned_first = _align_sate_tail(
                planned_first,
                _add_minutes(caps[first].free_end, _window_len(planned_first)),
                allow_earlier=True,
            )
        else:
            planned_first = _align_sate_tail(planned_first, max_end)
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
            blocking = busy_intervals(planned_first, toestellen) if toestellen else None
        else:
            cap2 = caps[second]
            planned_second = _align_sate_tail(
                planned_second,
                cap2.sate_end if cap2 and cap2.sate_end else max_end,
                blocking, toestellen,
            )
    return {first: (planned_first, None), second: (planned_second, blocking)}


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
    if not _DEVICE_DROP_CUTS or not blocking or not toestellen:
        return []
    survived = {id(po.order) for po in planned}
    lost = [(o, p) for o, p in candidates if id(o) not in survived]
    if not lost:
        return []
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
            continue
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
    warnings: list[str] = []
    weekdag = WEEKDAG_MAP.get(datum.weekday(), "")
    max_end = config.shift_end(datum)

    day_orders = [o for o in orders if o.productie_datum == datum]

    lijn1: list[tuple[Order, Product]] = []
    lijn2: list[tuple[Order, Product]] = []

    for order in day_orders:
        product = products.get(order.art_nr)
        if product is None:
            warnings.append(
                f"Order {order.order_nr}: product {order.art_nr} niet gevonden — overgeslagen"
            )
            continue

        dag_err = check_dag_toegelaten(product, datum)
        if dag_err:
            warnings.append(
                f"[ERROR] DAG_RESTRICTIE_DROP: Order {order.order_nr}: {dag_err} "
                f"— geweigerd, niet ingepland"
            )
            continue

        lijn = resolve_lijn(
            order, product,
            lijn1_minutes=sum(calculate_duration(o, p) for o, p in lijn1),
            lijn2_minutes=sum(calculate_duration(o, p) for o, p in lijn2),
        )
        (lijn1 if lijn == 1 else lijn2).append((order, product))

    volgende = volgende or {}
    lijn1_sorted = sort_orders_for_lijn(
        lijn1, (vorige or {}).get(1), volgende.get(1, GEEN_VOLGENDE))
    lijn2_sorted = sort_orders_for_lijn(
        lijn2, (vorige or {}).get(2), volgende.get(2, GEEN_VOLGENDE))

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

    sate_removed: dict[int, list[Order]] = {1: [], 2: []}
    closer_line, other_line = 1, 2
    if _SATE_EXCLUSIVE:
        by_line = {1: planned_lijn1, 2: planned_lijn2}
        ends = {n: (sate_window(p) or (0, 0))[1] for n, p in by_line.items()}
        closer_line = 1 if ends[1] >= ends[2] else 2
        other_line = 2 if closer_line == 1 else 1
        by_line[other_line], sate_removed[other_line] = _trim_sate_conflicts(
            by_line[other_line], by_line[closer_line]
        )
        by_line[closer_line], sate_removed[closer_line] = _trim_sate_conflicts(
            by_line[closer_line], by_line[other_line]
        )
        planned_lijn1, planned_lijn2 = by_line[1], by_line[2]
    sate_removed1, sate_removed2 = sate_removed[1], sate_removed[2]
    sate_cap1 = _sate_cap_from(planned_lijn2) if _SATE_EXCLUSIVE else None
    sate_cap2 = _sate_cap_from(planned_lijn1) if _SATE_EXCLUSIVE else None

    planned_lijn1, s2_removed1 = _enforce_positie_regels(
        planned_lijn1, 1, datum, setup_matrix, max_end,
        blocking1, toestellen_beschikbaarheid, sate_cap1,
    )
    planned_lijn2, s2_removed2 = _enforce_positie_regels(
        planned_lijn2, 2, datum, setup_matrix, max_end,
        blocking2, toestellen_beschikbaarheid, sate_cap2,
    )
    if _SATE_EXCLUSIVE:
        by_line = {1: planned_lijn1, 2: planned_lijn2}
        blocking_of = {1: blocking1, 2: blocking2}
        both_sate = all(sate_window(p) is not None for p in by_line.values())
        if both_sate and _SATE_PARALLEL:
            by_line = _synchronise_sate(by_line, max_end, toestellen_beschikbaarheid)
        elif sate_window(by_line[other_line]) is None:
            by_line[closer_line] = _align_sate_tail(
                by_line[closer_line],
                _handover_target(by_line[closer_line], by_line[other_line], max_end),
                blocking_of[closer_line], toestellen_beschikbaarheid, allow_earlier=True,
            )
        else:
            by_line[closer_line] = _align_sate_tail(
                by_line[closer_line], max_end, blocking_of[closer_line], toestellen_beschikbaarheid
            )
            opens = _sate_start(by_line[closer_line])
            if opens is not None:
                by_line[other_line] = _align_sate_tail(
                    by_line[other_line], opens, blocking_of[other_line], toestellen_beschikbaarheid
                )
        by_line[other_line], extra_other = _trim_sate_conflicts(by_line[other_line], by_line[closer_line])
        by_line[closer_line], extra_closer = _trim_sate_conflicts(by_line[closer_line], by_line[other_line])
        sate_removed[other_line] += extra_other
        sate_removed[closer_line] += extra_closer
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
            warnings.append(
                f"[ERROR] POSITIE_REGEL_DROP: Order {order.order_nr} ({product.naam}, "
                f"{order.aantal} st) verwijderd van {datum}: geen enkele volgorde van "
                f"deze dag laat een toegelaten product openen en afsluiten."
            )
            continue
        if id(order) in sate_removed_ids:
            sate_lijn = next(l for o, l in sate_dropped if id(o) == id(order))
            other_l = 2 if sate_lijn == 1 else 1
            window = sate_window(planned_lijn2 if other_l == 2 else planned_lijn1)
            venster = (
                f" ({window[0] // 60:02d}:{window[0] % 60:02d}-"
                f"{window[1] // 60:02d}:{window[1] % 60:02d})" if window else ""
            )
            warnings.append(
                f"[ERROR] SATE_DROP: Order {order.order_nr} ({product.naam}, "
                f"{order.aantal} st) paste op {datum} maar liep door tijdens de "
                f"sate-productie op L{other_l}{venster} — L{sate_lijn} moet dan stilliggen"
            )
            continue
        devs = device_dropped_devs.get(id(order))
        if devs:
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
        datum=datum, lijn1=planned_lijn1, lijn2=planned_lijn2,
        warnings=warnings, kpis=kpis, dropped=dropped,
        positie_dropped=positie_dropped, device_dropped=device_dropped,
        sate_dropped=sate_dropped,
    )

    from frigilunch.domain.validator import validate_schedule

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
