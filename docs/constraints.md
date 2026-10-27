# Production scheduling constraints

<!-- GENERATED FILE — do not edit.
     Source: src/constraints/registry.py
     Regenerate: py -m src.constraints.registry -->

**44 constraints — 36 enforced, 8 not.**

Each rule is enforced at exactly one point:

| Point | Meaning | Count |
|---|---|---|
| `FILTER` | Removes candidate (day, line) slots before the solver — cannot be violated by construction | 15 |
| `BUILD` | Shapes the day timeline and may refuse an order | 20 |
| `VERIFY` | Asserts over a built day; reports only | 1 |
| `NONE` | **Not enforced anywhere** | 8 |

## Not enforced

These have a field on `Product` and a column in the spreadsheet, which
makes them look implemented. They are not. Nothing reads them.

| ID | Rule | Where the data goes |
|---|---|---|
| `L4` | Maximum productions per day | Product.maximum_per_dag (parsed, never read) |
| `L5` | Spiral (freezer) availability per line | Product.spiraal_l1 / spiraal_l2 (parsed, never read) |
| `A4` | Halal isolation | Product.halal (parsed, never read) |
| `D3` | Parameterized MEP lead days (>D-1) | Product.mep_lead_days (parsed, never read) |
| `D4` | Special day restrictions string | Product.restricted_days (parsed, never read) |
| `P2` | FTE limits per shift | removed from capacity.json (was staffing.fte_per_shift) |
| `P3` | Skill level requirements | Product.skill_required (computed by loader, never read) |
| `V2` | Speed caps (e.g. 800g on L2) | Product.speed_cap / weight_grams (parsed, never read) |

## Calendar

### `C1` — Shift end time

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.scheduler._schedule_lijn`
- **Backstop:** `src.validator.validate_schedule`
- **Data source:** src/config.py (_SHIFT_END)

22:30 normal days, 20:30 Friday. An order that would end later is refused outright, not warned about. One definition, used by the builder and its validator alike.

### `C2` — Shift start time per line

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.calendar_rules.get_earliest_start`
- **Data source:** src/config.py (_LINE_START)

L1 06:00, L2 06:30 — different crew arrival. No Friday exception.

### `C3` — Production window before delivery

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.constraints.slots.legal_slots`
- **Data source:** data/capacity.json (production_timing)

Production may use any open-horizon day up to the configured minimum-days-before-delivery cutoff. Earliness is a soft objective.

### `C4` — Workdays only

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.constraints.slots.legal_slots`
- **Data source:** src/constraints/slots.py (WERKDAGEN)

Mon-Fri. Weekends are not candidate days.

### `C5` — Frozen days are untouchable

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.constraints.slots.legal_slots`
- **Backstop:** `src.plan_state.block_end`
- **Data source:** data/generated/plan_state.json (frozen_until)

A previous run's commit window bars the DAYS, not just the orders on them — idle capacity inside a frozen week stays idle. Orders whose whole window falls inside it are reported as ARRIVED-AFTER-FREEZE.

## Lijn

### `L1` — Line-only products (alleen_l1 / alleen_l2)

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.constraints.line_rules.permitted_lines`
- **Backstop:** `src.validator.validate_schedule`
- **Data source:** Planning info.xlsx (L1/L2 columns) -> line_rules.derive_line_info

A product usable on only one line never gets a slot on the other.

### `L2` — Explicit line exclusion (kan_niet)

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.constraints.line_rules.permitted_lines`
- **Backstop:** `src.validator.validate_schedule`
- **Data source:** Planning info.xlsx (L1/L2 = kan_niet) -> line_rules.derive_line_info

Beats every other signal including the optimizer's own line pick. Re-checked at BUILD by resolve_lijn so a hand-set order.lijn cannot override it either. Both lines marked kan_niet is read as no restriction, not as impossible.

### `L3` — Line preference (voorkeur) and load balancing

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `src.constraints.line_rules.resolve_lijn`
- **Data source:** Planning info.xlsx (prio columns)

Only consulted when the optimizer left the line unset; it normally picks the line itself. Falls back to balancing production minutes.

### `L4` — Maximum productions per day  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.maximum_per_dag (parsed, never read)

NOT ENFORCED. The field exists on the model and is always 0.

### `L5` — Spiral (freezer) availability per line  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.spiraal_l1 / spiraal_l2 (parsed, never read)

NOT ENFORCED. Both default to True and nothing consults them.

## Capaciteit

### `K1` — Daily planning capacity per line

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.constraints.slots.planning_cap`
- **Data source:** src/config.py (derived: shift window - planning reserve)

The solver books against the cap, which is deliberately smaller than the shift window; the reserve absorbs the gap between its approximate per-order setup buffer and the real sequence-dependent cost.

### `K2` — Real capacity under actual changeovers

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.scheduler._schedule_lijn`
- **Data source:** src/setup_times.json

The oracle. What the solver believed fits is re-checked against real sequence-dependent setups; anything that no longer fits is dropped and becomes a no-good cut for the next solve.

## Ombouw

### `O1` — Changeover time between consecutive orders

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.changeover.calculate_setup_time`
- **Data source:** data/setup_times.json

MAX (never sum) of the applicable scenarios: same scale format 20, different format 30, kapvorm change 15, same-machine floor (Leonard 40, multifill 30), cleaning 30. Steps overlap physically, so the stoppage is the longest component.

### `O2` — Scale format families

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.changeover.schaal_formaat`
- **Data source:** src/constraints/changeover.py (kop equivalence table)

Groups scales by which head they share. An unknown or blank scale is never 'the same format' as anything, including itself.

### `O3` — Daesang fixed-time exception

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.changeover.calculate_setup_time`
- **Data source:** data/setup_times.json (daesang_uitzondering)

Named article pairs (same risotto) get a fixed 20 min instead of the computed time.

### `O4` — Minimize total changeover per day

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `src.day_sequencer.solve_optimal_sequence`
- **Data source:** —

CP-SAT prize-collecting TSP over one line's day. Runs on every committed-window day at final render, and as drop-recovery elsewhere. Its proposal is always re-verified through the real walker. Setup minutes are one term of its objective; G1/G2 adjacency and O6 idle time are the others, and day_quality.line_score applies the same weights when the builder chooses between two builds.

### `O5` — Cluster same-format orders onto the same line

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Data source:** env FRIGI_FORMAAT_SPREAD_PENALTY

Objective penalty per active (day, line, format family). Gives the sequencer more 20-min transitions to find.

### `O6` — No idle holes inside a line-day

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `src.day_sequencer.solve_optimal_sequence`
- **Data source:** env FRIGI_SEQ_IDLE_WEIGHT

Planner, 02/10: plan to the end of the shift, no gaps. Every minute between the line start and its last order that is neither production nor changeover costs the sequence one setup-minute, so an order that would wait hours on a machine the other line holds runs before the block instead of after it.

## Allergenen

### `A1` — Allergen level must not drop without cleaning

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.allergen_rules.allergie_daling`
- **Backstop:** `src.validator.validate_schedule`
- **Data source:** data/setup_times.json (reiniging.allergie_daling)

Levels run ascending through the day. A drop forces a cleaning changeover; the validator errors if one ever appears without it.

### `A2` — Cross-contamination cleaning

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.allergen_rules.check_kruiscontaminatie`
- **Data source:** Planning info.xlsx (allergen columns)

Introducing an allergen the previous product did not carry requires the same cleaning time as a level drop.

### `A3` — Allergen level from X-count

- **Severity:** INFO
- **Enforced at:** `BUILD` — `src.constraints.allergen_rules.allergie_niveau_from_allergenen`
- **Data source:** Planning info.xlsx (column 7) -> legende database row 10

The label IS the level: '-' 0, '!' 1 .. '!!!!!' 5, 'X' (sate) 6. Only when a row carries no label at all is the level inferred from the cross-contamination marker count (0 X -> 0 .. 7+ -> 5).

### `A5` — Sate ('X') stops the other line and closes its own

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.scheduler._build_line_pair`
- **Backstop:** `src.constraints.allergen_rules.check_sate_conflict`
- **Data source:** Planning info.xlsx (allergenen classificatie = 'X') -> allergie_niveau 6

Peanut carries through the hall, so level 6 is the one allergen rule with a cross-line dimension: while sate runs, the other line produces nothing else. Sate beside sate is allowed since the second Gernal (planner 22/10): two sate blocks on one day run side by side and open their windows on the same minute (scheduler._synchronise_sate; the validator warns as SATE_NIET_GELIJKTIJDIG when their starts end up more than FRIGI_SATE_SYNC_TOLERANCE_MIN apart). FRIGI_SATE_PARALLEL=0 restores the old queue, one block behind the other. Sate CLOSES its line — hard since 22/10, after sate nothing else can run there: the walker puts it last (sequence_rules.sate_last), the sequencer has no arc out of it but to more sate, a different sate article included (sequence_rules.sluit_samen), and a product that must close the day cannot share the line-day (A6). The block is slid towards closing time: the rule costs the other line the block's length wherever it sits, but placed anywhere earlier it also costs everything that line would have run afterwards, since a stopped line does not restart. So the other line works a full day and loses only the last hours. Only as late as the other line needs, though: when that line runs out of work earlier, the sate starts the moment it stops (the handover, planner 02/10). The window counts the changeover in front of the block, which is when the peanut arrives at the line. Orders it costs are reported as SATE_DROP, not CAPACITY_DROP: more minutes will not fix them, only a day without sate on the other line. Set FRIGI_SATE_EXCLUSIVE=0 to fall back to plain level-6 handling.

### `A6` — Sate reserves the other line's day in the assignment model

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Data source:** —

A5's cost, in the units the assignment model works in: on a day carrying sate, the other line's ORDINARY minutes are its capacity minus that sate. A line's own sate runs beside the other block (FRIGI_SATE_PARALLEL), so it only has to fit the line's own capacity; in the old queue it is ordinary booked minutes and two blocks charge each other. Also here, as a set constraint: sate and a non-sate product that must close the day never share a line-day, and two sate articles that must close are one closing claim. Sate an earlier phase already placed counts as a constant (_reserved_sate_minutes) — sate products are nearly all single-line, so they are hard orders, so they are placed in phase 1 and are not variables any more by the time the day gets filled around them. Without this the model books both lines full, the builder stops one at the cut-off, and everything behind it returns as a drop for the loop to re-place.

### `A4` — Halal isolation  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.halal (parsed, never read)

NOT ENFORCED. Halal-first-of-day, line separation and the no-pork-on -the-other-line rule are all absent from the running system.

## Productiedagen

### `D1` — Allowed production weekdays per product

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.constraints.slots.legal_slots`
- **Backstop:** `src.validator.validate_schedule`
- **Data source:** Planning info.xlsx (day columns) -> day_rules.allowed_days_for

Chicken Tue/Wed/Fri, fish not on Monday, etc. Also refused at BUILD (constraints.day_rules.check_dag_toegelaten) so a day can never be rendered with an illegal order on it, whatever put it there.

### `D2` — MEP lead time (D-1 / D-2)

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `src.constraints.day_rules.get_mep_date`
- **Backstop:** `src.validator.validate_schedule`
- **Data source:** Planning info.xlsx (MEP columns)

Emits the MEP date per order and warns when it lands on a weekend. Advisory — it does not move production.

### `D3` — Parameterized MEP lead days (>D-1)  **NOT ENFORCED**

- **Severity:** SOFT
- **Enforced at:** `NONE`
- **Data source:** Product.mep_lead_days (parsed, never read)

NOT ENFORCED. Only the fixed D-1 / D-2 split above exists.

### `D4` — Special day restrictions string  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.restricted_days (parsed, never read)

NOT ENFORCED. Superseded in practice by D1's day columns.

## Toestellen

### `M1` — Shared single-unit equipment

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.device_rules.stagger_around`
- **Backstop:** `src.constraints.device_rules.check_toestel_conflict`
- **Data source:** data/capacity.json (toestellen_beschikbaarheid)

One line is scheduled first; the other is staggered around its use of any machine with fewer than 2 units, so a collision is impossible by construction rather than detected afterwards. Machines with 2+ units are never staggered — that would waste real capacity. WHICH line goes first is not fixed: it decides which line absorbs the whole cost of sharing, so on a day where both want the same machine the day is built both ways round and the one that carries more orders wins (a tie keeps L1 first). See scheduler._build_line_pair. Gernal and Leonard have 2 units since the 02/10 meeting, so they are never staggered.

### `M2` — Device conflicts become solver cuts

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Data source:** —

Evidence comes from two places. A pair caught OVERLAPPING in time on one machine is forbidden from sharing any day for the rest of the run (day-scoped under FRIGI_DEVICE_CUT_DAY_SCOPED) — but that almost never fires, because M1's staggering is precisely what removes the overlap. The real source is a pair where the day builder LOST an order to the other line's hold on the machine (DaySchedule.device_dropped, proven by counterfactual: the order fits the same line with the block removed). Those are always scoped to the day observed — it is a statement about that day's timing, not about the pair. See docs/toestel-cut-gap.md.

## Personeel

### `P1` — Headcount ramp between consecutive orders

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `src.constraints.staffing_rules.check_bezetting_continuity`
- **Data source:** data/capacity.json (staffing.max_bezetting_delta)

Only INCREASES are capped — you cannot conjure staff mid-shift. A drop of any size is fine. A big jump is allowed anyway if the incoming order runs longer than 4h, since staff can arrive gradually within it. The day sequencer prunes the arc entirely rather than paying for it.

### `P2` — FTE limits per shift  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** removed from capacity.json (was staffing.fte_per_shift)

NOT ENFORCED. The config block was dead and has been deleted.

### `P3` — Skill level requirements  **NOT ENFORCED**

- **Severity:** SOFT
- **Enforced at:** `NONE`
- **Data source:** Product.skill_required (computed by loader, never read)

NOT ENFORCED.

## Snelheid

### `V1` — Production duration from line speed

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.changeover.calculate_duration`
- **Data source:** Planning info.xlsx (speed per line)

aantal / snelheid, rounded up to 5 minutes.

### `V2` — Speed caps (e.g. 800g on L2)  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.speed_cap / weight_grams (parsed, never read)

NOT ENFORCED. Duration always uses the raw sheet speed.

## Sequentie

### `S1` — Identical products, then kapvorm, then ascending allergen level

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `src.constraints.sequence_rules.sort_orders_for_lijn`
- **Data source:** —

The fixed greedy order: identical-product blocks (G1, smallest first), grouped by kapvorm (G2; no kapvorm falls through), then ascending allergen level. Also decides which orders die first when a day overflows: smaller ones, not whatever happened to be last in the list.

### `S2` — Must / cannot be first or last of day

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.constraints.sequence_rules.apply_positie_regels`
- **Backstop:** `src.validator.validate_schedule`
- **Data source:** Planning info.xlsx cols 28-31 -> Product.opstart_moet / opstart_kan_niet / afsluit_moet / afsluit_kan_niet

Prohibitions AND requirements are hard (planner 15/10: 'set in stone'). A 'moet' product must sit in the run of one article that opens (closes) the day, so two orders of one article may share the end, two articles may not. Enforced in the assignment model (at most one claiming article per line-day and end, FRIGI_POSITIE_MOET_HARD), the greedy sort, the sequencer (missing arcs) and scheduler._enforce_positie_regels, which gives up an order rather than the rule; the validator errors on any survivor. Can reject an order, which is the trade the planner chose.

### `S3` — A line-day's set can supply both its ends

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Backstop:** `src.scheduler._enforce_positie_regels`
- **Data source:** Planning info.xlsx cols 28-31 (same flags as S2)

S2 read as a property of the SET on a (day, line), so the solver cannot propose a day the builder would have to break up. A non-empty line-day needs an order allowed to open it and one allowed to close it, and — since those are two different orders — a line-day carrying two or more orders needs TWO that can take an end. Both are necessary conditions, so neither can exclude a feasible day. Orders an earlier phase already placed count toward this (they reach the model as a minutes budget, with their end capability alongside it); without that the rule used to be switched off for every day an earlier phase had touched.

## Groepering

### `G1` — Identical products produced together, smallest first

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Data source:** Planning info.xlsx col 'identiek product' -> grouping_rules.identiek_groepen

Planner, 02/10. Column 15 is a link list; the groups are its connected components (Product.identiek_groep). The assignment model pays FRIGI_IDENTIEK_SPLIT_PENALTY per (day, line) a group is spread over, the day sort keeps a group contiguous smallest first, the sequencer pays a bonus per back-to-back arc. Soft everywhere: a group that cannot share a day is split, never rejected.

### `G2` — Same kapvorm together, within a day and over days

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Data source:** Planning info.xlsx (kapvorm) -> grouping_rules.kapvorm_sleutel

Planner, 02/10 and 15/10, second layer under G1. FRIGI_KAPVORM_SPREAD_PENALTY per active (day, line, kapvorm), FRIGI_KAPVORM_CAMPAIGN_PENALTY per campaign start (active where the line's previous workday was not), kapvorm groups in the day sort, a sequencer bonus per back-to-back arc and for opening on the kapvorm the line closed on the day before. 'NVT' (nearly all of L2) has no kapvorm, so there the layer falls through. On L1, the automatic packing line, it outranks every other preference (planner 22/10): FRIGI_L1_KAPVORM_CHANGE_PENALTY (60, above the line preference) per extra kapvorm on an L1 line-day and per L1 campaign start across a seam; FRIGI_SEQ_L1_KAPVORM_BONUS (2000, above identical pairs and any idle or changeover) per arc in the sequencer; and the regroup pass (campaign_regroup) minimises L1 kapvorm changes over the committed window before anything else, NVT counted as a kapvorm of its own. Hard rules and the order count still come first.

### `G3` — Same schaal together, within a day and over days

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Data source:** Planning info.xlsx (schaal) -> grouping_rules.schaal_sleutel

Planner, 15/10, next to G2 and priced just below it: FRIGI_SCHAAL_SPREAD_PENALTY and FRIGI_SCHAAL_CAMPAIGN_PENALTY in the assignment model, schaal sub-groups inside each kapvorm group in the day sort, FRIGI_SEQ_SCHAAL_BONUS in the sequencer. The exact tray, not the changeover family O2 uses. The layer that counts on L2.

## Integriteit

### `X1` — No partial delivery of a split order

- **Severity:** HARD
- **Enforced at:** `FILTER` — `src.scheduler_ortools_v3.assign_production_dates_feedback`
- **Backstop:** `src.plan_state.build_new_state`
- **Data source:** —

An order counts as scheduled only if every chunk survives the oracle, and chunks are committed all-or-nothing.

### `X2` — Every order is scheduled or rejected, exactly once

- **Severity:** HARD
- **Enforced at:** `VERIFY` — `src.totals.compute`
- **Backstop:** `src.totals.report_imbalance`
- **Data source:** —

The accounting invariant. A run that loses an order says so loudly instead of shipping a plan whose numbers do not add up.

### `X3` — Frozen days replay byte-for-byte

- **Severity:** HARD
- **Enforced at:** `BUILD` — `src.scheduler.replay_locked_days`
- **Data source:** data/generated/plan_state.json (render)

A committed day is copied from its persisted time slots, never re-derived — re-running the greedy walk on a shrunk order set is not guaranteed to reproduce the same result.
