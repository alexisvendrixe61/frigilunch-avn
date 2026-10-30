# ADR 002 — Rolling 4-week commit window

**Status:** Accepted

## Context

The planner runs once a week and produces an open-horizon plan: every order with a known delivery date gets an attempted slot. Without a mechanism to preserve confirmed production, every run would be free to move any order — including ones the factory floor has already been briefed on, for which ingredients have been ordered, or for which the daily schedule has been printed.

A plan that changes completely each week is operationally useless.

## Decision

Each run commits the next `COMMIT_WEEKS = 4` weeks of its output to `data/generated/plan_state.json`. The schema records each committed day as a list of production runs with exact start/end times and changeover minutes, keyed by order identity.

The next run reads `plan_state.json` at stage 5 (apply freeze):
- Days whose production date is on or after this run's Monday are replayed byte-for-byte — the solver never sees them.
- Days in the past are aged out, and their orders are moved to `completed_orders`.
- The solver's horizon begins at `max(plan_start, frozen_until)` — it can only place orders on days not already committed.

`COMMIT_WEEKS` is defined in `frigilunch/domain/state.py`. The commit window is `[plan_start, plan_start + 4 weeks)`.

## Consequences

**Operational reliability.** Once a week is committed, the factory floor can rely on it. Four weeks gives enough lead time for ingredient procurement and staffing while keeping the rolling horizon fresh enough to absorb new orders.

**Late-arriving demand.** An order whose entire legal production window (delivery date minus minimum lead time) falls inside an already-frozen window has zero legal slots. The program cannot place it. It is reported in `actie_vereist_*.csv` as `ARRIVED-AFTER-FREEZE` and requires a human decision: expedite into the committed plan, move the delivery date, or add capacity.

**Testing requires `--reset`.** Running with `--dry-run` solves and exports but does not write `plan_state.json`. Running with `--reset` clears the existing freeze entirely. Without one of these flags, the solver's horizon shrinks each week — after four consecutive runs, the solver starts with almost no open days.

**Replay fidelity.** Frozen days are replayed from their persisted time slots, not re-derived by running the greedy walker on the current order set. Re-running the walker on a different order set is not guaranteed to reproduce the same sequence. This is enforced by `replay_locked_days` in `frigilunch/application/scheduler.py` (constraint X3).
