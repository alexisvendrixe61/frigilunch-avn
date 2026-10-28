# The `frigilunch` package — complete reference

## What this folder is

This is the **new, clean version** of the Frigilunch planner, built alongside
the older `src/` code. The goal is to gradually move all logic here and
eventually delete `src/`. Right now, roughly 20% of the real logic has moved
in; the rest still runs in `src/` and is called from here via "bridge" imports.

---

## The three-layer rule

Every file in this package belongs to exactly one layer. The rule is simple:
**arrows only point inward — never outward.**

```
┌──────────────────────────────────────────────────────────┐
│  adapters/                                               │
│  Reads files, writes files, talks to Excel and JSON.     │
│  Knows about the outside world.                          │
│                                                          │
│  master_data.py   excel_orders.py   excel_export.py      │
│  json_state.py    dashboard.py                           │
└──────────────────────┬───────────────────────────────────┘
                       │ calls ↓ only
┌──────────────────────▼───────────────────────────────────┐
│  application/                                            │
│  Coordinates the stages. Knows the order of operations.  │
│  No file I/O, no business rules.                         │
│                                                          │
│  pipeline.py    run_week.py                              │
└──────────────────────┬───────────────────────────────────┘
                       │ calls ↓ only
┌──────────────────────▼───────────────────────────────────┐
│  domain/                                                 │
│  The brain. Pure business logic only.                    │
│  No file I/O, no OR-Tools, no Excel — ever.              │
│                                                          │
│  models.py   oracle.py   benders.py   state.py           │
│  cuts.py     constraints/                                │
└──────────────────────────────────────────────────────────┘
```

**Why this matters:** you can test any domain function without touching a
file. You can swap Excel for a database without changing a single business
rule. Each layer has one job and only one.

---

## Entry point

### `cli.py`

The front door. Parses command-line arguments and hands them to `run_week`.

```
py -m frigilunch.cli --week 36 --year 2026 [--reset]
```

| Flag | Effect |
|---|---|
| `--week N` | ISO week to plan (required) |
| `--year Y` | ISO year (default: current year) |
| `--reset` | Ignore any existing `plan_state.json` and start fresh |
| `--data-dir` | Path to the `data/` folder (default: `data/`) |
| `--output-dir` | Where to write results (default: `output/`) |

All flags are available in both `py -m frigilunch.cli` and `py create_plan.py`.

---

## Application layer

### `application/run_week.py`

A thin assembler. Takes the CLI arguments, builds the standard file paths,
and calls `run_pipeline`. Nothing else.

```python
run_week(week, year, data_dir, output_dir, reset) → PipelineContext
```

### `application/pipeline.py`

The coordinator. Defines `PipelineContext` (the shared state object that all
stages read and write) and runs 9 stages in order.

```
Stage 1  _load_master        Load products from products.json
Stage 2  _load_orders        Read new orders from the Excel order book
Stage 3  _validate_master    Warn about missing/bad product data
Stage 4  _filter_orders      Keep only orders known by this Monday,
                             delivering before the horizon end
Stage 5  _apply_freeze       Split orders into:
                               - locked (already committed last run)
                               - free (needs solving this run)
Stage 6  _run_solver         Run the CP-SAT optimizer → assigns each
                             free order a (day, line)
Stage 7  _build_schedules    Replay frozen days + build minute-by-minute
                             timelines for newly solved days
Stage 8  _commit_window      Save the next 4 weeks to plan_state.json
Stage 9  _export_outputs     Write Excel, HTML dashboard, action CSV
```

**`PipelineContext`** is a dataclass that starts with just the config
(`week`, `year`, paths, `reset`) and gets populated as stages run:

| Field | Set by | Contains |
|---|---|---|
| `products` | `_load_master` | Product master dict |
| `orders` | `_load_orders` | All orders from Excel |
| `frozen_orders` / `locked_orders` | `_apply_freeze` | Orders locked by a previous run |
| `free_orders` | `_apply_freeze` | Orders the solver must place this run |
| `outcome` | `_run_solver` | `SolveOutcome` with scheduled/rejected lists |
| `replayed_schedules` | `_build_schedules` | Frozen days replayed byte-for-byte |
| `plan_state` | `_apply_freeze` / `_commit_window` | The JSON state dict |

**Current status:** stages 2–9 still bridge into `src/` (marked `# src bridge`
in the code). The structure is correct; the content is being migrated
gradually.

---

## Domain layer

### `domain/models.py`

The shared vocabulary. Pure data — no logic, just types.

| Class | What it represents |
|---|---|
| `Product` | One product from `Planning info.xlsx`: speed, allergen level, line rules, position rules, machine requirements |
| `Order` | One production order: article, quantity, delivery date, assigned day/line |
| `PlannedOrder` | An order placed on a specific line at an exact time, with changeover minutes |
| `DaySchedule` | The full built plan for one day: two line sequences, warnings, dropped orders, KPIs |
| `PlanScore` | Lexicographic quality score (rejected orders → rejected units → setup minutes → …) |
| `SolveOutcome` | What the solver returned: status, scheduled list, rejected list, score, stats |

**`PlanScore` fields in business-priority order:**

| Field | Meaning |
|---|---|
| `rejected_orders` | Number of orders that could not be placed (minimise first) |
| `rejected_units` | Total units in rejected orders |
| `transition_minutes` | Total changeover minutes across all days |
| `scale_changes` | Number of scale/format changes |
| `early_days` | Days produced earlier than needed |
| `moved_forecast_orders` | Orders moved vs. the previous forecast |

Any plan with fewer rejections beats any plan with more — no matter how much
better the second plan scores on everything else.

---

### `domain/oracle.py`

The **day builder**. Given a list of orders for one day, it builds the
exact minute-by-minute timeline.

```python
schedule_day(orders, products, datum, config) → DaySchedule
```

Pure and deterministic — same inputs always produce the same output.
No OR-Tools, no file I/O.

**What it does inside:**

1. **Assign lines** — each order goes to L1 or L2 based on product rules
   (hard constraints first: `alleen_l1`, `l2_uitgesloten`, etc.;
   load-balance as tiebreaker)
2. **Sort** — allergen level ascending (lowest first, so cleaning goes
   forward), then `apply_positie_regels` to honour must-open/must-close rules
3. **Walk the sequence** — for each order: compute changeover from the
   previous product, add production time, check we're still inside the shift.
   Orders past shift end are dropped.
4. **Enforce ends (S2)** — if the sequence has an illegal opener or closer,
   reorder or remove the smallest orders until it's legal.
5. **Return** a `DaySchedule` with both line timelines, warnings, dropped
   orders, and KPIs.

---

### `domain/benders.py`

The **feedback loop** (Logic-Based Benders decomposition). This is the heart
of why the planner works.

```python
run_benders(orders, products, solvers, config) → SolveOutcome
```

The problem: the solver cannot know exact changeover times (they depend on
the sequence the day builder chooses), but the day builder cannot choose a
sequence until it knows which orders are on the day.

Benders breaks the deadlock by alternating:

```
Repeat up to max_iterations:
  1. SOLVE   — CP solver picks (day, line) for every order
               using an approximate capacity estimate
  2. ORACLE  — day builder validates each day for real;
               some orders don't fit and get dropped
  3. CUT     — for each day that dropped orders, add a
               no-good constraint: "never propose this
               exact overloaded combination again"
  4. KEEP BEST — track the best result seen so far
```

Stops when no orders are dropped (converged), or `max_iterations` is reached.

**Solvers are injected** — `benders.py` never imports OR-Tools directly.
It receives a `Solvers(cp=..., oracle=...)` named tuple. This keeps the
domain pure and makes it easy to test with a stub solver.

---

### `domain/cuts.py`

Builds the feedback messages the Benders loop sends back to the solver.

| Function | Cut type | Meaning |
|---|---|---|
| `build_no_good_cut` | `"no_good"` | These exact orders cannot all share this day |
| `cuts_for_device_pair` | `"device_pair"` | These two specific orders cannot share this day (shared machine conflict) |

Cuts are returned as plain dicts — no OR-Tools objects — so this module stays
import-free and testable.

---

### `domain/state.py`

Pure functions for the **rolling freeze** — the commitment logic that locks
the next 4 weeks after each run.

| Function | What it does |
|---|---|
| `freeze_window(state, week, year, schedule)` | Returns a new state dict with this week's commit window frozen |
| `load_frozen_orders(state, week, year)` | Returns orders that are frozen and still in the future |
| `is_frozen(state, order_key)` | Check if a specific order is already committed |

No file I/O here — reading and writing JSON is the adapter's job
(`adapters/json_state.py`).

---

### `domain/constraints/`

Business rules, each in its own module.

#### `constraints/changeover.py` — O1, O2, O3, V1

**`changeover_minutes(prev, curr, setup_matrix) → int`**

The number of minutes the line stands still between two products.
Takes the **maximum** of all applicable scenarios (they physically overlap):

| Scenario | Cost |
|---|---|
| Same scale format | 20 min |
| Different scale format | 30 min |
| Kapvorm change | 15 min (below the 20-min floor — never actually adds time) |
| Same machine (Leonard / Gernal / Multifill) | 30–40 min |
| Allergen drop or cross-contamination | 30 min |
| Named Daesang pair | 20 min (short-circuits everything else) |

**`production_minutes(order, product) → int`**

Production time = `(quantity / speed_per_hour) × 60`, rounded up to the
nearest 5 minutes.

#### `constraints/sequencing.py`

Position rules — which products must or cannot open/close the day.

| Function | Rule |
|---|---|
| `apply_positie_regels(pairs)` | Reorder the list to place must-open first, must-close last |
| `positie_ok(products)` | Check that the current sequence has legal ends |

#### `constraints/allergen.py`

Allergen-level logic — sate (peanut, level 6) exclusivity and cross-line
contamination rules.

#### `constraints/device.py`

Machine-conflict detection — which products require a single-unit machine
and therefore cannot share a line-day with another product claiming the
same machine at the same time.

---

## Adapters layer

### `adapters/json_state.py`

Reads and writes `plan_state.json` — the file that remembers which weeks
are frozen between runs.

| Function | What it does |
|---|---|
| `read_state(path)` | Load the state file; return empty baseline if it doesn't exist |
| `write_state(path, state)` | Atomic write: temp file → rename, so a crash never corrupts the state |

Uses schema versioning (`schema_version: 1`) to detect stale files from
incompatible older runs.

### `adapters/master_data.py`

Reads `products.json` (generated from `Planning info.xlsx`) and converts
the `src.models.Product` format into `frigilunch.domain.models.Product`.

| Function | What it does |
|---|---|
| `read_products(path)` | Load products; bridge to `src.loader.load_products` |
| `validate_master(products, orders)` | Return warning strings for missing products and unconfigured speeds |

### `adapters/excel_orders.py`, `adapters/excel_export.py`, `adapters/dashboard.py`

These adapters handle order ingestion from Excel and result export (Excel
plan, HTML dashboard). They are stubs or thin bridges at this stage —
the real export logic still lives in `src/exporter.py`, `src/dashboard.py`.

---

## Migration status

| Module | Status |
|---|---|
| `domain/models.py` | Complete — domain types fully defined |
| `domain/oracle.py` | Complete — pure day builder, no `src/` dependency |
| `domain/benders.py` | Complete — pure feedback loop, solver injected |
| `domain/cuts.py` | Complete — no `src/` dependency |
| `domain/state.py` | Complete — freeze logic + `order_identity`, `iso_weeks_before` |
| `domain/plan_score.py` | Complete — pure scoring, no `src/` dependency |
| `domain/constraints/` | Complete — changeover (+ `calculate_duration`), sequencing, allergen, device |
| `adapters/json_state.py` | Complete — atomic read/write, schema versioning |
| `adapters/config.py` | Complete — pure copy of `src/config.py`, reads `capacity.json` |
| `adapters/exceptions_report.py` | Complete — exception CSV builder, no `src/` dependency |
| `adapters/master_data.py` | Bridges `src.loader` — conversion complete, loading not yet |
| `adapters/excel_orders.py` | Stub — bridges `src.order_filter` |
| `adapters/excel_export.py` | Stub — bridges `src.exporter` |
| `adapters/dashboard.py` | Stub — bridges `src.dashboard` |
| `application/totals.py` | Complete — order/unit reconciliation, no `src/` dependency |
| `application/pipeline.py` | Structure complete — all stages bridge `src/` |
| `application/run_week.py` | Complete |
| `cli.py` | Complete — `COMMIT_WEEKS` and `iso_weeks_before` from `domain.state` |

**Remaining bridges to remove** (in priority order):
1. **Phase 2** — `adapters/excel_orders.py` (src.order_filter), `adapters/master_data.py` (src.loader), export adapters (src.exporter, src.dashboard), `json_state.py` full (src.plan_state I/O functions)
2. **Phase 3** — `domain/constraints/` registry, calendar, grouping, line, day, staffing rules; `domain/validator.py`; `application/layout_metrics.py`
3. **Phase 4** — `domain/oracle.py` complete (src.scheduler day builder + multi_day_scheduler)
4. **Phase 5** — `domain/solver.py` (src.scheduler_ortools_v3 — the CP-SAT solver)
5. **Phase 6** — delete `src/`
