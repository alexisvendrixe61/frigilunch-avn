"""
cli.py — Command-line interface for the Frigilunch weekly planner.

Usage:
    python -m frigilunch.cli --week 40 --year 2026
    python -m frigilunch.cli --week 40 --year 2026 --reset
    python -m frigilunch.cli --week 40 --year 2026 --dry-run
    python -m frigilunch.cli --week 40 --year 2026 --warmup 4
    python -m frigilunch.cli --week 40 --year 2026 --render-only
    python -m frigilunch.cli --week 40 --year 2026 --suffix test
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from frigilunch.application.run_week import run_week
from frigilunch.domain.state import COMMIT_WEEKS, iso_weeks_before
from src.plan_state import load_plan_state  # src bridge — migrate with json_state.py


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Frigilunch weekly production planner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--week", type=int, required=True, help="ISO week number to plan")
    parser.add_argument("--year", type=int, default=datetime.now().year, help="ISO year")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Ignore existing plan_state.json and start from scratch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Solve and export but do NOT write plan_state.json. "
            "Safe for testing — nothing is committed."
        ),
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"Run the N preceding weeks first so the rolling lock is realistic. "
            f"Default: {COMMIT_WEEKS} on a cold start (empty or --reset), 0 otherwise. "
            "Cannot be combined with --dry-run."
        ),
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help=(
            "Re-render the HTML dashboard from the previous run's chart_data JSON. "
            "No solve, no Excel, no commit."
        ),
    )
    parser.add_argument(
        "--suffix",
        type=str,
        default=None,
        metavar="LABEL",
        help="Append a label to output filenames (e.g. planning_wk44_test.xlsx).",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Data root directory")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Output directory")
    return parser


def _run_warmup(weeks: list[tuple[int, int]], args: argparse.Namespace) -> None:
    """Run the N preceding weeks as subprocesses to build up the rolling lock."""
    for i, (y, w) in enumerate(weeks, start=1):
        cmd = [sys.executable, "-m", "frigilunch.cli",
               "--week", str(w), "--year", str(y), "--warmup", "0"]
        if args.reset and i == 1:
            cmd.append("--reset")
        if args.suffix:
            cmd += ["--suffix", args.suffix]
        print("\n" + "=" * 70)
        print(f"[WARMUP] {i}/{len(weeks)} — week {w} ({y})")
        print("=" * 70)
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[ERROR] Warmup run for week {w} failed (exit {rc}) — stopping.")
            sys.exit(rc)


def main() -> None:
    args = _build_parser().parse_args()

    data_dir = args.data_dir
    output_dir = args.output_dir
    state_path = data_dir / "generated" / "plan_state.json"

    # ── --render-only: re-render dashboard from last run's JSON, no solve ──
    if args.render_only:
        import json
        from src.dashboard import render_dashboard_html
        label = f"wk{args.week:02d}"
        if args.suffix:
            label = f"{label}_{args.suffix}"
        json_path = output_dir / f"chart_data_{label}.json"
        html_path = output_dir / f"capacity_dashboard_{label}.html"
        if not json_path.exists():
            print(f"[ERROR] {json_path} not found — run without --render-only first.")
            sys.exit(1)
        render_dashboard_html(
            json.loads(json_path.read_text(encoding="utf-8")),
            Path(__file__).parent.parent / "dashboard_template.html",
            html_path,
        )
        print(f"[OK] Re-rendered from {json_path.name}: {html_path}")
        return

    # ── validate flag combinations ──
    if args.dry_run and args.warmup:
        print("[ERROR] --dry-run and --warmup cannot be combined: warmup weeks must "
              "commit to build the rolling lock.\n"
              "        Run warmup first without --dry-run, then add --dry-run --warmup 0.")
        sys.exit(1)

    # ── warmup: run preceding weeks first ──
    warmup = args.warmup
    if warmup is None:
        try:
            state = load_plan_state(state_path)
            cold = args.reset or not state.get("days")
        except Exception:
            cold = True
        warmup = COMMIT_WEEKS if cold else 0

    if warmup:
        weeks = iso_weeks_before(args.year, args.week, warmup)
        print(f"\n[WARMUP] Cold start — running "
              f"{', '.join('wk' + str(w) for _y, w in weeks)} first, then week {args.week}.")
        print(f"[WARMUP] This is {warmup} extra run(s). Use --warmup 0 to skip "
              f"(faster but the target week will look fuller than it is).")
        _run_warmup(weeks, args)
        args.reset = False
        print("\n" + "=" * 70)
        print(f"[WARMUP] Done — now running the actual week {args.week}")
        print("=" * 70)

    run_week(
        week=args.week,
        year=args.year,
        data_dir=data_dir,
        output_dir=output_dir,
        reset=args.reset,
        dry_run=args.dry_run,
        suffix=args.suffix,
    )


if __name__ == "__main__":
    main()
