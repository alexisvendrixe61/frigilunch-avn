"""
multi_day_scheduler.py — run the daily scheduler across every day in a plan.

Two things live here:

  schedule_all_days()  group orders by productie_datum, call schedule_day()
                       per day, return the DaySchedule list that Excel export
                       and the dashboard are both built from.
  print_summary()      the stdout wrap-up at the end of a run.

The day/line ASSIGNMENT (which order goes where) is not done here — that's
`scheduler_ortools_v3.assign_production_dates_feedback`. This module only
walks days whose orders already carry a productie_datum and lijn.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from .constraints.grouping_rules import Volgende, volgende_van
from .day_quality import closer
from .models import DaySchedule, Order, Product
from .scheduler import schedule_day


def schedule_all_days(
    orders: list[Order],
    products: dict[str, Product],
    setup_matrix: dict,
    toestellen_beschikbaarheid: dict[str, int] | None = None,
    force_optimal_sequence: bool = False,
    force_until: date | None = None,
    voorgaand: list[DaySchedule] | None = None,
) -> list[DaySchedule]:
    """
    Groepeer orders per productie_datum en roep schedule_day() aan voor elke dag.
    Retourneert gesorteerde lijst van DaySchedule objecten.

    `force_optimal_sequence` runs the CP-SAT sequencer for every day (to
    minimize total changeover time), not just as drop-recovery — only pass
    True for a final, one-shot render (Excel/dashboard), never for repeated
    calls inside a search loop (see `_maybe_recover_drops` in scheduler.py).

    `force_until` narrows that to days on or before the given date, leaving
    the rest on the cheap greedy path. A solve per line-day is real money
    across a 100-day horizon; when the caller only needs accurate minutes for
    the weeks it is about to commit, paying for the open tail as well is
    waste.

    Days are built in date order and each line remembers the product it
    closed on, so the next workday can open on the same kapvorm / schaal (G2,
    G3 carry). `voorgaand` seeds that from days built elsewhere — the frozen
    days create_plan.py replays — so the first new day continues from them.
    """
    laatste: dict[int, Product] = {}
    for ds in sorted(voorgaand or [], key=lambda d: d.datum):
        _onthoud_afsluiter(laatste, ds)

    by_date: dict[date, list[Order]] = defaultdict(list)
    for o in orders:
        by_date[o.productie_datum].append(o)

    schedules: list[DaySchedule] = []
    dagen = sorted(by_date)
    for i, dag in enumerate(dagen):
        ds = schedule_day(
            orders=by_date[dag],
            products=products,
            setup_matrix=setup_matrix,
            datum=dag,
            toestellen_beschikbaarheid=toestellen_beschikbaarheid,
            force_optimal_sequence=(
                force_optimal_sequence
                and (force_until is None or dag <= force_until)
            ),
            vorige=dict(laatste),
            volgende=(
                _volgende(by_date[dagen[i + 1]], products) if i + 1 < len(dagen) else None
            ),
        )
        schedules.append(ds)
        _onthoud_afsluiter(laatste, ds)

    return schedules


def _volgende(orders: list[Order], products: dict[str, Product]) -> dict[int, Volgende]:
    """Per line, the kapvorm / schaal keys the next day's orders will run."""
    per_lijn: dict[int, list[Product]] = defaultdict(list)
    for o in orders:
        p = products.get(o.art_nr)
        if p is not None and o.lijn in (1, 2):
            per_lijn[o.lijn].append(p)
    return {lijn: volgende_van(ps) for lijn, ps in per_lijn.items()}


def _onthoud_afsluiter(laatste: dict[int, Product], ds: DaySchedule) -> None:
    """Record what each line closed this day with; an idle line keeps yesterday's."""
    for lijn, planned in ((1, ds.lijn1), (2, ds.lijn2)):
        if planned:
            laatste[lijn] = closer(planned)


# ── Samenvatting ──────────────────────────────────────────────────────────────

def print_summary(
    schedules: list[DaySchedule],
    assignment_warnings: list[str],
    product_warnings: list[str],
) -> None:
    """Print een beknopte samenvatting naar stdout."""
    print(f"\n{'='*70}")
    print(f"  FRIGILUNCH — PLANNING")
    print(f"{'='*70}")
    print(f"  Geplande dagen      : {len(schedules)}")
    total_orders = sum(
        len(s.lijn1) + len(s.lijn2) for s in schedules
    )
    print(f"  Totaal order-chunks : {total_orders}")

    errors = sum(
        1 for s in schedules
        for w in s.warnings if w.startswith("[ERROR]")
    )
    warns = sum(
        1 for s in schedules
        for w in s.warnings if w.startswith("[WARN") or w.startswith("[WARNING]")
    )
    print(f"  Hard errors         : {errors}")
    print(f"  Warnings            : {warns}")
    print(f"  Onplanbare warnings : {len(assignment_warnings)}")
    print(f"  Nieuwe producten    : {len(product_warnings)}")
    print(f"{'='*70}")

    if assignment_warnings:
        print("\n  ONPLANBARE / OVERLADEN ORDERS:")
        for w in assignment_warnings[:15]:
            print(f"  ! {w}")
        if len(assignment_warnings) > 15:
            print(f"  ... en {len(assignment_warnings)-15} meer")

    if product_warnings:
        print("\n  AUTO-AANGEVULDE PRODUCTEN (controleer allergeenprofiel):")
        for w in product_warnings:
            print(f"  ~ {w}")

    print()
