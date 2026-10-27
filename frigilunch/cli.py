"""
cli.py — Command-line interface for the Frigilunch weekly planner.

Usage:
    python -m frigilunch.cli --week 40 --year 2026
    python -m frigilunch.cli --week 40 --year 2026 --reset
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from frigilunch.application.run_week import run_week


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Frigilunch weekly production planner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--week", type=int, required=True, help="ISO week number to plan")
    parser.add_argument("--year", type=int, default=datetime.now().year, help="ISO year")
    parser.add_argument("--reset", action="store_true", help="Ignore plan_state.json")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Data root directory")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Output directory")
    return parser


def main() -> None:
    """Parse arguments and run the weekly planning pipeline."""
    args = _build_parser().parse_args()
    run_week(
        week=args.week,
        year=args.year,
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        reset=args.reset,
    )


if __name__ == "__main__":
    main()
