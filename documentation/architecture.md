# Architecture

## Package layout

The codebase uses three layers. Each layer may only call inward — adapters may call application and domain; application may call domain; domain may call nothing outside itself.

```
frigilunch/
├── domain/          pure business logic — no I/O, no OR-Tools imports
│   ├── models.py        Order, Product, DaySchedule, PlannedOrder, SolveOutcome, PlanScore
│   ├── constraints/     planning rules: allergen, changeover, device, sequencing, slots
│   ├── oracle.py        schedule_day() — deterministic day builder (pure function)
│   ├── benders.py       generic Benders loop (solver + oracle injected)
│   ├── state.py         rolling freeze logic, COMMIT_WEEKS, plan_state helpers
│   └── validator.py     post-build assertions; reports, never changes anything
│
├── application/     orchestration — wires domain to adapters
│   ├── pipeline.py      STAGES tuple + run_pipeline(); the entry point for a full run
│   ├── run_week.py      assembles paths and calls run_pipeline()
│   ├── date_optimizer.py  real CP-SAT solver (assign_production_dates_feedback)
│   ├── scheduler.py     heuristic day builder (schedule_day); called as oracle
│   ├── day_sequencer.py CP-SAT TSP for per-line daily sequencing
│   ├── multi_day_scheduler.py  runs scheduler across the full horizon
│   ├── day_quality.py   scoring functions used by the sequencer and day builder
│   └── plan_score.py    aggregate plan quality metrics
│
├── adapters/        I/O — reads/writes files; calls application and domain only
│   ├── config.py        reads capacity.json; defines shift windows and planning caps
│   ├── master_data.py   reads products.json; produces domain Product objects
│   ├── excel_orders.py  reads the order spreadsheet; produces domain Order objects
│   ├── excel_export.py  DaySchedule list → formatted Excel workbook
│   ├── dashboard.py     solved plan → dashboard payload → HTML
│   ├── json_state.py    reads and writes plan_state.json
│   └── exceptions_report.py  rejected orders → actie_vereist CSV
│
└── cli.py           argparse entry point; delegates to application/run_week.py
```

**The rule that matters:** `domain/` never imports from `application/` or `adapters/`. This makes every domain function testable in isolation with no file system or solver dependency.

---

## Data flow

```
data/source_data/*.xlsm/.xlsx
        │
        ├─ excel_orders.py ──────────────────────▶ list[Order]
        └─ master_data.py  ──────────────────────▶ dict[str, Product]
                                    │
                               pipeline.py
                               (9 STAGES)
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
             date_optimizer.py              scheduler.py (oracle)
             assign_production_dates_feedback()    schedule_day()
                    │                               │
                    └───────────────┬───────────────┘
                                    │
                             SolveOutcome
                          (scheduled, rejected)
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
             excel_export.py   dashboard.py  exceptions_report.py
             planning_*.xlsx   *.html        actie_vereist_*.csv
                                    │
                             json_state.py
                             plan_state.json
```

---

## The optimizer

`assign_production_dates_feedback` in `frigilunch/application/date_optimizer.py` decides which day and line each order runs on. It uses a logic-based Benders decomposition (see [decisions/001-benders-loop.md](decisions/001-benders-loop.md)):

### The loop

```
SOLVE ──▶ ORACLE ──▶ CUT ──▶ (repeat)
```

1. **SOLVE** — CP-SAT builds a light assignment model: which (day, line) for each order? Capacity is approximated as `sum(order durations + PROXY_BUFFER_MIN per order) ≤ planning_cap`. This is fast but approximate — the buffer is a flat guess, not the real sequence-dependent changeover cost.

2. **ORACLE** — For every assigned day, `schedule_day()` runs for real. It computes actual sequence-dependent changeovers and drops any orders that do not fit inside the shift window.

3. **CUT** — For every day that dropped at least one order, a "no-good cut" is added to the CP-SAT model: the exact combination of orders that over-filled that day may never be assigned there again. Dropped orders are free to migrate to other days.

4. **REPEAT** — Until no day drops any order (convergence), or the time budget runs out, or max iterations is reached.

The **best iteration** — the one where the oracle placed the most orders — is kept, not necessarily the last one.

> **Example — one Benders iteration:** Four orders are assigned to Thursday L1: DMB Kippensoep (90 min), VAT Bami Goreng (75 min), DMB Tomatensoep (60 min), VAT Groenteschotel (80 min). Solver estimate: 305 min production + 4 × 30 min proxy buffer = 425 min — fits within the 990 min window. The oracle runs: Kippensoep → Bami needs 45 min allergen cleaning; Bami → Tomaat 20 min; Tomaat → Groente 15 min. Real load: 90 + 45 + 75 + 20 + 60 + 15 + 80 = 385 min. Day passes — no cut added. A fifth order, Satéschotel (85 min), was not placed here: its estimated contribution (85 + 30 = 115 min) would push the day over the cap.

### The objective function (what CP-SAT minimises)

| Term | Weight | Effect |
|---|---|---|
| Unscheduled order | 10,000 + units × weight | Always place one more order before optimising anything else |
| Days late | 100 per day | Produce before delivery date |
| Days early (beyond grace window of 7 days) | 1 per day | Do not produce too far ahead |
| Active (day, line) slots | small | Consolidate production; avoid fragmented days |
| Format spread on a (day, line) | small | Same scale format on same line → shorter changeovers |
| Extra chunks (split orders) | configurable | Only split when necessary |

> **Example — objective in action:** Order A (DMB Kippensoep, due Monday) has two legal slots: Friday (3 days early, within the 7-day grace window, cost 0) and Monday (0 days early, cost 0). The consolidation term decides: four other orders are already on Monday → Monday is chosen to keep production dense. Order B (VAT Bami Goreng, due Wednesday) has Monday (2 days early, within grace, cost 0) and the following Thursday (1 day late, cost 100 per day). Monday wins decisively — placing it late costs 100 points while placing it early costs nothing.

### Order splitting

An order may be split into up to `FRIGI_MAX_CHUNKS` (default 5) chunks, each at least `FRIGI_MIN_CHUNK_MIN` (default 60) minutes, placed on different (day, line) combinations. Splitting is only permitted when the order cannot physically fit in the largest single day available to it. An order is counted as scheduled only if **all** its chunks survive the oracle — partial delivery is never accepted.

### Three-phase solve

The solver runs in three phases to handle orders with strict line constraints:
1. **Phase 1** — line-restricted orders only (alleen_l1, alleen_l2, sate)
2. **Phase 2** — remaining free orders
3. **Phase 3** — a forced-split retry for orders that failed both earlier phases

Each phase ends with a reconciliation pass (`reconcile_combined_schedule`) that re-checks the combined set against the real day builder.

> **Example — three phases:** Phase 1 places 12 sate orders (allergie_niveau 6, `alleen_l2=True`, must close their line) across 6 days on L2. Phase 2 assigns 470 remaining orders into the remaining slots, with the L2 minutes already reserved for each sate day. In Phase 3, "VAT Nasi Goreng 5,000 stuks" (≈265 min) has failed both earlier phases — all its legal weekdays are at capacity. It is forced-split into two 2,500-stuks chunks (≈135 min each) and placed on two consecutive Wednesdays.

---

## The day builder

`schedule_day` in `frigilunch/application/scheduler.py` takes a list of orders assigned to one day and builds the exact production sequence.

### What it does

1. Sorts orders by: identical-product groups (smallest first), then kapvorm, then ascending allergen level; pins required openers and closers at the ends (S2 rules).
2. Places each order in sequence, computing the changeover before it (`changeover_minutes` from `constraints/changeover.py`).
3. Drops any order that would end after the shift window (22:30 normal days, 20:30 Friday).

### L1/L2 line pairing

Both lines are built on the same day. The day builder:
1. Builds L1 first.
2. Builds L2 while staggering it around any single-unit machine that L1 is using at that moment (`stagger_around` in `constraints/device.py`).
3. If both lines compete for the same scarce machine, the day is built **both ways** (L1 first and L2 first) and the version that places more orders is kept. A tie keeps L1 first.

This means device conflicts are impossible by construction — there is no need to detect and report them after the fact.

> **Example — device staggering:** DMB Kippensoep runs on L1 (starts 06:00) and needs the teriet (one unit in the factory). The teriet is occupied 06:10–06:40. VAT Satéschotel is assigned to L2 the same day and also needs the teriet. L2 starts at 06:30 but the builder defers Satéschotel's teriet step until 06:40 — staggered around L1's hold. Both orders run on the same day without any device conflict.

### CP-SAT sequencer (day_sequencer.py)

After the greedy sort, `solve_optimal_sequence` in `frigilunch/application/day_sequencer.py` runs a CP-SAT prize-collecting TSP on one line's orders. It searches for an order sequence that minimises total changeover time while maximising grouping bonuses (identical products, same kapvorm, same schaal) and penalising idle gaps. The sequencer runs on every committed-window day at final render, and as a drop-recovery step when the greedy walk drops an order.

The sequencer's proposal is always re-verified through the real day-builder walk — it cannot produce a sequence the builder would reject.

> **Example — sequence optimisation:** Three orders on L1: VAT Groenteschotel (schaal C2-1H, 55 min), DMB Kippensoep (schaal C2-1H, 90 min), VAT Bami Goreng (schaal C3-2H, 75 min). The greedy sort groups the two C2-1H orders: Groente → Kippensoep (same schaal, 20 min changeover) → Bami (schaal change, 30 min). Total changeover: 50 min, plus a grouping bonus for the matching-schaal pair. Placing Bami first would still cost 50 min of changeover but lose the bonus — the TSP confirms the greedy order and earns the bonus.

---

## Rolling freeze

`COMMIT_WEEKS = 4` (defined in `frigilunch/domain/state.py`).

Each run:
1. Reads `data/generated/plan_state.json`.
2. Orders in `plan_state.days` whose production date is on or after today's Monday are replayed exactly as stored (stage 5 — apply freeze).
3. After solving and building, the next 4 weeks of newly built days are written back to `plan_state.json` (stage 8 — commit window).

### plan_state.json schema (version 2)

```json
{
  "schema_version": 2,
  "frozen_until": "2026-10-26",
  "days": {
    "2026-10-07": {
      "lijn1": [{"identity": "plan:12345", "aantal": 5000, "start": "06:00", "eind": "09:10", "ombouw_voor": 20}],
      "lijn2": [...],
      "warnings": []
    }
  },
  "completed_orders": {},
  "rejections": {}
}
```

`frozen_until` is the exclusive upper bound of the frozen window. The solver's first available day is `max(plan_start, frozen_until)`.

An order identity is `plan:<plan_nr>` when the order carries a plan number, otherwise a composite `order_nr#art_nr#klant#lever_datum`. The plan number form is stable across re-imports; the composite can collide when ~1.2% of orders share all four fields with a genuine sibling.

---

## Known issues

**Non-determinism across runs.** CP-SAT runs multi-worker under a wall-clock budget. Re-running the same input can land a few orders differently. The per-day sequencer is seeded and single-worker, so the day order is reproducible. Never quote a single run's order count as the definitive answer.

**Order identity collision rate.** `order_nr` in the source system is really the order date, so ~1.2% of orders share order_nr + art_nr + klant + lever_datum with a genuine sibling. A real order ID from the source system would remove the ambiguity.

**Late-arriving demand.** An order whose entire delivery window falls inside an already-frozen week is reported as `ARRIVED-AFTER-FREEZE` and listed in the action CSV. It needs a human decision — no future run can place it automatically.

**Redundant reconciliation pass.** `assign_production_dates_feedback` already calls `reconcile_combined_schedule` at the end of its three-phase loop. The pipeline then calls it again on the same result. The second call is redundant and overwrites rejection reasons (replacing `NEVER-PLACED`/`ORACLE-DROPPED` with `COMBINED-SCHEDULE-DROP`). Removing the outer call is planned.
