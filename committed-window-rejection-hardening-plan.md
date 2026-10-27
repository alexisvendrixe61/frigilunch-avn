# Blueprint: Committed-Window Rejection Hardening

## Executor Instructions

1. Read this file at the start of every execution loop.
2. Execute only the first unchecked step.
3. Run the stated verification before checking a step off.
4. Commit each completed step separately with `blueprint: <step title>`.
5. Do not alter an already-published day or production sequence.
6. Do not describe a rejection as physically impossible unless infeasibility was proven.

## Context

The week-40 dashboard contains seven rejected orders in the committed four-week
cycle. Three have no stored original cause, three are confirmed shared-device
conflicts, and one timed out without a proven cause. The rolling lock itself is
correct: published days must remain immutable. The defect is that an unexplained
omission can reach the frozen boundary, while shared-device conflicts are still
learned late through the exact day oracle instead of being avoided by assignment.

The current implementation already has plan-number identities, a completed-order
ledger, lexicographic placement priority, exact final-render validation, rejection
history, and publication blocking for unknown/model-mismatch results. This plan
hardens the boundary around the four committed weeks and adds targeted device-aware
repair without relaxing any production rule.

## Scope

**In scope:**

- Prevent a known order from silently disappearing before its eligible period freezes.
- Distinguish a genuinely late-arriving order from an order omitted by an earlier run.
- Persist an auditable cause for every rejected order by plan number.
- Repair exact shared-device conflicts before publication.
- Report search limits honestly instead of calling them proven overcapacity.

**Out of scope:**

- Moving or resequencing an already-published day.
- Changing product, allergen, weekday, line, position, or device rules.
- Automatically changing delivery dates or adding overtime.

## Architecture Decisions

- The four-week committed cycle is the safety boundary. The open tail remains
  provisional and may be re-optimized next week.
- Publication completeness is checked against the source order's known date and
  plan number, not only against the current solver input.
- Exact day scheduling remains the source of truth for device feasibility.
- Device repair moves free orders only; locked runs are immutable context.
- `infeasible` is reserved for a proof. A feasible incumbent with omitted orders is
  `search_limit` or `model_mismatch` and blocks publication.

## Plan

- [ ] **Step 1: Persist publication provenance**
      **Files:** `src/plan_state.py`, `tests/test_rolling_lock.py`
      **What:** Add `published_week`, `published_at`, and `known_order_cutoff` to the
      state schema. Keep backward-compatible loading. Store rejection records by
      `plan:<plan_nr>` with first/last attempted week and the exact original cause.
      **Verify:** `py -m pytest tests/test_rolling_lock.py -q`.

- [ ] **Step 2: Classify frozen omissions correctly**
      **Files:** `src/plan_state.py`, `create_plan.py`, `tests/test_late_arrival.py`
      **What:** Add a classifier with outcomes `late_arrival_after_publication`,
      `previously_rejected`, and `unexplained_published_omission`. Compare the order's
      known date with the prior state's cutoff. Never label a known-before-cutoff
      omission as ARRIVED-AFTER-FREEZE.
      **Verify:** tests cover all three outcomes and legacy state without provenance.

- [ ] **Step 3: Add the committed-window accounting gate**
      **Files:** `create_plan.py`, `src/totals.py`, `tests/test_publication_policy.py`
      **What:** Before saving state, require every known order whose last legal
      production day falls inside the committed cycle to be exactly one of locked,
      newly scheduled, completed, or rejected with a durable diagnostic. Block
      publication for unexplained omissions and identity overlap.
      **Verify:** a fixture modeled on plans 45476/45697/45698 cannot publish without
      an original cause; a genuinely late order is reported but does not mutate days.

- [ ] **Step 4: Make rejection wording proof-aware**
      **Files:** `src/scheduler_ortools_v3.py`, `src/exceptions_report.py`,
      `tests/test_scheduler_outcome.py`
      **What:** Replace “genuinely oversubscribed” whenever CP-SAT returned FEASIBLE,
      UNKNOWN, or hit its time limit. Emit `SEARCH-LIMIT` with attempted slot count.
      Use `PROVEN-INFEASIBLE` only after an infeasible mandatory probe or exhaustive
      targeted diagnostic.
      **Verify:** plan 45318-shaped test reports search limit, never proven capacity.

- [ ] **Step 5: Preserve and repair the exact incumbent after device drops**
      **Files:** `src/scheduler_ortools_v3.py`, `tests/test_feedback_optimizer.py`
      **What:** When the day oracle rejects an order for a shared device, retain all
      surviving day assignments as hints, add a day-scoped pair/device cut, and solve
      a repair neighborhood containing the dropped order plus movable orders on the
      conflicting days. Locked orders are fixed context.
      **Verify:** fixtures for Gernal+Leonard+Multipond and Multipond-only conflicts
      recover the dropped order when another legal day exists.
      **Interim fix landed (not a substitute for the above, left unchecked):**
      the feedback loop already retains hints and adds day-scoped device cuts
      (`global_conflict_pairs`) exactly as this step wants — that machinery
      pre-dates this plan. What it lacked was patience: the plateau/no-improve
      counter (`_NO_IMPROVE_STOP`, default 6) advanced on every non-improving
      iteration, including one that had just learned a brand-new device cut, so
      the loop could stop before the solver ever got to exploit that cut. Fixed
      by resetting the streak to 0 whenever `conflict_count > 0` on a
      non-improving iteration (`scheduler_ortools_v3.py`, feedback loop, the
      `else: no_improve_streak += 1` branch). Confirmed on a real run
      (`create_plan.py --week 40 --year 2026 --reset`): the wk36 warm-up phase
      used to plateau at iteration 8 (221/228 orders, 11 unresolved device
      conflicts) and now runs to iteration 10 and fully converges (228/228, 0
      conflicts). All 226 existing tests still pass. The actual repair-neighborhood
      solve (retarget the dropped order directly instead of waiting for the next
      full re-solve to stumble onto a clash-free day) and Steps 6-7's verified-day-
      pattern architecture are still open.

- [ ] **Step 6: Add exact device occupancy to committed-cycle candidates**
      **Files:** `src/scheduler_ortools_v3.py`, `src/scheduler.py`, create
      `src/verified_day_pattern.py`, `tests/test_verified_day_pattern.py`
      **What:** Represent a fully rendered two-line day as an immutable verified
      pattern containing order identities, line sequences, start/end times, device
      intervals, setup minutes, and score. A pattern is accepted only when
      `schedule_day` has zero drops and validation errors.
      **Verify:** overlapping use of a single device can never exist in a verified
      pattern; two non-overlapping uses can.

- [ ] **Step 7: Select verified patterns for the four committed weeks**
      **Files:** create `src/committed_pattern_solver.py`, modify `create_plan.py`,
      add `tests/test_committed_pattern_solver.py`
      **What:** Use CP-SAT to choose at most one verified pattern per day, covering
      each selected order once. Optimize lexicographically: rejected order count,
      rejected units, exact setup/changeover score, delivery proximity. Feed locked
      days as fixed patterns and allow every known order to use any legal unfrozen day.
      **Verify:** the week-40 seven-order regression fixture either places each order
      or returns a specific proven/limited diagnostic; no oracle drop can occur after
      pattern selection.

- [ ] **Step 8: Add week-40 regression acceptance**
      **Files:** `tests/fixtures/rolling_wk36_wk40/`,
      `tests/test_optimizer_benchmark.py`
      **What:** Add the seven committed-cycle orders with their plan numbers, device
      requirements, known dates, and prior state. Assert unique accounting, immutable
      locked days, preserved causes, zero final-render drops, and publication refusal
      for any unexplained omission.
      **Verify:** `py -m pytest tests/test_optimizer_benchmark.py -q`.

## Verification

Run `py -m pytest -q`, then execute week 36 through week 40 in an isolated directory.
For every run, hash all surviving locked day entries before and after. Week 40 passes
only when the committed-cycle dashboard count equals the order-level rejection list,
every row has a plan number and durable cause, no order overlaps scheduled/rejected,
and no result with `search_limit`, `model_mismatch`, or unexplained omission advances
`plan_state.json`.
