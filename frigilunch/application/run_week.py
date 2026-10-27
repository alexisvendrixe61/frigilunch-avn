"""
run_week.py — Thin assembler: wire paths and call the pipeline.

This is the single entry point called by the CLI and integration tests.
It constructs a PipelineContext with the standard path conventions and
delegates everything else to run_pipeline().
"""
from __future__ import annotations

from pathlib import Path

from frigilunch.application.pipeline import PipelineContext, run_pipeline


def run_week(
    week: int,
    year: int,
    data_dir: Path,
    output_dir: Path,
    reset: bool = False,
) -> PipelineContext:
    """Run the weekly production planning pipeline.

    Args:
        week:       ISO week number to plan.
        year:       ISO year.
        data_dir:   Root of the data directory (contains source_data/ and generated/).
        output_dir: Where to write planning_wkNN.xlsx, dashboard, action CSV.
        reset:      When True, ignore any existing plan_state.json (first run).

    Returns:
        The completed PipelineContext (outcome, warnings, etc. available on it).
    """
    ctx = PipelineContext(
        week=week,
        year=year,
        orders_path=data_dir / "source_data" / "orderverwerking en planning.xlsm",
        master_path=data_dir / "generated" / "products.json",
        state_path=data_dir / "generated" / "plan_state.json",
        output_dir=output_dir,
        reset=reset,
    )
    return run_pipeline(ctx)
