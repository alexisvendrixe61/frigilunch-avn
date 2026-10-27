"""
The registry has to be true, or it is worse than nothing — `docs/constraints.md`
spent a long while confidently describing rules that were enforced nowhere.

These tests are what make the catalogue trustworthy: every implementation path
it claims must resolve, no HARD rule may rely on VERIFY alone, and the
generated document must match the table it is generated from.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.constraints import registry
from src.constraints.registry import REGISTRY, Point, Severity, verify_registry

ROOT = Path(__file__).resolve().parent.parent


def test_every_claimed_implementation_actually_exists():
    """
    The self-check. A rename that misses the registry fails here rather than
    silently producing a lying document.
    """
    problems = verify_registry()
    assert problems == [], "\n".join(problems)


def test_ids_are_unique():
    ids = [c.id for c in REGISTRY]
    assert len(ids) == len(set(ids))


def test_no_hard_rule_is_enforced_only_at_verify():
    """
    VERIFY reports; it does not prevent. A HARD rule whose only enforcement is
    a post-hoc check is not enforced — that was PIPE-4 (day restrictions warned
    in the builder, errored in the validator, and were stopped by neither).
    """
    offenders = [
        c.id for c in REGISTRY
        if c.severity is Severity.HARD and c.point is Point.VERIFY and not c.backstop
    ]
    assert offenders == [], f"HARD rules with no preventing owner: {offenders}"


def test_unenforced_rules_claim_no_implementation():
    """A rule marked NONE must not point at code, or it reads as implemented."""
    for c in REGISTRY:
        if c.point is Point.NONE:
            assert not c.impl, f"{c.id} is NONE but claims impl {c.impl!r}"
            assert "NOT ENFORCED" in c.notes.upper(), (
                f"{c.id} is unenforced but its notes don't say so"
            )


def test_enforced_rules_all_point_somewhere():
    for c in REGISTRY:
        if c.point is not Point.NONE:
            assert c.impl, f"{c.id} claims point={c.point.value} but has no impl"


def test_rules_implemented_outside_the_package_say_why():
    """
    The guard against constraint logic drifting back out into the pipeline.
    A rule may live outside src/constraints/ — some genuinely must — but it has
    to state the reason, so the exceptions stay a short reviewed list instead
    of somewhere rules quietly accumulate.
    """
    unjustified = [
        c.id for c in REGISTRY
        if c.implemented and not c.inside_package and not c.outside_because
    ]
    assert unjustified == [], (
        f"implemented outside src/constraints/ with no justification: {unjustified}"
    )


def test_the_set_of_outside_rules_is_the_reviewed_one():
    """
    Pinned deliberately. Adding to this list should be a decision someone makes
    on purpose, not something a refactor does quietly — if this fails, either
    move the rule into the package or update the list and say why here.
    """
    outside = {c.id for c in REGISTRY if c.implemented and not c.inside_package}
    expected = {
        "C1", "K2",          # the day walk itself
        "A5",                # sate exclusivity: a window on one line, a cut-off on the other
        "O4", "O6",          # the CP-SAT day sequencer
        "O5", "M2", "X1", "S3", "A6",  # terms in the assignment solver's model
        "G1", "G2", "G3",    # G3: same schaal together, planner 15/10
        "X2",                # plan bookkeeping
        "X3",                # frozen-day replay
    }
    assert outside == expected, (
        f"newly outside: {outside - expected}  |  newly inside: {expected - outside}"
    )


def test_every_constraint_explains_itself():
    for c in REGISTRY:
        assert c.notes.strip(), f"{c.id} has no notes"
        assert c.name.strip(), f"{c.id} has no name"


def test_the_known_unenforced_rules_are_listed():
    """
    These have a field on `Product` and a column in the spreadsheet, so they
    look implemented. If one ever does get implemented, this test should fail
    and be updated — that is the point.
    """
    unenforced = {c.id for c in REGISTRY if c.point is Point.NONE}
    expected = {"L4", "L5", "A4", "D3", "D4", "P2", "P3", "V2"}
    assert expected <= unenforced, f"newly enforced (update the registry): {expected - unenforced}"


def test_generated_doc_is_in_sync_with_the_registry():
    """
    `docs/constraints.md` is output, not a source. If this fails, run:
        py -m src.constraints.registry
    """
    doc = ROOT / "docs" / "constraints.md"
    assert doc.exists(), "docs/constraints.md is missing — regenerate it"
    on_disk = doc.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected = registry.render_markdown().replace("\r\n", "\n")
    assert on_disk == expected, (
        "docs/constraints.md is stale — regenerate with `py -m src.constraints.registry`"
    )


def test_generated_doc_warns_about_unenforced_rules():
    md = registry.render_markdown()
    assert "## Not enforced" in md
    assert "NOT ENFORCED" in md
    # Spot-check one that is genuinely food-safety adjacent.
    assert "Halal isolation" in md
