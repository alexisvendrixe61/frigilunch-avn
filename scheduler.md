# How the scheduler works

## The two levels

| Level | Question | Technology |
|---|---|---|
| **Across days** | Which day and which line for each order? | CP-SAT (OR-Tools) |
| **Within a day** | What order to run them in, exact times? | Greedy walk (deterministic) |

CP-SAT can't model sequence-dependent setup times at scale, so it approximates
capacity with a flat proxy buffer per order. The greedy walk knows the real
setup rules and produces exact minute-by-minute timelines. A feedback loop
connects them: CP-SAT proposes, the greedy walk validates, cuts correct.

## A run, end to end

`create_plan.py --week N --year Y`

| # | Step | What it decides |
|---|---|---|
| 1 | Load + filter | Unknown article → rejected immediately. Keep only orders already placed by the run date, delivering inside the horizon. |
| 2 | Read `plan_state.json` | Committed days come back untouched; the solver is barred from every day before `frozen_until`. |
| 3 | Slot filter | Per order, the set of legal (day, line) pairs. Anything removed here the solver can never violate. |
| 4 | CP-SAT assignment | Day + line for every remaining order (4 phases, below). |
| 5 | Build days | Real minute-by-minute timeline per line-day. Orders that no longer fit are dropped. |
| 6 | Reconcile | Dropped orders go back to the solver, 3 rounds. Still no fit → `COMBINED-SCHEDULE-DROP`. |
| 7 | Commit | The next 4 weeks of built days are written to `plan_state.json` and frozen. |

Outputs: `output/planning_*.xlsx`, `output/capacity_dashboard_*.html`,
`output/actie_vereist_*.csv`.

## The 4 solver phases

1. **Line-restricted orders** — orders that can only run on one line get first
   pick of full capacity. They have nowhere else to go if they lose a slot.
2. **Flexible orders** — solve against what phase 1 actually left, measured
   from its real schedules rather than estimated. Phase 1's orders are not
   just a minutes budget: they are on the day when phase 2's oracle builds it,
   and they count toward whether that day can supply both its ends (S3).
3. **Retry with chunking** — orders rejected as `NEVER-PLACED` or
   `ORACLE-DROPPED` are retried with forced splitting, to scatter into thin
   leftover capacity.
4. **Reconcile** — all three phases' output goes through the real day builder
   together. What can't survive sharing a day is dropped.

### The feedback loop (inside each phase)

```
Repeat up to 15 iterations / 240s per phase:
  1. CP-SAT picks (day, line) for each order
  2. Greedy oracle builds each day's real timeline — may drop orders
  3. No-good cut: "never propose this exact overloaded set again"
  4. Keep the best result so far
```

Logic-based Benders decomposition. It is **wall-clock budgeted**, so two runs
on identical input give slightly different answers. Never quote a single run's
counts as the plan's fixed properties.

## Within a day

One line first, then the other staggered around its claim on any single-unit
machine (rule M1):

1. **Sort** — identical products (column "identiek product") form one block,
   smallest order first (G1); blocks are grouped by `kapvorm` (G2 — products
   without one, "NVT", nearly all of L2, share one bucket, so there the layer
   falls through); groups ordered by highest allergen level ascending, then
   total quantity descending; inside a group by allergen level ascending →
   quantity descending → FTE ascending.
2. **Pin the ends** — products that must/cannot open or close the day (S2).
3. **Walk** the list once: setup time from the previous order, production
   duration, stagger for shared machines, cleaning if allergens require it.
   Past shift end → **drop the order**.
4. **Re-sequence** — for the committed window, a small CP-SAT TSP looks for a
   better permutation; accepted only if the greedy walk confirms it is
   strictly better. "Better" is one score, used everywhere the builder picks
   between two builds (`src/day_quality.py`): identical products back to back
   (400 per pair), same kapvorm back to back (60), minus idle minutes, minus
   changeover minutes. Idle time counts because a day should run to its end
   without holes (O6) — before, an order that waited hours on a machine the
   other line held looked exactly as good as one run before the block. It is
   given the other line's hold on each shared machine, so it optimizes against
   the day that actually exists.
5. **Try the mirror** — on a day where both lines want the same single-unit
   machine, all of the above is repeated with the other line going first, and
   the version carrying more orders wins. Being second is the whole cost of
   sharing a machine, and which line pays it is not a fact about the floor.

Sate ("X", allergen level 6) is the exception to "the two lines are
independent" (rule A5). Peanut carries through the hall, so while sate runs the
other line produces nothing else. Sate beside sate is allowed since the second
Gernal (planner, 22/10): two sate blocks on one day run **side by side** and
open on the same minute (`FRIGI_SATE_PARALLEL=0` restores the old queue). Sate
is placed last on its line and slid towards closing time, and the other line is
stopped where it opens: the block costs that line its own length wherever it
sits, but anywhere earlier it also costs everything that line would have run
after it, since a stopped line does not restart. So the other line works a full
day and loses only the last hours.

Only as late as the other line needs, though (planner, 02/10). When the other
line runs out of work before closing time, the sate starts the moment it stops
— the **handover** — instead of leaving both lines idle until the slot at the
end of the shift. With sate on both lines the common opening follows the same
idea: as soon as both lines are done with ordinary work, but late enough that
the longer block still ends before closing time.

That closing position is a **rule** since 22/10: after sate nothing else can
run on that line. A product the sheet says must close the day therefore never
shares a line-day with sate (the assignment model keeps them apart), and two
different sate products close the day together as one block. Orders the rule
costs are reported as `SATE_DROP`, and the assignment model reserves the time
up front (A6) so it does not keep booking days it will lose.

Note the sort walks in this order, so quantity decides which orders die on an
overfull day, and FTE is a last tiebreak that almost never binds. Measured:
the resulting sequences are already at **93% of the theoretical optimum**, so
there is little left in sequencing alone.

## What a changeover costs

The **maximum** of every applicable scenario, never the sum — the steps
physically overlap. From `data/setup_times.json`:

| Scenario | Minutes |
|---|---|
| Same scale format | 20 |
| Different scale format | 30 |
| Kapvorm change | 15 |
| Leonard → Leonard (floor) | 40 |
| Multifill → Multifill (floor) | 30 |
| Allergen drop or cross-contamination cleaning | 30 |
| Named article pairs (Daesang) | 20, short-circuits everything |

Two consequences worth knowing:

- **A kapvorm change costs no changeover.** 15 is below the 20 floor and the
  model takes the maximum, so no kapvorm change has ever added a minute. Which
  is why kapvorm grouping is scored explicitly instead (G2): a penalty in the
  assignment model, a bonus in the sequencer.
- **Identical products cost the same as any other product of the same format**
  (20). The changeover model cannot see the difference, so identical products
  are grouped by their own terms (G1), not by setup time.

Measured over 5 runs (1,440 transitions, 39,550 min): 73% is the unavoidable
20-minute floor. Of the 27% above it, cleaning is 56%, the Leonard floor 33%,
different format 9%, kapvorm 0%.

## What the solver optimizes

Effective weights (code default, overridden by `.env`):

| Term | Weight | Note |
|---|---|---|
| Order unscheduled | 10,000 + 10/unit | Dominates everything. The per-unit term makes large orders worth more than small ones. |
| Days late | 100 | **Never fires.** `min_days_before_delivery = 0` means the slot filter offers no day after the delivery date, so `days_late` is always 0. |
| Days early | 1 | Only beyond an 11-day grace window, up to the 14-day limit. |
| Line activation | 5 | Prefer filling one line over half-filling two. |
| Day activation | 2 | Prefer fewer days. |
| Off-preferred line | 50 | Per order on the line the product does not prefer (04/09 meeting). |
| Identical split | 40 | Per (day, line) an identical-product group is spread over (G1, 02/10 meeting). Just below the line preference. |
| Kapvorm spread | 3 | Per active (day, line, kapvorm) (G2). Between line and day activation. |
| L1 kapvorm change | 60 | Per extra kapvorm on an L1 line-day and per L1 kapvorm campaign starting across a seam (G2, planner 22/10: the top rule on the automatic packing line). Above the line preference; the regroup pass minimises L1 kapvorm changes before anything else, and the sequencer pays 2000 per same-kapvorm arc on L1. |
| Format spread | 1 | Concentrate same-format orders on one (day, line). The lightest weight in the model. |
| Stacked "moet" | 500 | Per must-open (or must-close) product landing on a line-day that already has one. A day has one first and one last order, so the second such requirement cannot be met whatever the sequencer does. Soft on purpose: 20x below the cost of losing an order. |
| Split | 0 | Splitting is currently free. |

`.env` also sets `FRIGI_KAPVORM_CARRY` and `FRIGI_KAPVORM_WEEK_PENALTY` with a
measurement report calling them active. **No code reads them.**
`FRIGI_KAPVORM_SPREAD_PENALTY` is read since the 02/10 work (the G2 term above).

## Order splitting

- Phases 1 & 2: only when an order physically cannot fit any single day.
- Phase 3: every order is eligible — the point is to use thin leftovers.
- Up to 5 chunks, each ≥ 60 minutes.
- All-or-nothing: if any chunk fails the oracle, the whole order is rejected.

## Rolling lock

Each run commits the **day schedules** whose date falls in the next 4 weeks to
`plan_state.json`, keyed by date, each holding its two line sequences in
running order with every run's exact time slot and the day's warnings.

The next run replays those days rather than re-deriving them, and the solver is
barred from **every day** before `frozen_until` — not just the orders already
on them. Idle capacity on a frozen day stays idle.

Running week N commits weeks N…N+3. On the run for week N+1, week N has passed
and is dropped as produced, weeks N+1…N+3 arrive already locked, and exactly
one new week is committed. Four committed, three carried, one new.

The day is the stored unit deliberately. Storing loose orders and regrouping
them on load previously allowed three silent leaks: split chunks sharing a day
and line overwrote each other, orders the day builder dropped were reported
rejected *and* committed, and a replayed day's warnings were reset to empty.
See `src/plan_state.py`.

### Warm-up (`--warmup N`)

A run with an empty `plan_state.json` cannot place anything before its own
`plan_start`, so every order delivering early in the target week loses most of
its 14-day production window. Measured on week 36: 43 orders left with 5 days
instead of 14, and 786,646 units compressed into the first fortnight. The week
reads as far fuller than it is, and the rejections that follow are an artefact.

`--warmup N` re-runs the N preceding weeks first, each committing normally, so
the target week starts from the state a real weekly cadence would have left.
Default is `COMMIT_WEEKS` (4) when the state is empty or `--reset` is given,
otherwise 0. Running `--week 36` cold therefore runs 32, 33, 34, 35, 36.

**Why 4 and not 3.** A run for week W newly solves week `W + COMMIT_WEEKS - 1`
— everything before that is already frozen by the previous run. So week 36 is
decided by the run for week 33, week 37 by the run for 34, and so on. With only
3 warm-up weeks the chain starts at 33, which means the cold run decides the
target week itself:

| warm-up | runs | wk36 | wk37 | wk38 | wk39 |
|---|---|---|---|---|---|
| 0 | 36 | **cold** | **cold** | **cold** | **cold** |
| 3 | 33–36 | **cold** (run 33) | run 34 | run 35 | run 36 |
| 4 | 32–36 | run 33 | run 34 | run 35 | run 36 |

Going beyond 4 does not change *which* run decides each week — only the frozen
backdrop those runs work against becomes warm too. That is a second-order
effect and has not been measured; 5 or 6 is defensible if a run is cheap
enough, but 4 is the point where the whole committed band stops being cold.

Each warm-up week runs as its own subprocess with `--warmup 0` and its own
`--as-of` (its own Monday), so it sees only demand booked by then and cannot
recurse. Only the first inherits `--reset`; the rest must inherit what the
previous one committed. It does **not** combine with `--dry-run` — a warm-up
week only means anything if it commits — and the run refuses rather than
quietly breaking one of the two promises.

This shifts the cold-start distortion N weeks back rather than removing it: the
first warm-up week is still cold. Only a real historical `plan_state.json`
removes it entirely.

## Why orders get rejected

Every rejection carries its reason into `actie_vereist_*.csv` and the
dashboard:

| Cause | Meaning |
|---|---|
| `Product … unknown` | Article missing from `Planning info.xlsx`. No duration can be computed, so it never reaches the solver. A data problem, not a capacity one. |
| `NEVER-PLACED` | No legal (day, line) with room inside the delivery window, even split. Genuine oversubscription. |
| `COMBINED-SCHEDULE-DROP` | Assigned a day, but still didn't fit the real combined schedule after 3 reconciliation rounds. Each phase now validates against the days the earlier phases actually built, so most of what used to surface here fails inside the phase that caused it, where the loop can still act on it. |
| `ORACLE-DROPPED` | Capacity looked available in the proxy model but wasn't once real changeovers were applied. |
| `DAY-BUILD-DROP` | The day schedule couldn't close it before shift end. The cost of booking the full shift window with no reserve. |
| `LINE-DAY-RESTRICTION` | The slot filter left no legal slot at all. |
| `POSITIE-REGEL` | It fit the day on capacity, but the day builder could not arrange that day with both ends legal while keeping it (S2). Not a capacity story — such an order usually fits on another day in its window, next to a day-mate allowed to take the end it cannot. |

Single-unit machines were the dominant driver of the middle four — `gernal`
alone was named in about half of them. Since the 02/10 meeting there are two
Gernals and two Leonards (`data/capacity.json`), so neither is staggered any more.

`ORACLE-DROPPED` was largely the machine too. M2 ("these two orders may not
share a day") used to fire literally never: M1's staggering removes the overlap
it looked for, and the day builder discarded the real reason a staggered order
was dropped, so the solver learned "this set is too big" instead of "these two
cannot share a day". It now learns the pair, from a counterfactual the day
builder establishes while it still can. See
[`docs/toestel-cut-gap.md`](docs/toestel-cut-gap.md).

## Key config

Effective values; code defaults in `src/scheduler_ortools_v3.py`, overrides in
`.env`, capacity and timing in `data/capacity.json`.

| Parameter | Effective | What it does |
|---|---|---|
| `FRIGI_PROXY_BUFFER` | 30 | Per-order setup guess in the CP-SAT model (minutes) |
| `FRIGI_EARLY_GRACE_DAYS` | 11 | Days early that cost nothing |
| `FRIGI_EARLY_PENALTY` | 1 | Per day early beyond the grace window |
| `FRIGI_LATE_PENALTY` | 100 | Inert — see above |
| `FRIGI_MAX_CHUNKS` | 5 | Max pieces per split order |
| `FRIGI_MIN_CHUNK_MIN` | 60 | Minimum chunk size |
| `FRIGI_SYMMETRIC_DEVICE_PRIORITY` | 1 | Build a contended day both ways round and keep the better one |
| `FRIGI_IDENTIEK_SPLIT_PENALTY` | 40 | G1 term in the assignment model |
| `FRIGI_KAPVORM_SPREAD_PENALTY` | 3 | G2 term in the assignment model |
| `FRIGI_SEQ_IDENTIEK_BONUS` / `_KAPVORM_BONUS` / `_IDLE_WEIGHT` | 400 / 60 / 1 | The day sequencer's shape terms (and `day_quality.line_score`) |
| `FRIGI_PHASE_CONTEXT` | 1 | A later phase sees the earlier phase's built day, not just its minutes |
| `FRIGI_S2_DISTINCT_ENDS` | 1 | Two or more orders on a line-day need two that can take an end |
| `FRIGI_S2_SEQUENCER_REPAIR` | 1 | Solve an illegal line-day's ends instead of removing orders until it is legal |
| `FRIGI_POSITIE_MOET_PENALTY` | 500 | Cost of stacking two "moet opstarten"/"moet afsluiten" on one line-day |
| `FRIGI_DEVICE_DROP_CUTS` | 1 | Blame a drop on the shared machine only when the order fits without it, and cut the pair for that day |

The last five were measured as one arm — they address the same failures, so
switching them one at a time says little. Week 36, full warm-up chain,
`--reset`, 2 runs per arm, whole-horizon totals:

| | all off | all on |
|---|---|---|
| Orders scheduled | 413–423 | 420–427 |
| Rejections excluding unknown articles | 16, 21 | 10, 10 |
| `COMBINED-SCHEDULE-DROP` | 6, 6 | **0, 0** |
| `POSITIE-REGEL` | 1, 4 | 0, 1 |
| `NEVER-PLACED` | 2, 3 | 2, 3 |
| Reconciliation rounds over the chain | 30, 30 | **0, 0** |
| Scheduled units | 4.209–4.253M | 4.226–4.229M |
| Changeover minutes | 7,830–8,040 | 8,080–8,420 |
| Runtime | ~8:00 | ~5:40 |

Read it honestly. `NEVER-PLACED` does not move: that bucket is real
oversubscription and none of this was ever going to touch it. Units are inside
the run-to-run spread, so the gain is in orders placed, not volume. Changeover
is 3–5% **higher** — more orders sharing a day costs more setup, and that is
the trade. What does move decisively is the two buckets that were never about
capacity in the first place, and the runtime, because the reconciliation rounds
this makes unnecessary were the expensive part of a run.

The committed-window slice is too noisy to read here: a cold `--reset` chain
freezes a different set of weeks each time, so `orders_in_window` itself varies
(141–155) and `ARRIVED-AFTER-FREEZE` (3–7) dominates what is left. Compare on
whole-horizon totals.
| `feedback_timeout_seconds` | 240 | Wall-clock budget **per phase** |
| `feedback_iter_timeout_seconds` | 10 | Per CP-SAT iteration |
| `capacity_minuten` | 990/960, Fri 870/840 | Bookable minutes per (day, line). Planning reserve is 0 — the full shift window. |
| `delivery_proximity_grace_days` | 7 | Soft earliness grace; not a hard production window |
| `COMMIT_WEEKS` | 4 | Weeks committed and frozen per run |
| `--warmup` | 4 when cold | Preceding weeks to run first, see above |

`.env` is loaded by `src/scheduler_ortools_v3.py` at import. Modules that read
`FRIGI_*` at import time and are imported earlier see only the code defaults.

## Where each rule is enforced

`docs/constraints.md` is generated from `src/constraints/registry.py` and lists
every rule, its severity, and the single place it is enforced. Regenerate with
`py -m src.constraints.registry`; never hand-edit it. The registry is
self-checking — a rule claiming an implementation that doesn't resolve fails
the test suite.

Enforcement points: **FILTER** removes illegal (day, line) slots before the
solver sees them, **BUILD** shapes the day and may refuse an order, **VERIFY**
only reports, and **NONE** means the rule has data and a field but is not
enforced anywhere.
