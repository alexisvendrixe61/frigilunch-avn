# Blueprint: Rolling Four-Week Production Optimizer

## Executor Instructions

You are executing this blueprint. Follow these rules:

1. Read this file first on every loop; after context compaction it is the ground truth.
2. Do only the next unchecked step.
3. Run the stated verification before changing `- [ ]` to `- [x]`.
4. Commit after each completed step with `git commit -m "blueprint: <step title>"`.
5. Never modify or unlock a previously published production day.
6. Follow the referenced code patterns and update this blueprint if discoveries affect later steps.
7. If blocked, record the reason under the step and continue only where doing so is independent and safe.

## Context

The business publishes a rolling four-week plan. For a run in ISO week `W`, the output contains weeks `W` through `W+3`, all fixed. The next weekly run drops the elapsed week, replays the remaining three weeks byte-for-byte, optimizes the newly opened fourth week and the still-open horizon, and publishes `W+1` through `W+4`. A published week is never automatically changed.

All orders known at the run's as-of date must be considered across the complete unfrozen horizon. Production may be placed substantially before delivery when that helps fit all work and creates efficient sequences. Delivery is a hard latest date, but the current 14-day earliest-production boundary is not the desired rule. The optimization priorities are lexicographic:

1. fit every known order before delivery;
2. minimize real production inefficiency, especially scale/format changes and other transition time;
3. among equally complete and efficient plans, produce closer to delivery;
4. avoid unnecessary movement in the unpublished forecast as a final tie-breaker.

The current code does not implement this hierarchy end-to-end. `src/constraints/slots.py::production_window` imposes a hard 14-day window from `data/capacity.json`; `src/scheduler_ortools_v3.py` assigns using approximate capacity and fixed buffers; separate hard/flexible phases reserve decisions one-way; and `src/day_sequencer.py` may subsequently drop orders. Feedback iterations are selected mainly by fitted order count rather than complete service plus exact changeover efficiency. Completed orders can also reappear after their production day ages out of state.

## Scope

**In scope:**

- Preserve the rolling four-week immutable publication model exactly.
- Make all known orders eligible on any legal, unfrozen production day no later than delivery.
- Make zero rejected orders the primary feasibility target.
- Optimize exact scale/format/product transitions after full placement is achieved.
- Use delivery proximity only after completeness and efficiency.
- Let all unfrozen orders be rearranged together; remove one-way optimizer phases.
- Prevent exact day construction from silently invalidating an assignment.
- Persist completed identities so fulfilled orders cannot return as demand.
- Distinguish proven infeasibility, search timeout, late arrival, invalid data, and model mismatch.
- Benchmark rolling week-36 through week-40 snapshots.

**Out of scope:**

- Automatically changing a previously published/frozen day.
- Inserting a late order into a frozen week without a manual business decision.
- Treating delivery proximity as more important than fitting work or reducing changeovers.
- Relaxing allergen, device, position, line, staffing, or calendar rules to make a result fit.
- Editing the existing week-40 planning during implementation.

## Architecture Decisions

1. **Four-week freeze is unconditional.** `plan_state.json` remains the source of immutable published days. A run replays the surviving three weeks and adds exactly one newly fixed week.
2. **No generic maximum earliness.** A known order may use any unfrozen legal workday up to its delivery cutoff. A true shelf-life limit may later be defined per product; generic delivery closeness is soft only.
3. **Completeness is a feasibility requirement.** The primary solve makes every eligible order mandatory. Only if CP-SAT proves `INFEASIBLE` does a diagnostic relaxation identify the smallest unmet set. `UNKNOWN` or timeout is never reported as proven capacity infeasibility.
4. **Efficiency uses exact transitions.** The objective uses the same scale/format/product transition function as final sequencing, not a flat buffer.
5. **One objective hierarchy everywhere.** Assignment, exact sequencing, incumbent selection, device conflict resolution, and reporting use one lexicographic score.
6. **No final-stage dropping.** A plan is publishable only when every assigned order passes exact day/device/saté/position validation. A failed day feeds a new constraint or repair search.
7. **Optimize the complete open horizon; publish only four weeks.** Assignments beyond the fixed block are forecasts and may be reoptimized next week, but let the solver reserve future capacity and choose efficient early production.
8. **Late arrivals remain exceptions.** If an order becomes known only after all pre-delivery days are frozen, no compliant automatic placement exists.

## Interfaces and Models

Add these internal dataclasses following `src/models.py`:

```python
@dataclass(frozen=True, order=True)
class PlanScore:
    rejected_orders: int
    rejected_units: int
    transition_minutes: int
    scale_changes: int
    early_days: int
    moved_forecast_orders: int

@dataclass(frozen=True)
class SolveOutcome:
    status: Literal["complete", "infeasible", "unknown"]
    scheduled: tuple[Order, ...]
    rejected: tuple[Order, ...]
    score: PlanScore
    diagnostics: tuple["RejectionDiagnostic", ...]

@dataclass(frozen=True)
class RejectionDiagnostic:
    order_key: str
    category: Literal[
        "late_after_freeze", "invalid_master_data", "proven_infeasible",
        "search_limit", "model_mismatch",
    ]
    attempted_slots: tuple["AttemptedSlot", ...]
    blocking_constraints: tuple[str, ...]
    detail: str

@dataclass(frozen=True)
class CompletedOrderRecord:
    order_key: str
    plan_nr: int | None
    production_date: date
    delivery_date: date
    line: int
    units: int
```

`PlanScore` compares fields lexicographically in the order shown. In the primary mandatory solve both rejection fields are zero. `transition_minutes` comes from the canonical setup function. If the business later chooses a different diagnostic trade-off between rejected orders and units, change it in one score-policy module only.

## Plan

- [x] **Step 1: Lock down the rolling four-week invariant**
      **Files:** `tests/test_rolling_lock.py` (modify)
      **What:** Add runs for W, W+1, and W+2. Assert each published state contains exactly four weeks including the run week; surviving days replay byte-for-byte; only the new fourth week is newly solved; no order, sequence, time, or line in surviving weeks changes.
      **Verify:** `py -m pytest -q tests/test_rolling_lock.py`

- [x] **Step 2: Specify unrestricted early-production eligibility**
      **Files:** `tests/test_late_arrival.py`, `tests/test_config.py` (modify)
      **What:** Prove a known order may be scheduled more than 14 days early on an unfrozen day, delivery remains a hard latest bound, and an order first known after all pre-delivery dates freeze is `late_after_freeze`.
      **Verify:** `py -m pytest -q tests/test_late_arrival.py tests/test_config.py`

- [x] **Step 3: Remove the generic hard 14-day lower bound**
      **Files:** `src/config.py`, `src/constraints/slots.py`, `data/capacity.json` (modify)
      **What:** Make the earliest candidate the open-horizon start unless a future explicit per-product shelf-life rule exists. Keep `min_days_before_delivery` as the hard latest offset. Replace `max_days_before_delivery` with `delivery_proximity_grace_days` and use it only in the objective.
      **Verify:** `py -m pytest -q tests/test_late_arrival.py tests/test_config.py`

- [x] **Step 4: Persist completed identities across rolling runs**
      **Files:** `src/plan_state.py`, `tests/test_rolling_lock.py` (modify)
      **What:** Version state and serialize `CompletedOrderRecord`. When a published day passes, retain its order identities through delivery and exclude them from free demand. Prefer `plan_nr`; migrate the composite identity. Reproduce article `64224100`.
      **Verify:** `py -m pytest -q tests/test_rolling_lock.py`

- [x] **Step 5: Wire completed records through plan creation**
      **Files:** `create_plan.py` (modify)
      **What:** Load, carry, expire, and save the completed ledger while preserving the exact four-week `days` block. Show completed orders as fulfilled using the original production date and never feed them back to the optimizer.
      **Verify:** Run the rolling tests and two consecutive fixture dry-runs; dry-run must not change input/state files.

- [x] **Step 6: Define one canonical lexicographic score**
      **Files:** `src/models.py` (modify), `src/plan_score.py`, `tests/test_plan_score.py` (create)
      **What:** Add `PlanScore` plus pure calculators. Test that any complete plan beats every incomplete plan; then exact transition efficiency wins; only then lower earliness and forecast movement win.
      **Verify:** `py -m pytest -q tests/test_plan_score.py`

- [x] **Step 7: Make exact transition cost canonical**
      **Files:** existing module owning `calculate_setup_time`, `src/day_sequencer.py`, `tests/test_day_build.py` (modify)
      **What:** Expose one transition API covering scale/format changes and other setup rules. Make sequencing and `PlanScore` use it. Cover same scale, different scale, asymmetric transitions, line changes, and day start.
      **Verify:** `py -m pytest -q tests/test_day_build.py tests/test_plan_score.py`

- [x] **Step 8: Add mandatory-order solve status**
      **Files:** `src/scheduler_ortools_v3.py`, `src/models.py` (modify), `tests/test_scheduler_outcome.py` (create)
      **What:** Return `SolveOutcome`. Require every order with a legal slot in the primary solve. Preserve CP-SAT `INFEASIBLE`, `FEASIBLE/OPTIMAL`, and `UNKNOWN`; never turn timeout into `NEVER-PLACED`.
      **Verify:** `py -m pytest -q tests/test_scheduler_outcome.py`

- [x] **Step 9: Add an infeasibility diagnostic relaxation**
      **Files:** `src/scheduler_ortools_v3.py`, `tests/test_scheduler_outcome.py` (modify)
      **What:** Only after proven primary infeasibility, solve with optional presence minimizing rejected orders then units. Return the sacrificed set as evidence, not as the normal objective.
      **Verify:** `py -m pytest -q tests/test_scheduler_outcome.py`

- [x] **Step 10: Stop freezing clean days inside the open solver**
      **Files:** `src/scheduler_ortools_v3.py` (modify), `tests/test_feedback_optimizer.py` (create)
      **What:** Replace whole-day `stable_days` exclusion with fixed placements or per-line residual context. A half-empty clean day remains usable after a later displacement. Published days stay separately immutable.
      **Verify:** `py -m pytest -q tests/test_feedback_optimizer.py tests/test_rolling_lock.py`

- [x] **Step 11: Remove one-way hard/flexible phase commitment**
      **Files:** `src/scheduler_ortools_v3.py`, `tests/test_feedback_optimizer.py` (modify)
      **What:** Solve restricted and flexible orders together, or add a cross-phase neighborhood that may move both groups before publication. Test a case where moving a restricted order lets all work fit.
      **Verify:** `py -m pytest -q tests/test_feedback_optimizer.py`

- [x] **Step 12: Use exact feedback without dropping orders**
      **Files:** `src/scheduler_ortools_v3.py`, `src/day_sequencer.py`, `tests/test_feedback_optimizer.py` (modify)
      **What:** A failed exact day returns an evidenced day/line/blocker cut and triggers re-solve. Never publish an incumbent from which the day builder removed an assigned order.
      **Verify:** `py -m pytest -q tests/test_feedback_optimizer.py tests/test_day_build.py`

- [x] **Step 13: Optimize scale/setup efficiency after feasibility**
      **Files:** `src/scheduler_ortools_v3.py`, `src/day_sequencer.py` (modify), `tests/test_optimizer_objective.py` (create)
      **What:** Improve complete plans using canonical transition cost. Apply lexicographic stages or safe dominating weights: zero rejects, transition minutes/scale changes, early days, forecast movement. Every accepted improvement remains exactly feasible.
      **Verify:** `py -m pytest -q tests/test_optimizer_objective.py tests/test_plan_score.py`

- [x] **Step 14: Model devices and saté consistently during repair**
      **Files:** `src/scheduler_ortools_v3.py`, `src/scheduler.py`, `tests/test_feedback_optimizer.py`, `tests/test_sate_exclusive.py` (modify)
      **What:** Include device intervals and saté windows in validation. Make observed device cuts day-scoped unless intrinsically global. Let repair move any unfrozen blocker. Cover Gernal/Multifill and cross-line saté.
      **Verify:** `py -m pytest -q tests/test_feedback_optimizer.py tests/test_sate_exclusive.py`

- [x] **Step 15: Preserve position-rule causality**
      **Files:** `src/scheduler_ortools_v3.py`, `tests/test_s2_master_constraint.py`, `tests/test_positie_regels.py` (modify)
      **What:** Distinguish no eligible opener/closer from a companion lost to time/device/saté. Feed companion loss back into search rather than dropping the dependent order. Correct stale diagnostic text.
      **Verify:** `py -m pytest -q tests/test_s2_master_constraint.py tests/test_positie_regels.py`

- [x] **Step 16: Add structured rejection diagnostics**
      **Files:** `src/models.py`, `src/exceptions_report.py`, `src/dashboard.py`, `tests/test_exceptions_report.py` (modify)
      **What:** Report `late_after_freeze`, `invalid_master_data`, `proven_infeasible`, `search_limit`, or `model_mismatch`, with plan number, candidate days, attempted slots, exact blockers, and proof status. Preserve the original cause across later freezes.
      **Verify:** `py -m pytest -q tests/test_exceptions_report.py`

- [x] **Step 17: Surface critical product-data inference**
      **Files:** `src/product_setup_loader.py`, `create_plan.py` (modify), `tests/test_product_setup_loader.py` (create or modify)
      **What:** Preserve explicit/missing/inferred provenance for line, weekday, speed, position, allergen, and device fields. Carry active warnings into output. Do not relax real rules to make a plan fit.
      **Verify:** `py -m pytest -q tests/test_product_setup_loader.py`

- [x] **Step 18: Build rolling-horizon benchmark fixtures**
      **Files:** `tests/fixtures/rolling_wk36_wk40/`, `tests/test_optimizer_benchmark.py` (create)
      **What:** Create sanitized deterministic snapshots for weeks 36-40. Record known-at-run orders, immutable state, placements/rejections, units, exact transition minutes, scale changes, early days, solver status, and runtime. Include the seven investigated week-40 orders.
      **Verify:** `py -m pytest -q tests/test_optimizer_benchmark.py`

- [x] **Step 19: Add publication acceptance checks**
      **Files:** `create_plan.py`, `tests/test_optimizer_benchmark.py` (modify)
      **What:** Assert every scheduled order survives exact rendering and each unscheduled order has a structured category. Publish the four-week block when the only exception is genuinely late-after-freeze; treat `UNKNOWN`, model mismatch, or unexplained loss as a failed planner run, not a valid result.
      **Verify:** `py -m pytest -q tests/test_optimizer_benchmark.py tests/test_rolling_lock.py`

- [x] **Step 20: Replay and tune without changing priorities**
      **Files:** `data/capacity.json` (solver budgets only), benchmark artifact under `tests/fixtures/rolling_wk36_wk40/`
      **What:** Replay all fixture weeks. Tune time limits, workers, and neighborhoods only after correctness. Never trade a rejected order for fewer setup minutes. Record before/after completion, transitions, scale changes, earliness, status, and runtime.
      **Verify:** `py -m pytest -q tests/test_optimizer_benchmark.py; py -m pytest -q`

## Tests

Critical behavior:

- Week W publishes W..W+3; W+1 replays W+1..W+3 exactly and adds only W+4.
- A known order may be produced more than 14 days early.
- A complete plan always beats an incomplete plan.
- Between complete plans, fewer exact scale/setup changes wins even when production is earlier.
- Delivery proximity breaks ties only after completeness and efficiency.
- Restricted and flexible orders can be rearranged together anywhere unfrozen.
- Exact rendering never drops an order from a publishable result.
- Timeout reports `unknown`; only a proof reports `infeasible`.
- Completed work never resurrects.
- Genuine late arrivals never change frozen weeks.

## Verification

```powershell
py -m pytest -q tests/test_rolling_lock.py tests/test_late_arrival.py tests/test_config.py
py -m pytest -q tests/test_plan_score.py tests/test_scheduler_outcome.py tests/test_feedback_optimizer.py
py -m pytest -q tests/test_day_build.py tests/test_optimizer_objective.py tests/test_sate_exclusive.py
py -m pytest -q tests/test_s2_master_constraint.py tests/test_positie_regels.py tests/test_exceptions_report.py
py -m pytest -q tests/test_product_setup_loader.py tests/test_optimizer_benchmark.py
py -m pytest -q
```

End-to-end acceptance requires immutable rolling four-week output across consecutive runs; every known legally feasible order in the exact rendered plan; transition efficiency improved only among complete plans; delivery proximity optimized last; and every exception proved late-after-freeze, invalid data, infeasible, or honestly search-limited. Article `64224100` must remain fulfilled after its completed day ages out.
