# Production scheduling constraints

> Generated from the constraint modules — last updated manually; run the constraint registry to regenerate.
> All module references use `frigilunch/domain/constraints/` (the current package; the old `src/constraints/` is gone).

**44 constraints — 36 enforced, 8 not.**

Each rule is enforced at exactly one point:

| Point | Meaning | Count |
|---|---|---|
| `FILTER` | Removes candidate (day, line) slots before the solver — cannot be violated by construction | 15 |
| `BUILD` | Shapes the day timeline and may refuse an order | 20 |
| `VERIFY` | Asserts over a built day; reports only | 1 |
| `NONE` | **Not enforced anywhere** | 8 |

## Not enforced

These have a field on `Product` and a column in the spreadsheet, which makes them look implemented. They are not. Nothing reads them.

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
- **Enforced at:** `BUILD` — `frigilunch/application/scheduler.py` (`_schedule_lijn`)
- **Backstop:** `frigilunch/domain/validator.py` (`validate_schedule`)
- **Data source:** `frigilunch/adapters/config.py` (`_SHIFT_END`)

22:30 normal days, 20:30 Friday. An order that would end later is refused outright, not warned about. One definition, used by the builder and its validator alike.

> **Example:** VAT Groenteschotel starts at 20:10 on a Friday (shift end 20:30). It runs 35 min → would end at 20:45 → refused. Moved to Monday where the 22:30 window gives it room.

### `C2` — Shift start time per line

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/calendar_rules.py` (`get_earliest_start`)
- **Data source:** `frigilunch/adapters/config.py` (`_LINE_START`)

L1 06:00, L2 06:30 — different crew arrival. No Friday exception.

### `C3` — Production window before delivery

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/domain/constraints/slots.py` (`legal_slots`)
- **Data source:** `data/capacity.json` (production_timing)

Production may use any open-horizon day up to the configured minimum-days-before-delivery cutoff. Earliness is a soft objective.

### `C4` — Workdays only

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/domain/constraints/slots.py` (`legal_slots`)
- **Data source:** `frigilunch/domain/constraints/slots.py` (WERKDAGEN)

Mon-Fri. Weekends are not candidate days.

### `C5` — Frozen days are untouchable

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/domain/constraints/slots.py` (`legal_slots`)
- **Backstop:** `frigilunch/domain/state.py` (`block_end`)
- **Data source:** `data/generated/plan_state.json` (frozen_until)

A previous run's commit window bars the DAYS, not just the orders on them — idle capacity inside a frozen week stays idle. Orders whose whole window falls inside it are reported as ARRIVED-AFTER-FREEZE.

> **Example:** Weeks 44–47 were committed on Oct 27. An order arriving Nov 3 with delivery date Nov 7 has its entire window inside the frozen zone — every possible production day is locked. It cannot be placed and appears in `actie_vereist_*.csv` as `ARRIVED-AFTER-FREEZE`.

## Lijn

### `L1` — Line-only products (alleen_l1 / alleen_l2)

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/domain/constraints/line_rules.py` (`permitted_lines`)
- **Backstop:** `frigilunch/domain/validator.py` (`validate_schedule`)
- **Data source:** Planning info.xlsx (L1/L2 columns) → `line_rules.derive_line_info`

A product usable on only one line never gets a slot on the other.

> **Example:** DMB Kippensoep has `alleen_l1=True`. The solver may only propose Monday L1, Tuesday L1, etc. — never L2. Any hand-set `order.lijn=2` is overridden at BUILD by `resolve_lijn`.

### `L2` — Explicit line exclusion (kan_niet)

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/domain/constraints/line_rules.py` (`permitted_lines`)
- **Backstop:** `frigilunch/domain/validator.py` (`validate_schedule`)
- **Data source:** Planning info.xlsx (L1/L2 = kan_niet) → `line_rules.derive_line_info`

Beats every other signal including the optimizer's own line pick. Re-checked at BUILD by `resolve_lijn` so a hand-set `order.lijn` cannot override it either. Both lines marked `kan_niet` is read as no restriction, not as impossible.

### `L3` — Line preference (voorkeur) and load balancing

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/line_rules.py` (`resolve_lijn`)
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
- **Enforced at:** `FILTER` — `frigilunch/domain/constraints/slots.py` (`planning_cap`)
- **Data source:** `frigilunch/adapters/config.py` (derived: shift window − planning reserve)

The solver books against the cap, which is deliberately smaller than the shift window; the reserve absorbs the gap between its approximate per-order setup buffer and the real sequence-dependent cost.

### `K2` — Real capacity under actual changeovers

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/application/scheduler.py` (`_schedule_lijn`)
- **Data source:** `data/setup_times.json`

The oracle. What the solver believed fits is re-checked against real sequence-dependent setups; anything that no longer fits is dropped and becomes a no-good cut for the next solve.

## Ombouw

### `O1` — Changeover time between consecutive orders

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/changeover.py` (`changeover_minutes`)
- **Data source:** `data/setup_times.json`

MAX (never sum) of the applicable scenarios: same scale format 20 min, different format 30 min, kapvorm change 15 min, same-machine floor (Leonard 40, multifill 30), allergen cleaning 30 min. Steps overlap physically, so the stoppage is the longest component.

> **Example:** DMB Kippensoep (schaal C2-1H, allergie_niveau 1) followed by VAT Satéschotel (schaal C3-2H, allergie_niveau 6). Components: schaal change 30 min, allergen cleaning 30 min. MAX(30, 30) = **30 min** changeover — not 60. Satéschotel followed by Kippensoep would be a violation of A1 (level drops 6→1 without cleaning) and is blocked before it can be scheduled.

### `O2` — Scale format families

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/changeover.py` (`schaal_formaat`)
- **Data source:** `frigilunch/domain/constraints/changeover.py` (format equivalence table)

Groups scales by which head they share. An unknown or blank scale is never "the same format" as anything, including itself.

### `O3` — Daesang fixed-time exception

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/changeover.py` (`changeover_minutes`)
- **Data source:** `data/setup_times.json` (daesang_uitzondering)

Named article pairs (same risotto) get a fixed 20 min instead of the computed time.

### `O4` — Minimize total changeover per day

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `frigilunch/application/day_sequencer.py` (`solve_optimal_sequence`)
- **Data source:** —

CP-SAT prize-collecting TSP over one line's day. Runs on every committed-window day at final render, and as drop-recovery elsewhere. Its proposal is always re-verified through the real walker.

### `O5` — Cluster same-format orders onto the same line

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Data source:** env `FRIGI_FORMAAT_SPREAD_PENALTY`

Objective penalty per active (day, line, format family). Gives the sequencer more 20-min transitions to find.

### `O6` — No idle holes inside a line-day

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `frigilunch/application/day_sequencer.py` (`solve_optimal_sequence`)
- **Data source:** env `FRIGI_SEQ_IDLE_WEIGHT`

Every minute between the line start and its last order that is neither production nor changeover costs the sequence one setup-minute, so an order that would wait hours on a machine the other line holds runs before the block instead of after it.

## Allergenen

### `A1` — Allergen level must not drop without cleaning

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/allergen.py` (`allergie_daling`)
- **Backstop:** `frigilunch/domain/validator.py` (`validate_schedule`)
- **Data source:** `data/setup_times.json` (reiniging.allergie_daling)

Levels run ascending through the day. A drop forces a cleaning changeover; the validator errors if one ever appears without it.

> **Example:** Day sequence on L1: Kippensoep (niveau 1) → Bami Goreng (niveau 1, same level, OK) → Satéschotel (niveau 6, rises, OK). Placing Kippensoep after Satéschotel would drop from 6 to 1 without cleaning → build refuses the order and the validator would flag it as ALLERGIE_DALING.

### `A2` — Cross-contamination cleaning

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/allergen.py` (`check_kruiscontaminatie`)
- **Data source:** Planning info.xlsx (allergen columns)

Introducing an allergen the previous product did not carry requires the same cleaning time as a level drop.

### `A3` — Allergen level from X-count

- **Severity:** INFO
- **Enforced at:** `BUILD` — loader (`allergie_niveau_from_allergenen`)
- **Data source:** Planning info.xlsx (column 7)

The label IS the level: '-' → 0, '!' → 1 … '!!!!!' → 5, 'X' (sate) → 6. Only when a row carries no label at all is the level inferred from the cross-contamination marker count.

### `A5` — Sate ('X') stops the other line and closes its own

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/application/scheduler.py` (`_build_line_pair`)
- **Backstop:** `frigilunch/domain/constraints/allergen.py` (`check_sate_conflict`)
- **Data source:** Planning info.xlsx (allergenen classificatie = 'X') → allergie_niveau 6

Peanut carries through the hall, so level 6 is the one allergen rule with a cross-line dimension: while sate runs, the other line produces nothing else. Two sate blocks on one day can run side by side (FRIGI_SATE_PARALLEL=1, default); set FRIGI_SATE_PARALLEL=0 to queue them. Sate closes its line — after sate, nothing else can run there. Set FRIGI_SATE_EXCLUSIVE=0 to fall back to plain level-6 handling.

> **Example:** VAT Satéschotel (allergie_niveau 6) is placed on Wednesday L2 from 14:00–16:30. During that window, L1 may not run any other product — those orders are staggered to end before 14:00 or start after 16:30. Nothing may run on L2 after 16:30 that day (line closed). If SATE_PARALLEL=1 and a second sate order exists, it runs in parallel on L2 during the same window.

### `A6` — Sate reserves the other line's day in the assignment model

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Data source:** —

A5's cost in assignment-model terms: on a day carrying sate, the other line's ordinary bookable minutes are reduced by the sate block's length. Also enforced as a set constraint: sate and a non-sate product that must close the day never share a line-day.

### `A4` — Halal isolation  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.halal (parsed, never read)

NOT ENFORCED.

## Productiedagen

### `D1` — Allowed production weekdays per product

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/domain/constraints/slots.py` (`legal_slots`)
- **Backstop:** `frigilunch/domain/validator.py` (`validate_schedule`)
- **Data source:** Planning info.xlsx (day columns) → `day_rules.allowed_days_for`

Chicken Tue/Wed/Fri, fish not on Monday, etc. Also refused at BUILD so a day can never be rendered with an illegal order on it, whatever put it there.

> **Example:** DMB Kippensoep is allowed Tue, Wed, Fri only. The solver will never propose Monday or Thursday as a slot for it. If for any reason it were placed on a Monday, the BUILD stage would refuse it and the validator would flag DAG_RESTRICTIE.

### `D2` — MEP lead time (D-1 / D-2)

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/day_rules.py` (`get_mep_date`)
- **Backstop:** `frigilunch/domain/validator.py` (`validate_schedule`)
- **Data source:** Planning info.xlsx (MEP columns)

Emits the MEP date per order and warns when it lands on a weekend. Advisory — it does not move production.

### `D3` — Parameterized MEP lead days  **NOT ENFORCED**

- **Severity:** SOFT
- **Enforced at:** `NONE`
- **Data source:** Product.mep_lead_days (parsed, never read)

NOT ENFORCED. Only the fixed D-1 / D-2 split above exists.

### `D4` — Special day restrictions string  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.restricted_days (parsed, never read)

NOT ENFORCED. Superseded by D1's day columns.

## Toestellen

### `M1` — Shared single-unit equipment

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/device.py` (`stagger_around`)
- **Backstop:** `frigilunch/domain/constraints/device.py` (`check_toestel_conflict`)
- **Data source:** `data/capacity.json` (toestellen_beschikbaarheid)

One line is scheduled first; the other is staggered around its use of any machine with fewer than 2 units, so a collision is impossible by construction. Machines with 2+ units are never staggered. Which line goes first is not fixed: on a day where both lines want the same machine, the day is built both ways and the version that places more orders wins (tie keeps L1 first).

> **Example:** DMB Kippensoep (L1, needs teriet 06:10–06:40) and VAT Satéschotel (L2, needs teriet). There is 1 teriet unit. Builder schedules L1 first, records teriet busy 06:10–06:40, then builds L2 with Satéschotel's teriet step deferred to 06:40. Zero conflict — by construction, no post-hoc detection needed.

### `M2` — Device conflicts become solver cuts

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Data source:** —

A pair proven to cause a device conflict is forbidden from sharing a day for the rest of the run.

## Personeel

### `P1` — Headcount ramp between consecutive orders

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/staffing.py` (`check_bezetting_continuity`)
- **Data source:** `data/capacity.json` (staffing.max_bezetting_delta)

Only increases are capped — you cannot conjure staff mid-shift. A drop of any size is fine. A large jump is allowed when the incoming order runs longer than 4 hours.

### `P2` — FTE limits per shift  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** removed from capacity.json

NOT ENFORCED.

### `P3` — Skill level requirements  **NOT ENFORCED**

- **Severity:** SOFT
- **Enforced at:** `NONE`
- **Data source:** Product.skill_required (computed by loader, never read)

NOT ENFORCED.

## Snelheid

### `V1` — Production duration from line speed

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/changeover.py` (`production_minutes`)
- **Data source:** Planning info.xlsx (speed per line)

`aantal / snelheid`, rounded up to 5 minutes.

### `V2` — Speed caps  **NOT ENFORCED**

- **Severity:** HARD
- **Enforced at:** `NONE`
- **Data source:** Product.speed_cap / weight_grams (parsed, never read)

NOT ENFORCED. Duration always uses the raw sheet speed.

## Sequentie

### `S1` — Identical products, then kapvorm, then ascending allergen level

- **Severity:** SOFT
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/sequencing.py` (`sort_orders_for_lijn`)
- **Data source:** —

The fixed greedy order: identical-product blocks (G1, smallest first), grouped by kapvorm (G2), then ascending allergen level.

### `S2` — Must / cannot be first or last of day

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/domain/constraints/sequencing.py` (`apply_positie_regels`)
- **Backstop:** `frigilunch/domain/validator.py` (`validate_schedule`)
- **Data source:** Planning info.xlsx cols 28-31 → Product.opstart_moet / opstart_kan_niet / afsluit_moet / afsluit_kan_niet

Prohibitions AND requirements are hard. A `moet` product must sit in the run of one article that opens (closes) the day. Enforced in the assignment model, the greedy sort, the sequencer, and the day builder.

> **Example:** Product X has `opstart_moet=True` (must open the day). If X is on L1 on Wednesday, it is pinned to position 0 — no other order can precede it. If a second `opstart_moet` product is also on L1 Wednesday, the day builder drops one of them (two products cannot both be first). The solver avoids proposing such a combination via S3.

### `S3` — A line-day's set can supply both its ends

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Backstop:** `frigilunch/application/scheduler.py` (`_enforce_positie_regels`)
- **Data source:** Planning info.xlsx cols 28-31 (same flags as S2)

S2 read as a property of the SET on a (day, line): the solver cannot propose a day the builder would have to break up. A non-empty line-day needs an order allowed to open it and one allowed to close it.

## Groepering

### `G1` — Identical products produced together, smallest first

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Data source:** Planning info.xlsx col 'identiek product' → `grouping_rules.identiek_groepen`

Column 15 is a link list; the groups are its connected components (Product.identiek_groep). The assignment model pays `FRIGI_IDENTIEK_SPLIT_PENALTY` per (day, line) a group is spread over. Soft everywhere: a group that cannot share a day is split, never rejected.

### `G2` — Same kapvorm together, within a day and over days

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Data source:** Planning info.xlsx (kapvorm) → `grouping_rules.kapvorm_sleutel`

`FRIGI_KAPVORM_SPREAD_PENALTY` per active (day, line, kapvorm), `FRIGI_KAPVORM_CAMPAIGN_PENALTY` per campaign start. On L1, outranks every other preference.

### `G3` — Same schaal together, within a day and over days

- **Severity:** SOFT
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Data source:** Planning info.xlsx (schaal) → `grouping_rules.schaal_sleutel`

`FRIGI_SCHAAL_SPREAD_PENALTY` and `FRIGI_SCHAAL_CAMPAIGN_PENALTY` in the assignment model. The exact tray, not the changeover family O2 uses.

## Integriteit

### `X1` — No partial delivery of a split order

- **Severity:** HARD
- **Enforced at:** `FILTER` — `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`)
- **Backstop:** `frigilunch/domain/state.py` (`build_new_state`)
- **Data source:** —

An order counts as scheduled only if every chunk survives the oracle, and chunks are committed all-or-nothing.

### `X2` — Every order is scheduled or rejected, exactly once

- **Severity:** HARD
- **Enforced at:** `VERIFY` — `frigilunch/application/totals.py` (`compute`)
- **Backstop:** `frigilunch/application/totals.py` (`report_imbalance`)
- **Data source:** —

The accounting invariant. A run that loses an order says so loudly instead of shipping a plan whose numbers do not add up.

### `X3` — Frozen days replay byte-for-byte

- **Severity:** HARD
- **Enforced at:** `BUILD` — `frigilunch/application/scheduler.py` (`replay_locked_days`)
- **Data source:** `data/generated/plan_state.json`

A committed day is copied from its persisted time slots, never re-derived.
