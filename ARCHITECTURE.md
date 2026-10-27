# Frigilunch scheduler — how it works

## What it does

Takes incoming orders (art. number, quantity, delivery date) and assigns each
one a **production date + line** (L1 or L2), respecting capacity, allergen,
equipment, and per-product day/line rules — then builds an actual
minute-by-minute daily timeline and exports it to Excel and the capacity
dashboard.

The horizon is open-ended by default (out to the furthest known delivery date);
`--weeks N` caps it. Independently of the horizon, each run **commits** the
next `COMMIT_WEEKS` (4) weeks of its own output and freezes those days for the
next run — see `src/plan_state.py` and the README.

## Pipeline

```
data/source_data/*.xlsx ──▶ products.json / orders.json      (loader.py)
                                     │
                                     ▼
                     assign_production_dates_feedback()       (scheduler_ortools_v3.py)
                     "which day + which line for each order"
                                     │
                                     ▼
                          schedule_all_days()                 (multi_day_scheduler.py)
                     "build the real per-day timeline"
                            │                  │
                            ▼                  ▼
                  export_multi_to_excel()   build_dashboard_payload()
                  output/planning_*.xlsx    output/capacity_dashboard_*.html
```

One entry point: `create_plan.py --week N --year Y`.

`gen_chart_data.py` used to be a second one that ran the pipeline *without* the
rolling lock, on a hardcoded 10-week horizon and with no reconciliation — so it
produced a different plan for the same week and wrote it beside the real one
under a near-identical filename. `scheduler_ortools_v3.py`'s `__main__` block
shelled out to it, which meant running the solver module directly silently
produced the non-locking plan. Both are gone; `--render-only` on the single CLI
covers the one thing that script could do that this one could not.

## File map

| File | Role |
|---|---|
| `src/models.py` | `Order`, `Product`, `PlannedOrder`, `DaySchedule` dataclasses — the shared vocabulary |
| `src/loader.py` | Reads `products.json` / `capacity.json` / `setup_times.json` into those dataclasses |
| `src/order_filter.py` | Filters `orders.json` down to the delivery-date window being planned |
| `src/scheduler_ortools_v3.py` | **The optimizer.** Decides day + line per order. See below. |
| `src/scheduler.py` | `schedule_day` — builds one real day's minute-by-minute timeline (setups, sequencing, device conflicts) for a *given* set of orders. Used both as the optimizer's oracle and for the final export. |
| `src/multi_day_scheduler.py` | Runs `schedule_day` across every day in the window; produces the `DaySchedule` list used for export/dashboard |
| `src/config.py` | **The one reader of `capacity.json`**, and the one definition of a production day: line start times, shift ends, and the planning caps derived from them |
| `src/constraints/` | **Every business rule, indexed by `registry.py`.** Each declares the single point it is enforced at (FILTER / BUILD / VERIFY / NONE); the registry is self-checking and generates `docs/constraints.md` |
| `src/constraints/slots.py` | The FILTER point: every "may not run there" rule in one function — production window, workdays, allowed weekdays, line permission, the frozen zone |
| `src/totals.py` | The one calculation of how a plan did, shared by Excel, dashboard and stdout |
| `src/exceptions_report.py` | The rejections no future run will resolve, and what to do about each |
| `src/day_sequencer.py` | CP-SAT "does SOME sequence of these orders fit?" second opinion for one line's day. Called by `schedule_day` when the greedy walk drops something, and on every committed-window day at final render |
| `src/plan_state.py` | The rolling commit/lock: what a run freezes, what the next run must leave alone |
| `src/validator.py` | Post-hoc assertions over a built `DaySchedule` — reports, never changes |
| `src/exporter.py` | `DaySchedule` list → formatted Excel workbook |
| `src/dashboard.py` | Solved plan → dashboard payload → rendered HTML |
| `docs/constraints.md` | GENERATED from `src/constraints/registry.py`. Do not hand-edit |

## How the optimizer works (`assign_production_dates_feedback`)

The hard part: setup time between two orders on the same day depends on what
*sequence* they run in (scale change, mold change, allergen cleaning) — that's
`schedule_day`'s job, and it's too complex to encode inside a solver directly.
So this isn't one monolithic model — it's a **feedback loop** (logic-based
Benders decomposition) that alternates between a *fast approximate* solver and
the *real, exact* daily scheduler:

```
1. SOLVE   — CP-SAT picks (day, line) for every order, treating each day's
             capacity as: sum(order durations + a flat per-order setup buffer) ≤ day capacity.
             This is an approximation — the buffer is a guess, not the real setup cost.
2. ORACLE  — For every day that has orders assigned, run schedule_day() for real.
             It applies actual sequence-dependent setups and may DROP orders
             that don't actually fit once real setups are counted.
3. CUT     — For every day that dropped orders, add a constraint forbidding
             that exact over-full combination ("no-good cut") and re-solve.
4. REPEAT  — until a solve iteration drops nothing (converged) or it plateaus
             for 6 iterations with no improvement, or the time budget runs out.
```

The **best** iteration (most orders that actually fit, per the oracle) is kept
— not necessarily the last one, since CP-SAT's solution can vary between
iterations as cuts accumulate.

### What the solver is actually optimizing

Each order gets a slot `(day, line)` chosen from only its *legal* slots
(within its delivery-window, on an allowed weekday, on a line it's permitted
to run on). The objective minimizes, in order of weight:
- **10,000** per order left completely unscheduled (this dominates — the
  solver will always prefer lateness/splitting over dropping an order)
  Weighted by order size (`10000 + FRIGI_UNSCHEDULED_UNIT_WEIGHT × aantal`),
  so dropping a big order costs strictly more than dropping a small one.
- **`FRIGI_LATE_PENALTY` (100) × days late.** Producing after the delivery date
  always loses to producing before it.
- **`FRIGI_EARLY_PENALTY` (1) × days early, beyond a `FRIGI_EARLY_GRACE_DAYS`
  (7) free window.** Early production inside the grace window is free — there
  is no reason to avoid idle capacity that sits before a cluster of deadlines.
  (`capacity.json` used to describe this as a flat penalty of 10/day; that was
  measured to fit *fewer* orders and was changed.)
- Small **consolidation** penalties per activated (day, line), per active day,
  and per (day, line, scale-format family) — these only ever decide ties, but
  they are what pushes same-format orders onto the same line so the day
  sequencer can find 20-min instead of 30-min changeovers.
- a smaller penalty per extra chunk, for orders that get split (see below)

### Order splitting

Most orders are a single indivisible "chunk". An order only becomes
split-eligible if it **cannot physically fit in a single day even in the best
case** — its duration exceeds the largest day/line capacity it could ever land
on. (An earlier rule used ≥35% of the smallest day's capacity, which flagged
a third of the order book for machinery almost none of it needed.) The
reconciliation and phase-3 retry paths can additionally force eligibility for
the small batch that already failed twice. Eligible orders get up to
`FRIGI_MAX_CHUNKS` (default 5) pieces, each ≥`FRIGI_MIN_CHUNK_MIN` minutes (default 60), that can
land on different days/lines; the solver still only "counts" the order as
scheduled if **all** of its chunks survive the oracle — no partial delivery.

### Line assignment, not just day assignment

The optimizer decides the **line** too, not only the day — `Order.lijn` (1 or
2) is passed into `schedule_day`, which obeys it instead of doing its own
load-balancing. `schedule_day` builds one line first and then **staggers**
the other around its use of any scarce (single-unit) shared equipment, so the
two lines can never be assigned the same 1-unit machine at the same time by
construction — this is what keeps device conflicts at zero.

Which line is built first is not fixed. It decides which line absorbs the
whole cost of sharing: the second line is only ever pushed later, and past
closing time its orders are dropped. On a day where both lines want the same
single-unit machine, the day is therefore built both ways round and the
version carrying more orders is the one kept (a tie keeps L1 first). See
`scheduler._build_line_pair`.

### Known non-determinism

CP-SAT search order plus the per-iteration wall-clock budget
(`feedback_iter_timeout_seconds`) mean re-running the *same* input can land a
few orders differently each time (observed: 353–360 scheduled out of ~445).
Never treat a single run's exact count as the definitive answer — see
[[frigilunch-scheduler-nondeterminism]] in memory.

## Key tunables

| Where | What |
|---|---|
| `data/capacity.json` → `or_tools_solver` | `feedback_timeout_seconds` (total budget), `feedback_iter_timeout_seconds` (per CP-SAT call) |
| `data/capacity.json` → `toestellen_beschikbaarheid` | Equipment unit counts (≥2 units never conflict) |
| env `FRIGI_MIN_CHUNK_MIN` (60) / `FRIGI_MAX_CHUNKS` (5) | Order-splitting floor/ceiling |
| — | Split eligibility is no longer a size fraction. An order is split-eligible only if its duration exceeds the largest single day it could ever land on (`max_single_day_cap`), or if a caller forces it (the phase-3 retry batch). `FRIGI_SPLIT_CAP_FRACTION` no longer exists |
| env `FRIGI_TWO_PHASE_HARD_FIRST` (1) / `FRIGI_RETRY_SPLIT_NEVER_PLACED` (1) | Three-phase solve: line-restricted first, then flexible, then a forced-split retry |
| env `FRIGI_INCREMENTAL_RESOLVE` (1) | Lock in days that came back clean instead of re-deciding them each iteration |
| env `FRIGI_OPTIMAL_SEQUENCE` (1) | Let the CP-SAT day sequencer try to recover greedy drops |
| env `FRIGI_PROXY_BUFFER` (30) | Per-order setup-time guess used in step 1's approximate capacity check (real average ~20; measured A/B in `scheduler_ortools_v3.py`) |
| env `FRIGI_DISABLE_SPLIT` | Force whole-order-only, for A/B comparison |

## Further reading (project memory, not files)

- `frigilunch-scheduler-architecture` — this doc supersedes it; kept for history
- `frigilunch-rejection-causes` — why orders get rejected in practice (day/line
  restrictions, Friday capacity, genuine demand clustering vs. recoverable packing)
- `frigilunch-scheduler-nondeterminism` — why re-runs vary, and why not to quote one run
- `frigilunch-capacity-dashboard` — how the dashboard is generated

## Reconciliation, and the fact that it currently runs twice

Each solver phase verifies its own orders against its own oracle; the *other*
phases are represented to it only as a reserved-minutes budget, never as real
orders. `reconcile_combined_schedule` closes that gap by running the real
combined day schedule and retrying whatever drops.

`_assign_two_phase_hard_first` already ends with a `reconcile_combined_schedule`
call, and `create_plan.py` then calls it **again** on the same result. That is
redundant, costs a full `schedule_all_days` pass over the whole horizon per
round, and — because the second pass rewrites rejection reasons as
`COMBINED-SCHEDULE-DROP` — destroys the `NEVER-PLACED` / `ORACLE-DROPPED` split
the dashboard reports. Removing the outer call is planned.

## Known issues

- **Late-arriving demand still cannot be produced.** An order whose whole
  delivery window sits inside an already-frozen week has no legal slot, and the
  frozen zone only moves forward past it. It is now *reported* as
  `ARRIVED-AFTER-FREEZE` and listed in `output/actie_vereist_*.csv`, but
  resolving it needs a person. Measured against the real order book this is
  rare: 98.7% of orders arrive more than four weeks before delivery, outside
  the commit window entirely.
- **Re-running the same input does not give a byte-identical plan.** CP-SAT
  runs multi-worker under a wall-clock budget, so the day/line assignment can
  land a few orders differently between runs. The per-day sequencer is seeded
  and single-worker, so that layer *is* reproducible. Never quote a single
  run's exact count as the definitive answer.
- **`order_identity` is not perfectly unique.** `order_nr` is really the order
  date, so ~1.2% of orders share order_nr + art_nr + klant + lever_datum with a
  genuine sibling line item. Far tighter than order_nr alone (which collides up
  to 61-way), but a real order id from the source system would remove the
  ambiguity entirely.
- **Ten constraints are enforced nowhere** — halal isolation, speed caps,
  per-day production limits, identical-product grouping and others all have a
  spreadsheet column and a `Product` field but no implementation. They are
  listed as such at the top of the generated `docs/constraints.md`.
