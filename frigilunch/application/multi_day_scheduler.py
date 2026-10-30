"""
multi_day_scheduler.py — Run the day builder across every production day.

Ported from src.multi_day_scheduler; all imports now point to frigilunch.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from frigilunch.application.day_quality import closer
from frigilunch.application.scheduler import schedule_day
from frigilunch.domain.constraints.grouping_rules import Volgende, volgende_van
from frigilunch.domain.models import DaySchedule, Order, Product


def schedule_all_days(
    orders: list[Order],
    products: dict[str, Product],
    setup_matrix: dict,
    toestellen_beschikbaarheid: dict[str, int] | None = None,
    force_optimal_sequence: bool = False,
    force_until: date | None = None,
    voorgaand: list[DaySchedule] | None = None,
) -> list[DaySchedule]:
    """Group orders by productie_datum and call schedule_day() for each day."""
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
    per_lijn: dict[int, list[Product]] = defaultdict(list)
    for o in orders:
        p = products.get(o.art_nr)
        if p is not None and o.lijn in (1, 2):
            per_lijn[o.lijn].append(p)
    return {lijn: volgende_van(ps) for lijn, ps in per_lijn.items()}


def _onthoud_afsluiter(laatste: dict[int, Product], ds: DaySchedule) -> None:
    for lijn, planned in ((1, ds.lijn1), (2, ds.lijn2)):
        if planned:
            laatste[lijn] = closer(planned)


def print_summary(
    schedules: list[DaySchedule],
    assignment_warnings: list[str],
    product_warnings: list[str],
) -> None:
    print(f"\n{'='*70}")
    print("  FRIGILUNCH — PLANNING")
    print(f"{'='*70}")
    print(f"  Geplande dagen      : {len(schedules)}")
    total_orders = sum(len(s.lijn1) + len(s.lijn2) for s in schedules)
    print(f"  Totaal order-chunks : {total_orders}")
    errors = sum(1 for s in schedules for w in s.warnings if w.startswith("[ERROR]"))
    warns  = sum(1 for s in schedules for w in s.warnings if w.startswith("[WARN"))
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
