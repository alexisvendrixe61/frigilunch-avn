from datetime import date

from frigilunch.domain.state import publication_blockers, publication_notes
from src.models import PlanScore, RejectionDiagnostic, SolveOutcome


ZERO = PlanScore(0, 0, 0, 0, 0, 0)


def _outcome(status, diagnostics=()):
    return SolveOutcome(status, (), (), ZERO, tuple(diagnostics), {})


def test_complete_exact_result_is_publishable():
    assert publication_blockers(_outcome("complete")) == []


def test_genuine_late_after_freeze_exception_does_not_rewrite_frozen_days_or_block_roll():
    diagnostic = RejectionDiagnostic("late", "late_after_freeze")
    assert publication_blockers(_outcome("infeasible", [diagnostic])) == []


def test_unproven_rejections_are_reported_not_blocking():
    """15/10: oversubscription, oracle drops and unknown products are rejections
    the plan reports; the planned days are still valid, so the run commits."""
    diagnostics = [
        RejectionDiagnostic("x", "model_mismatch"),
        RejectionDiagnostic("y", "invalid_master_data"),
    ]
    outcome = _outcome("unknown", diagnostics)
    assert publication_blockers(outcome) == []
    assert len(publication_notes(outcome)) == 2


def test_final_render_drop_is_not_publishable():
    assert publication_blockers(_outcome("complete"), phantom_count=1)
