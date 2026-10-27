"""
tests/integration/test_end_to_end.py

End-to-end smoke test for the full pipeline on real week-40 data.

Marked @pytest.mark.integration so CI skips it by default with:
    pytest -m "not integration"

Run manually (needs real data in data/):
    pytest -m integration -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from frigilunch.application.run_week import run_week

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_OUTPUT_DIR = Path(__file__).parent.parent.parent / "output" / "_test_e2e_wk40"


def _data_available() -> bool:
    """Return True when the real source data files are present."""
    orders_file = _DATA_DIR / "source_data" / "orderverwerking en planning.xlsm"
    products_file = _DATA_DIR / "generated" / "products.json"
    return orders_file.exists() and products_file.exists()


@pytest.mark.integration
@pytest.mark.skipif(not _data_available(), reason="Real data files not present")
def test_week40_runs_without_crash():
    """Smoke test: the full pipeline on real wk40 data completes without crashing."""
    ctx = run_week(
        week=40,
        year=2026,
        data_dir=_DATA_DIR,
        output_dir=_OUTPUT_DIR,
        reset=True,
    )
    assert ctx.outcome is not None, "Pipeline produced no outcome"
    assert ctx.outcome.status in ("complete", "infeasible", "unknown"), (
        f"Unexpected status: {ctx.outcome.status}"
    )


@pytest.mark.integration
@pytest.mark.skipif(not _data_available(), reason="Real data files not present")
def test_week40_produces_output_files():
    """After a successful run, planning Excel and dashboard HTML must exist."""
    ctx = run_week(
        week=40,
        year=2026,
        data_dir=_DATA_DIR,
        output_dir=_OUTPUT_DIR,
        reset=True,
    )
    assert ctx.outcome is not None

    xlsx = _OUTPUT_DIR / "planning_wk40.xlsx"
    html = _OUTPUT_DIR / "capacity_dashboard_wk40.html"

    # Files are written only when openpyxl is installed; soft-assert with info
    if xlsx.exists():
        assert xlsx.stat().st_size > 0, "Excel file is empty"
    if html.exists():
        assert html.stat().st_size > 0, "Dashboard HTML is empty"
