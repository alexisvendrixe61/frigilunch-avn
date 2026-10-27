#!/usr/bin/env python3
"""
create_plan.py — the weekly rolling production plan.

You run this once a week, for the week you are currently in. It:

  1. Reingests products.json and orders.json from the two source spreadsheets
  2. Reattaches orders committed by earlier runs (plan_state.json) and blocks
     the solver out of every day those runs froze
  3. Filters the rest to orders actually KNOWN by this run's Monday, delivering
     inside the horizon
  4. Assigns a production day + line to each with the CP-SAT feedback solver
  5. Builds the real per-day timelines, exports Excel + the capacity dashboard
  6. Commits the next COMMIT_WEEKS of its own output back to plan_state.json

Because step 6 always commits COMMIT_WEEKS forward and step 2 always drops
weeks that have passed, running week 31 then week 32 newly locks exactly one
additional week (35) and leaves 32-34 exactly as week 31 planned them.

Usage:
    python create_plan.py --week 31 --year 2026
    python create_plan.py --week 31 --reset      # ignore plan_state.json
    python create_plan.py --week 31 --weeks 10   # cap the open-ended horizon
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src import config, exceptions_report
from src.constraints.device_rules import scarce_devices_needed
from src.campaign_regroup import regroup_campaigns
from src.layout_metrics import format_summary, plan_layout_summary
from src.loader import load_product_warnings, load_products, load_setup_times
from src.models import Order, PlanScore, RejectionDiagnostic, SolveOutcome
from src.multi_day_scheduler import (
    print_summary,
    schedule_all_days,
)
from src.scheduler import replay_locked_days
from src.scheduler_ortools_v3 import (
    assign_production_dates_feedback, _positie_reason, _sate_reason,
    _FAR_MAX_ITERS, _FAR_TIMEOUT_SECONDS,
)
from src.exporter import export_multi_to_excel
from src.order_filter import (
    filter_by_delivery_range,
    filter_by_known_date,
    load_excel_orders,
    save_orders_to_json,
)
from src.product_setup_loader import write_product_setup_json
from src import totals
from src.plan_state import (
    COMMIT_WEEKS,
    STATE_SCHEMA_VERSION,
    block_end,
    build_new_state,
    commit_window,
    explain_from_history,
    in_commit_window,
    last_committed_day,
    load_plan_state,
    merge_rejection_history,
    order_identity,
    partition_locked_days,
    roll_completed_orders,
    save_plan_state,
)

DATA = Path(__file__).parent / "data"


def publication_blockers(outcome, phantom_count: int = 0) -> list[str]:
    """
    Reasons an optimizer result must not advance the frozen state.

    Only an invalid PLAN blocks: an order the solver assigned that the final
    render then left out of every day plan. Rejections do not — oversubscribed
    orders, oracle drops, products missing from the database. Those are real
    and reported (actie_vereist, dashboard), but the days that ARE planned are
    still valid. The stricter rule of 29/09 also blocked every result the
    solver had not proven optimal and every rejection it had not proven
    impossible; with a real order book that is every run, so nothing ever
    committed and no rolling chain could build (found 15/10 on a wk34 -> wk42
    replay; Liam chose to report rather than block). `publication_notes` says
    what the rule no longer blocks on.
    """
    blockers: list[str] = []
    if phantom_count:
        blockers.append(f"{phantom_count} assigned order(s) were dropped by final rendering")
    return blockers


def publication_notes(outcome) -> list[str]:
    """What would have blocked under the strict rule: printed, never blocking."""
    notes: list[str] = []
    if outcome.status == "unknown":
        notes.append("optimizer did not prove a complete or infeasible result")
    counts: dict[str, int] = {}
    for diagnostic in outcome.diagnostics:
        if diagnostic.category in {"invalid_master_data", "search_limit", "model_mismatch"}:
            counts[diagnostic.category] = counts.get(diagnostic.category, 0) + 1
    if counts:
        notes.append("rejections not proven impossible: " + ", ".join(
            f"{category} {n}" for category, n in sorted(counts.items())
        ))
    return notes


def exclusive_horizon_end(last_delivery: date) -> date:
    """First Monday strictly after the final delivery date."""
    end = last_delivery + timedelta(days=1)
    while end.weekday() != 0:
        end += timedelta(days=1)
    return end


def _merge_solve_outcomes(near: SolveOutcome, far: SolveOutcome) -> SolveOutcome:
    """
    Combine the committed-window solve with the open-tail solve into one
    outcome, as if a single call had produced it. The two never compete for
    the same day (they run over disjoint date ranges), so scheduled/rejected/
    diagnostics are a plain concatenation; only the status and the printed
    solver stats need an actual merge rule.
    """
    status_rank = {"complete": 0, "infeasible": 1, "unknown": 2}
    status = max((near.status, far.status), key=status_rank.__getitem__)
    score = PlanScore(
        rejected_orders=near.score.rejected_orders + far.score.rejected_orders,
        rejected_units=near.score.rejected_units + far.score.rejected_units,
        transition_minutes=near.score.transition_minutes + far.score.transition_minutes,
        scale_changes=near.score.scale_changes + far.score.scale_changes,
        early_days=near.score.early_days + far.score.early_days,
        moved_forecast_orders=near.score.moved_forecast_orders + far.score.moved_forecast_orders,
    )
    stats = dict(near.stats)
    stats["rejection_reasons"] = {
        **near.stats.get("rejection_reasons", {}),
        **far.stats.get("rejection_reasons", {}),
    }
    stats["solver_time_seconds"] = round(
        near.stats.get("solver_time_seconds", 0) + far.stats.get("solver_time_seconds", 0), 2
    )
    stats["solver_status"] = (
        f"{near.stats.get('solver_status')}(committed)/{far.stats.get('solver_status')}(open tail)"
    )
    stats["solver_gap"] = None
    near_fb = near.stats.get("feedback") or {}
    far_fb = far.stats.get("feedback") or {}
    if near_fb or far_fb:
        stats["feedback"] = {
            "iterations": near_fb.get("iterations", 0) + far_fb.get("iterations", 0),
            "converged": near_fb.get("converged", True) and far_fb.get("converged", True),
            "cuts_added": near_fb.get("cuts_added", 0) + far_fb.get("cuts_added", 0),
        }
    return SolveOutcome(
        status=status,
        scheduled=tuple(near.scheduled) + tuple(far.scheduled),
        rejected=tuple(near.rejected) + tuple(far.rejected),
        score=score,
        diagnostics=tuple(near.diagnostics) + tuple(far.diagnostics),
        stats=stats,
    )


def assign_tiered(
    filtered_free: list[Order],
    products,
    solver_start: date,
    solver_weeks: int,
    commit_end: date,
    setup_matrix: dict,
    toestellen: dict,
    frozen_until: date | None,
) -> SolveOutcome:
    """
    Split the solve at the committed-window boundary (`commit_end`): the days
    this run is about to freeze get the full search budget, because a mistake
    there is locked into plan_state.json for good. The open tail beyond it
    gets a cheap pass instead of the same budget — nothing there commits this
    run, next week's run re-decides it with a fresh full budget anyway, so
    spending minutes perfecting it now is wasted effort that only crowds out
    iterations the committed window could have used.

    Falls back to a single full-budget call (today's behaviour) when the
    whole horizon already fits inside the committed window, or when
    FRIGI_TIERED_HORIZON=0. When this run's committed window was already
    entirely frozen by an earlier run (nothing new to commit either way —
    see the "Volledige commit-venster was al bevroren" case below), the
    ENTIRE remaining horizon is open tail, so it all gets the cheap pass.
    """
    plan_end = solver_start + timedelta(weeks=solver_weeks)
    # Default OFF since 15/10: the split solved the committed window with only
    # the orders due inside it and planned everything later from the day AFTER
    # it — so, with the freeze, every order could only ever be produced in its
    # own delivery week (wk34->wk42 replay: 0 of 344). The planner's rule is
    # the opposite: any known order may be produced on any open day up to its
    # delivery. One joint solve does that; FRIGI_URGENT_FIRST in the solver
    # keeps orders due inside the window ahead of the ones produced early.
    tiering_enabled = os.environ.get("FRIGI_TIERED_HORIZON", "0") == "1"
    if not tiering_enabled:
        return assign_production_dates_feedback(
            filtered_free, products, solver_start, weeks=solver_weeks,
            setup_matrix=setup_matrix, toestellen=toestellen,
            frozen_until=frozen_until, commit_end=commit_end,
            _mandatory_orders=False,
        )
    if commit_end <= solver_start:
        print(f"[SCHEDULING] Commit-venster was al bevroren t/m "
              f"{commit_end - timedelta(days=1)} — hele horizon telt als open staart, "
              f"snellere pas ({_FAR_MAX_ITERS} iteraties, {_FAR_TIMEOUT_SECONDS:.0f}s budget).")
        return assign_production_dates_feedback(
            filtered_free, products, solver_start, weeks=solver_weeks,
            setup_matrix=setup_matrix, toestellen=toestellen,
            frozen_until=frozen_until, commit_end=commit_end,
            _mandatory_orders=False,
            _max_iters=_FAR_MAX_ITERS,
            _feedback_timeout_seconds=_FAR_TIMEOUT_SECONDS,
        )
    if plan_end <= commit_end:
        return assign_production_dates_feedback(
            filtered_free, products, solver_start, weeks=solver_weeks,
            setup_matrix=setup_matrix, toestellen=toestellen,
            frozen_until=frozen_until, commit_end=commit_end,
            _mandatory_orders=False,
        )

    hard_end = commit_end
    near_weeks = max(0, -(-(hard_end - solver_start).days // 7))
    far_weeks = max(0, -(-(plan_end - hard_end).days // 7))
    near_orders = [o for o in filtered_free if o.lever_datum < hard_end]
    far_orders = [o for o in filtered_free if o.lever_datum >= hard_end]

    print(f"[SCHEDULING] Tiered horizon: committed venster {solver_start} t/m "
          f"{hard_end - timedelta(days=1)} ({len(near_orders)} orders) op volle sterkte, "
          f"open staart {hard_end} t/m {plan_end - timedelta(days=1)} "
          f"({len(far_orders)} orders) op een snellere pas "
          f"({_FAR_MAX_ITERS} iteraties, {_FAR_TIMEOUT_SECONDS:.0f}s budget).")

    near = assign_production_dates_feedback(
        near_orders, products, solver_start, weeks=near_weeks,
        setup_matrix=setup_matrix, toestellen=toestellen,
        frozen_until=frozen_until, commit_end=commit_end,
        _mandatory_orders=False,
    )

    if not far_orders:
        return near

    far = assign_production_dates_feedback(
        far_orders, products, hard_end, weeks=far_weeks,
        setup_matrix=setup_matrix, toestellen=toestellen,
        frozen_until=None, commit_end=None,
        _mandatory_orders=False,
        _max_iters=_FAR_MAX_ITERS,
        _feedback_timeout_seconds=_FAR_TIMEOUT_SECONDS,
    )
    return _merge_solve_outcomes(near, far)


GENERATED = DATA / "generated"
OUTPUT = Path(__file__).parent / "output"


def load_orders_from_json(path: str | Path) -> list[Order]:
    """Load orders from orders.json"""
    with open(path, "r", encoding="utf-8") as f:
        orders_data = json.load(f)

    orders = []
    for item in orders_data:
        order = Order(
            order_nr=item["order_nr"],
            art_nr=item["art_nr"],
            klant=item["klant"],
            aantal=item["aantal"],
            lever_datum=date.fromisoformat(item["lever_datum"]),
            productie_datum=date.fromisoformat(item["productie_datum"]),
            # .get: an orders.json written before the remark columns existed
            # is still readable — the next refresh from Excel fills them in.
            opmerking=item.get("opmerking", ""),
            opmerking_analyse=item.get("opmerking_analyse", ""),
            plan_nr=item.get("plan_nr", ""),
        )
        orders.append(order)

    return orders


def iso_weeks_before(year: int, week: int, n: int) -> list[tuple[int, int]]:
    """The n ISO (year, week) pairs immediately before this one, oldest first."""
    monday = date.fromisocalendar(year, week, 1)
    return [
        (monday - timedelta(weeks=k)).isocalendar()[:2]
        for k in range(n, 0, -1)
    ]


def run_warmup(weeks: list[tuple[int, int]], args) -> None:
    """
    Re-run this script once per preceding week so the target week starts from
    the plan_state a real weekly cadence would have produced.

    Why this exists: on a cold run the solver may not place anything before
    plan_start, so an order delivering early in the target week loses most of
    its 14-day production window — measured on wk36, 43 orders were left with
    5 days instead of 14 and 786,646 units piled into the first fortnight.
    In production those orders were placed and frozen by the runs for the
    preceding weeks. Simulating those runs is the only way to reproduce that
    without inventing a schedule nobody ever built.

    A subprocess per week rather than an in-process loop: a warm-up week has
    to be indistinguishable from having actually run it that week, and the
    solver keeps module-level state (the incremental-resolve day cache) that
    would carry over between iterations. Each child gets --warmup 0 so it
    cannot recurse, and falls back to its own --as-of default (the Monday of
    its own week), which is what stops it seeing demand not yet booked then.
    """
    for i, (y, w) in enumerate(weeks, start=1):
        cmd = [sys.executable, str(Path(__file__).resolve()),
               "--week", str(w), "--year", str(y), "--warmup", "0"]
        if args.weeks is not None:
            cmd += ["--weeks", str(args.weeks)]
        # Only the FIRST warm-up week starts from nothing. The rest must
        # inherit what the previous one committed — that chain IS the warm-up.
        if args.reset and i == 1:
            cmd.append("--reset")
        print("\n" + "=" * 80)
        print(f"[WARMUP] {i}/{len(weeks)} — week {w} ({y}) draaien om de rolling lock op te bouwen")
        print("=" * 80)
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[ERROR] Warmup-run voor week {w} faalde (exit {rc}) — gestopt.")
            sys.exit(rc)


def main():
    parser = argparse.ArgumentParser(
        description="Rolling weekly productieplanning vanaf inkomende orders"
    )
    parser.add_argument(
        "--month",
        type=int,
        default=None,
        help="Doel maand (1-12) voor delivery filtering, default: 5 (mei) — genegeerd als --week is opgegeven",
    )
    parser.add_argument(
        "--week",
        type=int,
        default=None,
        help="ISO weeknummer (1-53) — planning start = de maandag van deze week",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2026,
        help="Doel jaar voor delivery filtering, default: 2026",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=None,
        help=(
            "Planning horizon in weken. Default: plan alles tot de verste "
            "bekende leverdatum in orders.json (niet begrensd tot een vast "
            "aantal weken). Geef expliciet op om terug te beperken."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Negeer plan_state.json (eerder gecommitte weken) en plan volledig "
            "opnieuw vanaf nul, alsof er nooit eerder gepland is."
        ),
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help=(
            "Datum (YYYY-MM-DD) om als 'vandaag' te gebruiken voor de done-check "
            "op vastgezette orders (locked productie_datum < as-of => al geproduceerd, "
            "wordt gedropt). Default: de maandag van --week (dus normaal niet nodig — "
            "je run altijd de week waar je nu in zit). Override alleen voor edge cases, "
            "bv. een run midweek waarbij je de ECHTE dag van vandaag wilt gebruiken."
        ),
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Draai eerst de N weken vóór --week, elk met een normale commit, zodat "
            "deze run een gevulde plan_state.json aantreft in plaats van een lege. "
            f"Default: {COMMIT_WEEKS} als plan_state.json leeg is (of bij --reset), "
            "anders 0. Zonder opwarming wordt het productievenster van orders die vroeg "
            f"in de doelweek leveren afgekapt op de rundatum. Minder dan {COMMIT_WEEKS} "
            "laat de doelweek zelf door de koude run beslissen — zie scheduler.md. "
            "Kost N extra runs. Zet op 0 om snel te testen."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Plan en exporteer, maar schrijf NIETS naar plan_state.json. Gebruik dit "
            "om een week te verkennen zonder hem vast te zetten — zonder deze vlag "
            "commit ELKE run de eerstvolgende COMMIT_WEEKS weken."
        ),
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help=(
            "Hergebruik de chart_data JSON van de vorige run en render alleen het "
            "dashboard opnieuw. Geen solve, geen Excel, geen commit — bedoeld voor "
            "wijzigingen aan dashboard_template.html."
        ),
    )
    args = parser.parse_args()

    month_name = [
        "",
        "januari",
        "februari",
        "maart",
        "april",
        "mei",
        "juni",
        "juli",
        "augustus",
        "september",
        "oktober",
        "november",
        "december",
    ][args.month or 5]

    if args.week is not None:
        label = f"wk{args.week:02d}"
        period_desc = f"ISO WEEK {args.week}"
    else:
        label = f"{month_name}_{args.year}"
        period_desc = f"{month_name.upper()} {args.year}"

    output_file = OUTPUT / f"planning_{label}.xlsx"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    dashboard_json = OUTPUT / f"chart_data_{label}.json"
    dashboard_html = OUTPUT / f"capacity_dashboard_{label}.html"

    # ── --render-only: re-render the last run's dashboard, no solve ──
    # This was the one thing gen_chart_data.py could do that this script could
    # not, and the only reason to keep a second entry point around.
    if args.render_only:
        from src.dashboard import render_dashboard_html
        if not dashboard_json.exists():
            print(f"[ERROR] {dashboard_json} bestaat niet — draai eerst een echte run.")
            sys.exit(1)
        render_dashboard_html(
            json.loads(dashboard_json.read_text(encoding="utf-8")),
            Path(__file__).parent / "dashboard_template.html",
            dashboard_html,
        )
        print(f"[OK] Opnieuw gerenderd uit {dashboard_json.name}: {dashboard_html}")
        return

    # ── Warm-up: run the weeks before this one first, so the rolling lock is
    # built up the way a real weekly cadence would have built it. See
    # run_warmup for why a cold run distorts the target week. ──
    plan_state_path = GENERATED / "plan_state.json"
    warmup = args.warmup
    if warmup is None:
        cold = args.reset or not load_plan_state(plan_state_path)["days"]
        # COMMIT_WEEKS, not COMMIT_WEEKS - 1: a run for week W newly solves week
        # W + COMMIT_WEEKS - 1 (everything before that is already frozen), so the
        # TARGET week is decided by the run COMMIT_WEEKS - 1 weeks earlier. One
        # warm-up week fewer and that decider is the cold run itself — which
        # leaves exactly the week you care about distorted. See scheduler.md.
        warmup = COMMIT_WEEKS if cold else 0
    if warmup and args.week is None:
        print("[WARN] --warmup werkt alleen samen met --week — overgeslagen.")
        warmup = 0
    if warmup and args.dry_run:
        # --dry-run promises plan_state.json is left untouched, but a warm-up
        # week only means anything if it commits. Rather than quietly break
        # one of the two, make the choice explicit.
        print("[ERROR] --dry-run en --warmup gaan niet samen: de opwarmweken moeten "
              "committen om de lock op te bouwen.\n"
              "        Draai eerst de opwarming zonder --dry-run, en daarna deze week "
              "met --dry-run --warmup 0.")
        sys.exit(1)
    if warmup:
        weeks = iso_weeks_before(args.year, args.week, warmup)
        print(f"\n[WARMUP] plan_state.json is leeg — eerst "
              f"{', '.join('wk' + str(w) for _y, w in weeks)} draaien, dan week {args.week}.")
        print(f"[WARMUP] Dit zijn {warmup} extra runs. Gebruik --warmup 0 om over te slaan "
              f"(sneller, maar de doelweek lijkt dan voller dan hij is).")
        run_warmup(weeks, args)
        # The warm-up just wrote the state this run needs; resetting now would
        # throw away exactly what it was for.
        args.reset = False
        print("\n" + "=" * 80)
        print(f"[WARMUP] Opwarming klaar — nu de eigenlijke run voor week {args.week}")
        print("=" * 80)

    print("=" * 80)
    print(f"[PLANNING] PRODUCTIE PLANNING: {period_desc} EN DAARNA")
    print("=" * 80)

    # ── Regenerate products from Planning info.xlsx ──
    print(f"\n[LOAD] Regenereer products uit Planning info.xlsx...")
    excel_file = DATA / "source_data" / "Planning info.xlsx"
    products_file = GENERATED / "products.json"

    # Same reasoning as the orders reingest below: speeds, line rules and
    # allergen levels all come from here, so falling back to a stale
    # products.json produces a plausible-looking but wrong plan.
    if not excel_file.exists():
        print(f"[ERROR] {excel_file} niet gevonden — kan de productdatabase niet inlezen.")
        sys.exit(1)
    write_product_setup_json(excel_file, products_file)
    print(f"[OK] Regenereerd: {products_file}")

    # ── Load products and constraints ──
    print(f"\n[LOAD] Laad product database...")
    if not products_file.exists():
        # No silent fallback: planning against a stale product database would
        # produce a plausible-looking but wrong plan (speeds, line rules and
        # allergen levels all come from here). Fail loudly instead.
        print(f"[ERROR] {products_file} bestaat niet en kon niet worden gegenereerd "
              f"uit '{excel_file.name}'. Zet de bronsheet terug en run opnieuw.")
        sys.exit(1)
    products, toestellen = load_products(products_file)
    all_product_warnings = load_product_warnings(products_file)
    setup_matrix = load_setup_times(DATA / "setup_times.json")
    print(f"[OK] {len(products)} producten geladen")

    # ── Device availability: capacity.json wins over products.json ──
    _cfg_toestellen = config.device_availability()
    if _cfg_toestellen:
        toestellen = _cfg_toestellen
        scarce = sorted(k for k, v in toestellen.items() if v < 2)
        print(f"[OK] Toestelbeschikbaarheid uit capacity.json ({len(toestellen)} toestellen, "
              f"{len(scarce)} schaars): {', '.join(scarce)}")

    # ── Regenerate orders.json from orderverwerking en planning.xlsm (latest data) ──
    print(f"\n[LOAD] Regenereer orders uit 'orderverwerking en planning.xlsm'...")
    orders_excel = DATA / "source_data" / "orderverwerking en planning.xlsm"
    orders_path = GENERATED / "orders.json"
    # No silent fallback to a stale orders.json. This used to be a bare
    # `except Exception -> [WARN], laad bestaande orders.json`, which meant a
    # UnicodeEncodeError while merely PRINTING the sheet summary (cp1252
    # console, emoji in the message) quietly produced a full plan against
    # last week's order book. A plan built on the wrong demand is worse than
    # no plan.
    if not orders_excel.exists():
        print(f"[ERROR] {orders_excel} niet gevonden — kan de orders niet inlezen.")
        sys.exit(1)
    fresh_orders = load_excel_orders(orders_excel, sheet_name="orderverwerking")
    save_orders_to_json(fresh_orders, orders_path)
    print(f"[OK] Regenereerd: {orders_path}")

    # ── Load orders from JSON ──
    print(f"\n[LOAD] Laad orders uit orders.json...")
    if not orders_path.exists():
        print(f"[ERROR] Bestand niet gevonden: {orders_path}")
        sys.exit(1)

    all_orders = load_orders_from_json(orders_path)
    print(f"[OK] {len(all_orders)} orders geladen")

    # ── Calculate plan window ──
    print(f"\n[PLAN] Bereken planning startdatum...")
    if args.week is not None:
        plan_start = date.fromisocalendar(args.year, args.week, 1)  # 1 = maandag
    else:
        # Plan starts on first day of target month (snapped to Monday)
        plan_start = date(args.year, args.month or 5, 1)
        while plan_start.weekday() != 0:
            plan_start += timedelta(days=1)

    commit_start, commit_end = commit_window(plan_start)
    as_of = date.fromisoformat(args.as_of) if args.as_of else plan_start

    if args.weeks is not None:
        plan_end = plan_start + timedelta(weeks=args.weeks)
    else:
        # Open horizon: out to the furthest delivery date this run can actually
        # SEE. It used to be taken from every order in the file, including the
        # ones filter_by_known_date removes moments later as future
        # information — so a wk31 run planned 42 weeks and 209 workdays for
        # 174 known orders, sizing its own horizon from data it then declared
        # it did not have.
        known_all, _ = filter_by_known_date(all_orders, as_of)
        future_deliveries = [o.lever_datum for o in known_all if o.lever_datum >= plan_start]
        plan_end = (
            exclusive_horizon_end(max(future_deliveries))
            if future_deliveries else commit_end
        )
        # ``plan_end`` is exclusive. It must be the Monday AFTER the last
        # delivery, including when that delivery itself is a Monday.

    # ── Locked orders from a previous run's commit ──
    if args.reset:
        print(f"\n[LOCK] --reset: plan_state.json wordt genegeerd, volledig herplannen")
        plan_state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "frozen_until": None,
            "days": {},
            "completed_orders": {},
            "rejections": {},
            "warnings": [],
        }
    else:
        plan_state = load_plan_state(plan_state_path)
        print(f"\n[LOCK] {len(plan_state['days'])} eerder gecommitte dagen in plan_state.json")
    for w in plan_state.get("warnings", []):
        print(f"[WARN] {w}")

    completed_orders = roll_completed_orders(
        all_orders,
        plan_state["days"],
        plan_state.get("completed_orders", {}),
        today=as_of,
    )
    locked_days, remaining_orders, lock_warnings = partition_locked_days(
        all_orders,
        plan_state["days"],
        today=as_of,
        completed_state=completed_orders,
    )
    for w in lock_warnings:
        print(f"[WARN] {w}")
    locked_orders = [run.order for day in locked_days for run in day.runs]
    if locked_days:
        print(f"[OK] {len(locked_orders)} orders vastgezet over {len(locked_days)} dagen "
              f"(gepland in eerdere run): {locked_days[0].datum} -> {locked_days[-1].datum}")

    # ── Freeze boundary: the solver may not place ANYTHING before this date —
    # not just already-locked orders staying put, the days themselves are
    # off-limits, even genuinely idle capacity. See plan_state.block_end. ──
    solver_start = block_end(plan_start, plan_state.get("frozen_until"))
    # Ceiling, not floor: floor division silently dropped up to 6 days off the
    # end of the horizon, so orders delivering in that stub had no legal slot
    # and came back as NEVER-PLACED for no real reason.
    solver_weeks = max(0, -(-(plan_end - solver_start).days // 7))
    if solver_start > plan_start:
        print(f"[LOCK] Bevroren t/m {solver_start - timedelta(days=1)} (vorige run's commit) "
              f"— solver plant alleen vanaf {solver_start}")
    if solver_weeks == 0:
        print(f"[WARN] Bevroren venster beslaat de hele planning-horizon — niets nieuws te plannen deze run")

    # ── Filter free (not locked) orders to what was actually known by `as_of` ──
    known_orders, known_warnings = filter_by_known_date(remaining_orders, as_of)
    for w in known_warnings:
        print(f"[WARN] {w}")
    dropped_future = len(remaining_orders) - len(known_orders)
    if dropped_future:
        print(f"[FILTER] {dropped_future} orders nog niet geplaatst op {as_of} "
              f"(order_nr later) — genegeerd voor deze run")

    # ── Filter free (not locked) orders on delivery date ──
    print(f"\n[FILTER] Filter op {period_desc} en daarna...")
    filtered_free = filter_by_delivery_range(known_orders, plan_start, plan_end)
    filtered_orders = locked_orders + filtered_free
    active_articles = {o.art_nr for o in filtered_orders}
    product_warnings = [
        warning for warning in all_product_warnings
        if any(f"product {article}:" in warning for article in active_articles)
    ]
    for warning in product_warnings:
        print(f"[WARN] {warning}")
    print(f"[OK] {len(filtered_free)} nieuw te plannen orders gefilterd "
          f"(+ {len(locked_orders)} vastgezet)")

    if not filtered_orders:
        print(f"\n[ERROR] Geen orders gevonden voor {period_desc} en daarna")
        sys.exit(1)

    # ── Pre-flight data quality check ──
    # Strip orders the solver would silently reject anyway due to missing master
    # data. Catching them here (before the solver) means: clear actionable
    # warnings, no wasted solver time, and the rejection shows up in the action
    # list with the right cause from the start.
    dq_bad: list[tuple[Order, str]] = []
    dq_good: list[Order] = []
    for o in filtered_free:
        if o.art_nr not in products:
            dq_bad.append((o, "unknown_product"))
        elif products[o.art_nr].constraint_provenance.get("speed") == "inferred":
            dq_bad.append((o, "missing_speed"))
        else:
            dq_good.append(o)
    if dq_bad:
        by_cause: dict[str, list[Order]] = {}
        for o, cause in dq_bad:
            by_cause.setdefault(cause, []).append(o)
        for cause, orders_bad in by_cause.items():
            arts = sorted({o.art_nr for o in orders_bad})
            print(f"[DATA QUALITY] {len(orders_bad)} order(s) geweigerd ({cause}): "
                  f"art.nr {', '.join(arts)} — voeg toe aan Planning info.xlsx")
        filtered_free = dq_good

    actual_weeks = (plan_end - plan_start).days // 7
    print(f"[OK] Planning loopt van {plan_start} tot {plan_end}")
    print(f"[OK] ({actual_weeks} weken, nieuw commit-venster {commit_start} t/m {last_committed_day(commit_end)})")

    # ── Show order summary ──
    total_units = sum(o.aantal for o in filtered_orders)
    delivery_dates = sorted(set(o.lever_datum for o in filtered_orders))
    print(f"\n[SUMMARY] Overzicht gefilterde orders:")
    print(f"[INFO] Aantal orders: {len(filtered_orders)}")
    print(f"[INFO] Totale stuks: {total_units:,}")
    print(f"[INFO] Leveringsdatum range: {delivery_dates[0]} -> {delivery_dates[-1]}")

    # ── Solver budget (for reporting; the solver reads this itself too) ──
    # feedback_timeout_seconds is spent once PER PHASE (line-restricted /
    # flexible / retry), not once in total — so report it as a per-phase
    # budget, not the unrelated single-pass `timeout_seconds` this used to
    # print.
    solver_config = config.solver_config()
    iter_timeout = solver_config.get("feedback_iter_timeout_seconds", 10)
    timeout = solver_config.get("feedback_timeout_seconds", 240)

    # ── Assign production dates (OR-Tools CP-SAT V3 feedback) — only for free
    # orders, and only from `solver_start` onward. The frozen zone (before
    # solver_start) is never even offered to the solver as a candidate day —
    # not a capacity-budget approximation like before, an actual absence of
    # any day/line slot there — so nothing can land in it, locked or not. ──
    print(f"\n[SCHEDULING] Wijs productiedatums toe (OR-Tools CP-SAT V3 feedback, "
          f"{timeout}s per fase, {iter_timeout}s per iteratie)...")
    solve_outcome = assign_tiered(
        filtered_free, products, solver_start, solver_weeks, commit_end,
        setup_matrix, toestellen,
        # So an order that can never be placed because its whole delivery
        # window sits inside the frozen zone is reported as exactly that,
        # instead of looking like ordinary oversubscription forever.
        solver_start if solver_start > plan_start else None,
    )

    # Merge pre-flight data-quality rejects back into the outcome so they
    # appear in the HTML dashboard and action list with the correct cause.
    if dq_bad:
        dq_orders = tuple(o for o, _ in dq_bad)
        dq_diagnostics = tuple(
            RejectionDiagnostic(
                order_identity(o), "invalid_master_data",
                detail="unknown_product" if cause == "unknown_product" else "MISSING-SPEED",
            )
            for o, cause in dq_bad
        )
        dq_reasons = {
            order_identity(o): (
                f"Product {o.art_nr} unknown" if cause == "unknown_product"
                else f"MISSING-SPEED: Product {o.art_nr} has no speed on either production line"
            )
            for o, cause in dq_bad
        }
        solve_outcome = SolveOutcome(
            status=solve_outcome.status,
            scheduled=solve_outcome.scheduled,
            rejected=solve_outcome.rejected + dq_orders,
            score=PlanScore(
                rejected_orders=solve_outcome.score.rejected_orders + len(dq_orders),
                rejected_units=solve_outcome.score.rejected_units + sum(o.aantal for o in dq_orders),
                transition_minutes=solve_outcome.score.transition_minutes,
                scale_changes=solve_outcome.score.scale_changes,
                early_days=solve_outcome.score.early_days,
                moved_forecast_orders=solve_outcome.score.moved_forecast_orders,
            ),
            diagnostics=solve_outcome.diagnostics + dq_diagnostics,
            stats={
                **solve_outcome.stats,
                "rejection_reasons": {
                    **solve_outcome.stats.get("rejection_reasons", {}),
                    **dq_reasons,
                },
            },
        )

    scheduled_free, rejected, stats = solve_outcome
    _in_ids = {order_identity(o) for o in filtered_free}
    _out_ids = {order_identity(o) for o in scheduled_free} | {order_identity(o) for o in rejected}
    _lost = _in_ids - _out_ids
    if _lost:
        print(f"[WARN] SOLVE-STAGE LOSS: {len(_lost)} order(s) in filtered_free missing from "
              f"both scheduled_free and rejected: {sorted(_lost)[:10]}")

    # Reconciliation is NOT run here. assign_production_dates_feedback already
    # ends with reconcile_combined_schedule (see _assign_two_phase_hard_first),
    # so calling it again on the same result was pure duplication: another full
    # schedule_all_days pass over the whole horizon per round, and — because a
    # second pass rewrites every reason it touches as COMBINED-SCHEDULE-DROP —
    # it destroyed the NEVER-PLACED / ORACLE-DROPPED split the dashboard
    # reports (which is why that breakdown read 0 / 0 / all-other).
    #
    # locked_orders never take part in reconciliation either way: they are
    # replayed byte-for-byte from their persisted render below rather than
    # re-derived, so there is nothing to reconcile them against.

    # ── Render frozen days by REPLAYING what was committed, never by
    # re-deriving them through schedule_day — see replay_locked_days for why
    # re-derivation isn't guaranteed to reproduce the same result once a
    # locked order is checked without its original (now-uncommitted)
    # day-mates. The stored day carries its own sequence and warnings, so
    # both come back untouched. ──
    replayed_schedules, replay_warnings = replay_locked_days(locked_days, products)
    for w in replay_warnings:
        print(f"[WARN] {w}")

    # ── G2 / G3 over days: regroup kapvorm and schaal in the window this run
    # freezes. The solver's soft stage cannot get there in its budget (see
    # campaign_regroup); this moves single orders onto days where their
    # kapvorm / schaal already runs, each move checked by the real day
    # builder, never at the cost of an order or a hard rule. ──
    if commit_end > solver_start:
        regroup = regroup_campaigns(
            scheduled_free, products, setup_matrix, toestellen,
            solver_start, commit_end, voorgaand=replayed_schedules,
            frozen_until=solver_start if solver_start > plan_start else None,
        )
        print(f"[REGROUP] kapvorm/schaal-campagnes: {regroup['moves']} verplaatsingen, {regroup['swaps']} wissels "
              f"({regroup['evaluated']} getest, score {regroup['score_before']} -> "
              f"{regroup['score_after']}, {regroup['seconds']}s)")

    print(f"\n[SCHEDULE] Genereer dagschema's...")
    # Use the same exact per-day sequencer as the optimizer's oracle over the
    # whole reported horizon. A weaker open-tail render could otherwise drop
    # an order that the oracle had already proved fits.
    derived_schedules = schedule_all_days(
        scheduled_free, products, setup_matrix, toestellen,
        force_optimal_sequence=True,
        voorgaand=replayed_schedules,
    )
    schedules = sorted(replayed_schedules + derived_schedules, key=lambda s: s.datum)
    print(f"[OK] {len(schedules)} dagschema's aangemaakt "
          f"({len(replayed_schedules)} replayed, {len(derived_schedules)} nieuw berekend)")

    scheduled = locked_orders + scheduled_free

    # ── Reconcile the solver's answer against the day plans actually built ──
    # The solver decides day+line; `schedule_all_days` above is what really
    # lays each day out, and it drops anything that would run past shift end
    # (capacity, midnight-cross, or a device stagger pushing it past close).
    # Those two lists could disagree, and `scheduled` was taken from the FIRST
    # one — so a dropped order stayed counted as scheduled, contributed its
    # units to the totals, and showed as placed on the dashboard, while no day
    # tab in the Excel contained it. Nobody would ever have made it.
    #
    # totals.compute() could not catch this either: it only compares the
    # solver's own three lists against each other, so an order missing from
    # the day plans is invisible to `unaccounted` by construction.
    #
    # Note the drop machinery INSIDE the solver (oracle feedback, no-good
    # cuts, reconciliation rounds, forced splitting) already retries these —
    # this is strictly about the final render, which has no retry left.
    planned_now = {id(po.order) for s in schedules for po in (s.lijn1 + s.lijn2)}
    phantom = [o for o in scheduled if id(o) not in planned_now]
    if phantom:
        toestellen_avail = config.device_availability()
        by_day = {id(o): s.datum for s in schedules for o in s.dropped}
        # Same split as the solver's oracle makes: an order removed to keep the
        # day's ends legal did not run out of time, and saying so here keeps the
        # final render from reintroducing the capacity story the solver just
        # stopped telling.
        s2_ids = {id(o) for s in schedules for o in s.positie_dropped}
        # A5 is a third cause with a third remedy: not more minutes, not a free
        # machine, but a day whose other line is not running peanut.
        sate_lijn = {id(o): lijn for s in schedules for o, lijn in s.sate_dropped}
        for o in phantom:
            product = products.get(o.art_nr)
            if id(o) in s2_ids:
                stats.setdefault("rejection_reasons", {})[order_identity(o)] = (
                    _positie_reason(o, product)
                )
                continue
            if id(o) in sate_lijn:
                stats.setdefault("rejection_reasons", {})[order_identity(o)] = (
                    _sate_reason(o, product, sate_lijn[id(o)])
                )
                continue
            devs = sorted(scarce_devices_needed(product, toestellen_avail)) if product else []
            dag = by_day.get(id(o))
            stats.setdefault("rejection_reasons", {})[order_identity(o)] = (
                f"DAY-BUILD-DROP: de solver plaatste deze order"
                + (f" op {dag}" if dag else "")
                + f", maar het dagschema kreeg hem niet meer voor sluitingstijd rond."
                + (f" TOESTELLEN: {', '.join(devs)} (1 exemplaar) kunnen de start"
                   f" hebben opgeschoven." if devs else "")
            )
        scheduled = [o for o in scheduled if id(o) in planned_now]
        rejected = list(rejected) + phantom
        print(f"[WARN] {len(phantom)} order(s) door de solver ingepland maar niet in het "
              f"dagschema geland — verplaatst naar rejected (zie DAY-BUILD-DROP)")

    # Solver stats only cover the free-order subset — recount the WHOLE plan
    # (locked carried forward + newly solved) through src/totals.py, the same
    # ── Say WHY a frozen-out order was never made ──
    # ARRIVED-AFTER-FREEZE only reports that the order is now unreachable. The
    # run that actually tried it — one or two weeks earlier — is the one that
    # knows why, so its reason is carried forward in plan_state and appended
    # here. Without this the report says "ordered too late" about orders booked
    # months in advance.
    run_week = plan_start.isocalendar()[1]
    _reasons = stats.setdefault("rejection_reasons", {})
    _history = plan_state.get("rejections", {})
    for o in rejected:
        key = order_identity(o)
        if _reasons.get(key, "").startswith("ARRIVED-AFTER-FREEZE"):
            _reasons[key] += explain_from_history(_history.get(key))
    # Remember this run's real attempts for the runs after it. Bounded to
    # orders still in the book whose delivery has not passed.
    _keep = {order_identity(o) for o in all_orders if o.lever_datum >= as_of}
    new_rejections = merge_rejection_history(_history, _reasons, run_week, _keep)

    # calculation the Excel export and the dashboard use, so all three report
    # the same numbers for the same run.
    plan_totals = totals.compute(filtered_orders, scheduled, rejected)
    stats["scheduled_orders"] = plan_totals.scheduled_orders
    stats["scheduled_units"] = plan_totals.scheduled_units
    stats["rejected_orders"] = plan_totals.rejected_orders
    stats["rejected_units"] = plan_totals.rejected_units
    stats["scheduled_chunks"] = plan_totals.scheduled_chunks
    stats["orders_split"] = plan_totals.orders_split
    stats["orders_in_window"] = plan_totals.orders_in

    # The invariant: every order that went in is either scheduled or rejected,
    # exactly once. Silence here is the whole point; noise means the plan's
    # own arithmetic doesn't close and the numbers below can't be trusted.
    for line in totals.report_imbalance(plan_totals, prefix="[WARN] TOTALS"):
        print(line)

    # How the newly committed days look to the floor: idle holes, unused shift
    # end, identical products and kapvorm broken up (planner feedback 02/10).
    layout = plan_layout_summary(schedules, commit_start, commit_end)
    stats["layout"] = layout
    print(f"[LAYOUT] commit-venster: {format_summary(layout)}")

    # Build the next commit state now (not just before saving) so the
    # dashboard's "committed" zone can reflect the REAL locked window —
    # previously-locked days plus whatever this run newly commits — instead
    # of a generic weeks-from-plan_start guess. Reading `schedules` (the day
    # plans actually built) rather than the solver's order list is what keeps
    # a DAY-BUILD-DROP order out of the commit: it was already removed from
    # `scheduled` above, and it was never in a day's lijn1/lijn2 to begin with.
    new_days = build_new_state(schedules, commit_start, commit_end)
    dash_committed_start = (
        min(o.productie_datum for o in locked_orders) if locked_orders else commit_start
    )
    dash_committed_end = last_committed_day(commit_end)

    print(f"[OK] {stats['scheduled_orders']} orders ingepland "
          f"({len(locked_orders)} vastgezet + {len(scheduled_free)} nieuw)")
    print(f"[INFO] ({stats['scheduled_units']:,} stuks)")

    if rejected:
        print(f"[WARN] {stats['rejected_orders']} orders kunnen niet op tijd geleverd worden")
        print(f"[INFO] ({stats['rejected_units']:,} stuks)")

    # Show solver stats if using OR-Tools
    if "solver_time_seconds" in stats:
        print(f"\n[SOLVER] OR-Tools solver stats:")
        print(f"      Status: {stats.get('solver_status', 'UNKNOWN')}")
        print(f"      Time: {stats.get('solver_time_seconds', 0):.2f}s")
        if stats.get("solver_gap") is not None:
            print(f"      Objective value: {stats['solver_gap']:.0f}")
        fb = stats.get("feedback")
        if fb:
            conv = "JA (0 gedropte orders)" if fb.get("converged") else "NEE (timeout/max iteraties)"
            print(f"      Feedback iteraties: {fb.get('iterations')}")
            print(f"      Geconvergeerd: {conv}")
            print(f"      Cuts toegevoegd: {fb.get('cuts_added')}")

    # Calculate actual utilization from schedules
    print(f"\n[METRICS] Capaciteitsbezetting (gemeten uit daadwerkelijke schedules):")

    def calculate_actual_utilization(scheds) -> tuple[float, float]:
        """
        Work minutes over available shift-window minutes, across the plan.

        Both halves come from src/config now — this used to hardcode
        900/870/750/720 (the solver's planning caps) while measuring elapsed
        span rather than work, which could push the result over 100%.
        """
        totals = {1: 0, 2: 0}
        capacity = {1: 0, 2: 0}
        for sched in scheds:
            for lijn in (1, 2):
                totals[lijn] += sched.kpis.get(f"lijn{lijn}_werk_min", 0)
                capacity[lijn] += config.window_minutes(sched.datum, lijn)

        return tuple(
            (totals[l] / capacity[l] * 100) if capacity[l] else 0.0 for l in (1, 2)
        )

    util_l1, util_l2 = calculate_actual_utilization(schedules)
    print(f"      L1: {util_l1:.1f}%")
    print(f"      L2: {util_l2:.1f}%")

    # ── Export to Excel ──
    print(f"\n[EXPORT] Exporteer naar Excel...")
    rejection_reasons = stats.get('rejection_reasons', {}) if rejected else {}
    export_multi_to_excel(
        schedules,
        str(output_file),
        assignment_warnings=[],
        product_warnings=product_warnings,
        rejected_orders=rejected if rejected else None,
        rejection_reasons=rejection_reasons,
    )
    print(f"[OK] Opgeslagen: {output_file}")

    # ── Dashboard (same solved plan — no second solve) ──
    print(f"\n[EXPORT] Genereer capacity dashboard...")
    from src.dashboard import build_dashboard_payload, render_dashboard_html
    payload = build_dashboard_payload(
        filtered_orders, scheduled, rejected, stats, schedules, products,
        plan_start, plan_end,
        committed_start=dash_committed_start,
        committed_end=dash_committed_end,
        completed_records=completed_orders,
        completed_source_orders=[
            o for o in all_orders if order_identity(o) in completed_orders
        ],
    )
    dashboard_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    render_dashboard_html(payload, Path(__file__).parent / "dashboard_template.html", dashboard_html)
    print(f"[OK] Opgeslagen: {dashboard_json}")
    print(f"[OK] Opgeslagen: {dashboard_html}")

    # ── Commit near-term weeks for the next run — frozen_until = this run's
    # own commit_end, so the NEXT run's solver is barred from every day
    # before it, not just the specific orders listed in `days`. ──
    committed_runs = sum(len(d["lijn1"]) + len(d["lijn2"]) for d in new_days.values())
    blockers = publication_blockers(solve_outcome, len(phantom) if 'phantom' in locals() else 0)
    for note in publication_notes(solve_outcome):
        print(f"[LOCK] let op (blokkeert niet): {note}")
    if args.dry_run:
        if blockers:
            print("\n[LOCK] --dry-run: NIETS vastgezet; deze uitkomst zou ook in een "
                  "gewone run NIET gepubliceerd worden:")
            for blocker in blockers:
                print(f"       - {blocker}")
        else:
            print(f"\n[LOCK] --dry-run: NIETS vastgezet. Een gewone run zou hier "
                  f"{len(new_days)} dagen ({committed_runs} runs) committen ({commit_start} t/m "
                  f"{last_committed_day(commit_end)}) en die weken bevriezen voor de "
                  f"volgende run. plan_state.json is ongewijzigd.")
    elif blockers:
        print("\n[LOCK] PLAN NIET GEPUBLICEERD; plan_state.json blijft ongewijzigd:")
        for blocker in blockers:
            print(f"       - {blocker}")
    elif solver_start >= commit_end:
        # This run's whole commit window was already frozen by an earlier run,
        # so it placed nothing inside it and `new_days` can only be a subset of
        # what is already committed — writing it would drop the rest. Happens
        # when the same week is re-run, or a week is planned out of order.
        print(f"\n[LOCK] Volledige commit-venster was al bevroren t/m "
              f"{last_committed_day(commit_end)} — plan_state.json blijft ongewijzigd.")
    else:
        print(f"\n[LOCK] Commit dagen {commit_start} t/m {commit_end} naar plan_state.json...")
        new_plan_state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "frozen_until": commit_end.isoformat(),
            "days": new_days,
            "completed_orders": completed_orders,
            "rejections": new_rejections,
        }
        save_plan_state(new_plan_state, plan_state_path)
        print(f"[OK] {len(new_days)} dagen ({committed_runs} runs) vastgezet, bevroren t/m "
              f"{last_committed_day(commit_end)} voor volgende run ({plan_state_path.name})")

    # ── Orders no future run will resolve ──
    # Ordinary capacity rejections may well place themselves next week as the
    # order book shifts; these will not, and are the only ones that need a
    # person. See src/exceptions_report.py.
    exception_rows = exceptions_report.build(
        rejected, products, stats.get("rejection_reasons", {})
    )
    if exception_rows:
        exceptions_csv = OUTPUT / f"actie_vereist_{label}.csv"
        exceptions_report.write_csv(exception_rows, exceptions_csv)
        exceptions_report.print_report(exception_rows)
        print(f"[OUTPUT] Actielijst: {exceptions_csv}")

    # ── Summary ──
    print(f"\n{'=' * 80}")
    print(f"[SUCCESS] PLANNING VOLTOOID!")
    print(f"{'=' * 80}")
    print_summary(schedules, [], [])

    print(f"\n[OUTPUT] Excel: {output_file}")
    print(f"[OUTPUT] Dashboard: {dashboard_html}")
    print(f"\n[INFO] Open het Excel-bestand voor de dag-per-dag planning, of het dashboard voor het weekoverzicht.")


if __name__ == "__main__":
    main()
