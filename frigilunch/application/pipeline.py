"""
pipeline.py — Orchestration layer for the weekly production planning pipeline.

Imports from domain/ and adapters/ only.  domain/ never imports from here.

Each stage function takes and returns a PipelineContext; side effects
(file reads/writes) are contained to the adapters called from within.

STAGES order:
    _load_master → _validate_master → _load_orders → _filter_orders
    → _apply_freeze → _run_solver → _commit_window → _export_outputs
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from frigilunch.adapters.json_state import read_state, write_state
from frigilunch.adapters.master_data import read_products, validate_master
from frigilunch.domain.benders import Solvers, run_benders
from frigilunch.domain.models import DaySchedule, Order, Product, SolveOutcome
from frigilunch.domain.oracle import schedule_day
from frigilunch.domain.state import freeze_window, load_frozen_orders, _order_key


# ── context ───────────────────────────────────────────────────────────────────

@dataclass
class PipelineContext:
    """Shared state threaded through each pipeline stage."""

    week: int
    year: int
    orders_path: Path
    master_path: Path
    state_path: Path
    output_dir: Path
    reset: bool = False

    # populated during pipeline execution
    products: dict[str, Product] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    frozen_orders: list[Order] = field(default_factory=list)
    free_orders: list[Order] = field(default_factory=list)
    plan_state: dict = field(default_factory=dict)
    outcome: SolveOutcome | None = None
    warnings: list[str] = field(default_factory=list)
    # dynamic planning window — set by _filter_orders
    plan_end: date | None = None
    commit_end: date | None = None
    solver_start: date | None = None
    # frozen day replay — set by _apply_freeze, consumed by _build_schedules
    locked_days: list = field(default_factory=list)
    locked_orders: list = field(default_factory=list)
    replayed_schedules: list = field(default_factory=list)


# ── stage helpers ─────────────────────────────────────────────────────────────

def _plan_start(ctx: PipelineContext) -> date:
    return date.fromisocalendar(ctx.year, ctx.week, 1)


def _plan_end(ctx: PipelineContext) -> date:
    """Return ctx.plan_end if set; fallback to 8 weeks from plan_start."""
    return ctx.plan_end if ctx.plan_end is not None else _plan_start(ctx) + timedelta(weeks=8)


def _exclusive_horizon_end(last_delivery: date) -> date:
    """First Monday strictly after the final delivery date (matches create_plan.py)."""
    end = last_delivery + timedelta(days=1)
    while end.weekday() != 0:
        end += timedelta(days=1)
    return end


def _stub_cp_solver(
    orders: list[Order],
    frozen: list[date],
    cuts: list[dict],
    config: dict,
) -> dict:
    """Stub CP solver: assigns each order to its existing productie_datum."""
    assignments = {
        o.order_nr: {"date": o.productie_datum, "lijn": o.lijn}
        for o in orders
    }
    return {"status": "feasible", "assignments": assignments}


# ── pipeline stages ───────────────────────────────────────────────────────────

def _load_master(ctx: PipelineContext) -> PipelineContext:
    """Stage 1: Load products from the generated products.json."""
    ctx.products = read_products(ctx.master_path)
    return ctx


def _validate_master(ctx: PipelineContext) -> PipelineContext:
    """Stage 2: Pre-flight checks — collect warnings for bad master data."""
    warns = validate_master(ctx.products, ctx.orders or [])
    ctx.warnings.extend(warns)
    for w in warns:
        print(f"[WARN] {w}")
    return ctx


def _load_orders(ctx: PipelineContext) -> PipelineContext:
    """Stage 3: Refresh orders.json from Excel, then load from JSON (src bridge)."""
    from src.order_filter import load_excel_orders, save_orders_to_json
    _repo = Path(__file__).parent.parent.parent
    orders_json = _repo / "data" / "generated" / "orders.json"
    fresh = load_excel_orders(ctx.orders_path, sheet_name="orderverwerking")
    save_orders_to_json(fresh, orders_json)
    ctx.orders = fresh
    print(f"[OK] {len(ctx.orders)} orders geladen")
    return ctx


def _filter_orders(ctx: PipelineContext) -> PipelineContext:
    """Stage 4: Filter to known-by-as-of and delivery window (src bridge)."""
    from src.order_filter import filter_by_delivery_range, filter_by_known_date
    from src.plan_state import commit_window
    plan_start = _plan_start(ctx)
    as_of = plan_start
    known, warns = filter_by_known_date(ctx.orders, as_of)
    for w in warns:
        print(f"[WARN] {w}")
    # Dynamic horizon: plan to the furthest known delivery (same as create_plan.py)
    future = [o.lever_datum for o in known if o.lever_datum >= plan_start]
    _, ctx.commit_end = commit_window(plan_start)
    ctx.plan_end = _exclusive_horizon_end(max(future)) if future else ctx.commit_end
    ctx.orders = filter_by_delivery_range(known, plan_start, ctx.plan_end)
    print(f"[OK] {len(ctx.orders)} orders na filtering op levervenster "
          f"(t/m {ctx.plan_end})")
    return ctx


def _apply_freeze(ctx: PipelineContext) -> PipelineContext:
    """Stage 5: Separate frozen (locked) from free orders using state (src bridge)."""
    from src.plan_state import partition_locked_days
    plan_start = _plan_start(ctx)
    if ctx.reset:
        ctx.plan_state = {}
    else:
        from src.plan_state import load_plan_state
        try:
            ctx.plan_state = load_plan_state(ctx.state_path)
        except Exception:
            ctx.plan_state = {}

    days_state = ctx.plan_state.get("days", {})
    completed_state = ctx.plan_state.get("completed_orders", {}) or {}
    ctx.locked_days, remaining, warnings = partition_locked_days(
        ctx.orders, days_state, today=plan_start, completed_state=completed_state,
    )
    for w in warnings:
        print(f"[WARN] {w}")
    ctx.locked_orders = [run.order for day in ctx.locked_days for run in day.runs]
    ctx.free_orders = remaining
    ctx.frozen_orders = ctx.locked_orders  # keep alias for downstream compat
    print(f"[OK] {len(ctx.locked_orders)} vastgezet over {len(ctx.locked_days)} dagen, "
          f"{len(ctx.free_orders)} vrij te plannen")
    return ctx


def _adapt_src_outcome(src_outcome: object) -> SolveOutcome:
    """Convert src.models.SolveOutcome to frigilunch.domain.models.SolveOutcome."""
    from frigilunch.domain.models import Order as DOrder, PlanScore as DScore

    def adapt_order(o: object) -> DOrder:
        return DOrder(
            order_nr=o.order_nr, art_nr=o.art_nr, klant=o.klant,
            aantal=o.aantal, lever_datum=o.lever_datum,
            productie_datum=o.productie_datum, lijn=o.lijn,
            plan_nr=getattr(o, "plan_nr", ""),
            opmerking=getattr(o, "opmerking", ""),
            opmerking_analyse=getattr(o, "opmerking_analyse", ""),
        )

    sc = src_outcome.score
    score = DScore(
        rejected_orders=sc.rejected_orders, rejected_units=sc.rejected_units,
        transition_minutes=sc.transition_minutes, scale_changes=sc.scale_changes,
        early_days=sc.early_days, moved_forecast_orders=sc.moved_forecast_orders,
    )
    return SolveOutcome(
        status=src_outcome.status,
        scheduled=tuple(adapt_order(o) for o in src_outcome.scheduled),
        rejected=tuple(adapt_order(o) for o in src_outcome.rejected),
        score=score,
        stats=src_outcome.stats,
    )


def _run_solver(ctx: PipelineContext) -> PipelineContext:
    """Stage 6: Run the real CP-SAT solver (falls back to stub if unavailable)."""
    _repo_root = Path(__file__).parent.parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

    try:
        from src.loader import load_products as _load_src, load_setup_times
        from src.plan_state import block_end
        from src.scheduler_ortools_v3 import assign_production_dates_feedback

        src_products, _ = _load_src(_repo_root / "data" / "generated" / "products.json")
        setup_matrix = load_setup_times(_repo_root / "data" / "setup_times.json")
        toestellen = _load_toestellen()
        plan_start = _plan_start(ctx)
        frozen_until = ctx.plan_state.get("frozen_until")
        solver_start = block_end(plan_start, frozen_until)
        ctx.solver_start = solver_start
        plan_end = _plan_end(ctx)
        commit_end = ctx.commit_end
        solver_weeks = max(1, -(-(plan_end - solver_start).days // 7))
        frozen_until_date = solver_start if solver_start > plan_start else None

        src_outcome = assign_production_dates_feedback(
            orders=ctx.free_orders,
            products=src_products,
            start_date=solver_start,
            weeks=solver_weeks,
            setup_matrix=setup_matrix,
            toestellen=toestellen,
            frozen_until=frozen_until_date,
            commit_end=commit_end,
            _mandatory_orders=False,
        )
        # Stash src-native scheduled list before adapting so _build_schedules
        # can pass them directly to schedule_all_days (same types it expects).
        src_outcome.stats["_src_scheduled"] = list(src_outcome.scheduled)
        ctx.outcome = _adapt_src_outcome(src_outcome)
    except Exception as exc:
        ctx.warnings.append(f"Real solver unavailable ({exc}); using stub")
        solvers = Solvers(cp=_stub_cp_solver, oracle=schedule_day)
        ctx.outcome = run_benders(
            orders=ctx.free_orders, products=ctx.products,
            solvers=solvers, config={"max_iterations": 5, "frozen_dates": []},
        )

    print(f"[OK] Solver: {ctx.outcome.status}, "
          f"{len(ctx.outcome.scheduled)} gepland, {len(ctx.outcome.rejected)} geweigerd")
    return ctx


def _commit_window(ctx: PipelineContext) -> PipelineContext:
    """Stage 8: Commit this week's schedule to plan_state.json (src bridge)."""
    if ctx.outcome is None:
        return ctx
    from src.plan_state import STATE_SCHEMA_VERSION, build_new_state, save_plan_state
    all_days = ctx.outcome.stats.get("days", [])
    plan_start = _plan_start(ctx)
    commit_end = ctx.commit_end
    if commit_end is None:
        return ctx

    solver_start = ctx.solver_start or plan_start
    if solver_start >= commit_end:
        print("[LOCK] commit-venster al bevroren — plan_state.json ongewijzigd")
        return ctx

    new_days = build_new_state(all_days, plan_start, commit_end)
    existing = ctx.plan_state
    new_state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "frozen_until": commit_end.isoformat(),
        "days": {**existing.get("days", {}), **new_days},
        "completed_orders": existing.get("completed_orders", {}),
        "rejections": {
            **existing.get("rejections", {}),
            **ctx.outcome.stats.get("rejection_reasons", {}),
        },
    }
    save_plan_state(new_state, ctx.state_path)
    ctx.plan_state = new_state
    committed_runs = sum(len(d["lijn1"]) + len(d["lijn2"]) for d in new_days.values())
    print(f"[OK] {len(new_days)} dagen ({committed_runs} runs) vastgezet t/m {commit_end}")
    return ctx


def _build_schedules(ctx: PipelineContext) -> PipelineContext:
    """Stage 7b: Replay frozen days + build new timelines, merge into full schedule."""
    if ctx.outcome is None:
        return ctx
    from src.loader import load_products, load_setup_times
    from src.multi_day_scheduler import schedule_all_days
    from src.scheduler import replay_locked_days
    _repo = Path(__file__).parent.parent.parent
    src_products, _ = load_products(_repo / "data" / "generated" / "products.json")
    setup_matrix = load_setup_times(_repo / "data" / "setup_times.json")
    toestellen = _load_toestellen()

    # Replay committed days from plan_state (byte-for-byte, not re-derived)
    replayed, replay_warns = replay_locked_days(ctx.locked_days, src_products)
    ctx.replayed_schedules = replayed
    for w in replay_warns:
        print(f"[WARN] {w}")

    # Build timelines for newly solved orders, with frozen days as prior context
    scheduled = ctx.outcome.stats.get("_src_scheduled", list(ctx.outcome.scheduled))
    derived = schedule_all_days(
        scheduled, src_products, setup_matrix, toestellen,
        force_optimal_sequence=True,
        voorgaand=replayed,
    )
    all_days = sorted(replayed + derived, key=lambda s: s.datum)
    ctx.outcome.stats["days"] = all_days
    ctx.outcome.stats.pop("_src_scheduled", None)
    print(f"[OK] {len(all_days)} dagschema's ({len(replayed)} replay + {len(derived)} nieuw)")
    return ctx


def _load_toestellen() -> dict:
    """Load device availability from capacity.json (src bridge)."""
    from src import config
    return config.device_availability()


def _export_outputs(ctx: PipelineContext) -> PipelineContext:
    """Stage 8: Write Excel, HTML dashboard, and action CSV (src bridge)."""
    if ctx.outcome is None:
        return ctx
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    label = f"wk{ctx.week:02d}"
    try:
        _export_excel(ctx, label)
        _export_dashboard(ctx, label)
        _export_action_csv(ctx, label)
    except Exception as exc:
        ctx.warnings.append(f"export skipped: {exc}")
        print(f"[WARN] export mislukt: {exc}")
    return ctx


def _export_excel(ctx: PipelineContext, label: str) -> None:
    """Write planning Excel via the original src exporter."""
    from src.exporter import export_multi_to_excel
    days = ctx.outcome.stats.get("days", [])
    path = ctx.output_dir / f"planning_{label}.xlsx"
    rejection_reasons = ctx.outcome.stats.get("rejection_reasons", {})
    rejected = list(ctx.outcome.rejected) or None
    export_multi_to_excel(
        days,
        str(path),
        assignment_warnings=[],
        product_warnings=[w for w in ctx.warnings if "speed" in w.lower() or "product" in w.lower()],
        rejected_orders=rejected,
        rejection_reasons=rejection_reasons,
    )
    print(f"[OK] Excel geschreven: {path}")


def _export_dashboard(ctx: PipelineContext, label: str) -> None:
    """Write HTML capacity dashboard via the original src dashboard."""
    import json
    from src.dashboard import build_dashboard_payload, render_dashboard_html
    from src.loader import load_products as _load_src_products
    _repo = Path(__file__).parent.parent.parent
    src_products, _ = _load_src_products(_repo / "data" / "generated" / "products.json")
    days = ctx.outcome.stats.get("days", [])
    all_orders = ctx.locked_orders + ctx.free_orders
    committed_start = ctx.plan_state.get("committed_start")
    committed_end = ctx.plan_state.get("committed_end")
    completed_records = ctx.plan_state.get("completed_orders", {})
    completed_source_orders = [
        o for o in all_orders
        if f"{o.order_nr}|{o.art_nr}" in completed_records
    ]
    # locked_orders + newly scheduled = full planned set (mirrors create_plan.py)
    all_scheduled = ctx.locked_orders + list(ctx.outcome.scheduled)
    payload = build_dashboard_payload(
        all_orders,
        all_scheduled,
        list(ctx.outcome.rejected),
        ctx.outcome.stats,
        days,
        src_products,
        _plan_start(ctx),
        _plan_end(ctx),
        committed_start=committed_start,
        committed_end=committed_end,
        completed_records=completed_records if completed_records else None,
        completed_source_orders=completed_source_orders if completed_source_orders else None,
    )
    json_path = ctx.output_dir / f"chart_data_{label}.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    html_path = ctx.output_dir / f"capacity_dashboard_{label}.html"
    render_dashboard_html(payload, _repo / "dashboard_template.html", html_path)
    print(f"[OK] Dashboard geschreven: {html_path}")


def _export_action_csv(ctx: PipelineContext, label: str) -> None:
    """Write action-required CSV via the original src exceptions report."""
    from src import exceptions_report
    path = ctx.output_dir / f"actie_vereist_{label}.csv"
    rows = exceptions_report.build(
        list(ctx.outcome.rejected),
        ctx.products,
        ctx.outcome.stats.get("rejection_reasons", {}),
    )
    if rows:
        exceptions_report.write_csv(rows, path)
        print(f"[OK] Actie CSV geschreven: {path}")


# ── public entry point ────────────────────────────────────────────────────────

STAGES: tuple[Callable, ...] = (
    _load_master,
    _load_orders,
    _validate_master,
    _filter_orders,
    _apply_freeze,
    _run_solver,
    _build_schedules,
    _commit_window,
    _export_outputs,
)


def run_pipeline(ctx: PipelineContext) -> PipelineContext:
    """Run all pipeline stages in order, threading context through each."""
    for stage in STAGES:
        ctx = stage(ctx)
    return ctx
