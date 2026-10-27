"""Regression contracts for the normal open-horizon feedback mode."""

from src import scheduler_ortools_v3 as optimizer


def test_normal_run_uses_joint_solver_not_one_way_hard_first_phases():
    assert optimizer._TWO_PHASE_HARD_FIRST is False


def test_normal_run_does_not_lock_whole_clean_days_during_feedback():
    assert optimizer._INCREMENTAL_RESOLVE is False
