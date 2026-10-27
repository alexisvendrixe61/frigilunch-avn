"""
registry.py — the catalogue. Every production rule this system knows about,
what it means, and **the single place it is enforced**.

## Why this exists

"Where are the constraints?" had no answer. A rule could be enforced in the
solver's slot filter, in the day builder's greedy walk, in the day sequencer's
arc pruning, in `validator.py`, or in a 2,000-line engine that nothing
imported — and nothing told you which. Worse, several rules were *documented*
as enforced while being enforced nowhere at all.

## The three enforcement points

Every constraint is enforced at exactly one of these, and says which:

  FILTER  Removes candidate (day, line) slots before the solver ever sees
          them. If a rule can be expressed as "this order may not run here",
          it belongs here — the solver then cannot violate it by construction.
          See `constraints/slots.py`.

  BUILD   Shapes the minute-by-minute day and may REFUSE an order. Anything
          sequence-dependent (changeover cost, allergen ordering, shared
          equipment, staffing ramp) can only be known here.
          See `scheduler.schedule_day`.

  VERIFY  Asserts over an already-built day. Reports, never changes. A rule
          whose only enforcement is VERIFY is not enforced — that is a bug,
          and `test_registry.py` fails on it.

  NONE    Not enforced anywhere. Listed honestly rather than omitted, because
          several of these have data in the spreadsheet and a field on
          `Product`, which makes them look implemented.

## The self-check

`impl` is a dotted path to the live implementation. `verify_registry()`
resolves every one of them, so this file cannot claim an implementation that
does not exist, and a rename that misses the registry fails the test suite
rather than silently producing a lying document.

`docs/constraints.md` is GENERATED from this table (`py -m src.constraints.registry`).
Do not hand-edit it — it drifted for exactly that reason before.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from enum import Enum


class Point(str, Enum):
    FILTER = "FILTER"
    BUILD = "BUILD"
    VERIFY = "VERIFY"
    NONE = "NONE"


class Severity(str, Enum):
    HARD = "HARD"     # a plan violating this is wrong and must not ship
    SOFT = "SOFT"     # preference; the objective pays for breaking it
    INFO = "INFO"     # reported for the planner, never blocks


@dataclass(frozen=True)
class Constraint:
    id: str
    name: str
    category: str
    severity: Severity
    point: Point
    impl: str = ""            # dotted path to the live implementation
    backstop: str = ""        # optional VERIFY check that double-checks it
    data_source: str = ""
    notes: str = ""
    # Required when `impl` lives outside src/constraints/. Some rules genuinely
    # belong with the machinery that applies them — a CP-SAT objective term is
    # not extractable, and neither is "refuse the order that does not fit" from
    # the walk that places them. This field keeps that a short, deliberate list
    # rather than somewhere rules quietly accumulate; verify_registry() rejects
    # an outside impl without one.
    outside_because: str = ""

    @property
    def implemented(self) -> bool:
        return self.point is not Point.NONE

    @property
    def inside_package(self) -> bool:
        return self.impl.startswith("src.constraints.")


# ─────────────────────────────────────────────────────────────────────────────
# The catalogue
# ─────────────────────────────────────────────────────────────────────────────

REGISTRY: list[Constraint] = [
    # ── Calendar ────────────────────────────────────────────────────────────
    Constraint(
        id="C1", name="Shift end time", category="calendar",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.scheduler._schedule_lijn",
        backstop="src.validator.validate_schedule",
        data_source="src/config.py (_SHIFT_END)",
        outside_because="Inseparable from the day walk: placing orders in sequence and refusing the one that runs past close is a single algorithm.",
        notes="22:30 normal days, 20:30 Friday. An order that would end later is "
              "refused outright, not warned about. One definition, used by the "
              "builder and its validator alike.",
    ),
    Constraint(
        id="C2", name="Shift start time per line", category="calendar",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.calendar_rules.get_earliest_start",
        data_source="src/config.py (_LINE_START)",
        notes="L1 06:00, L2 06:30 — different crew arrival. No Friday exception.",
    ),
    Constraint(
        id="C3", name="Production window before delivery", category="calendar",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.constraints.slots.legal_slots",
        data_source="data/capacity.json (production_timing)",
        notes="Production may use any open-horizon day up to the configured "
              "minimum-days-before-delivery cutoff. Earliness is a soft objective.",
    ),
    Constraint(
        id="C4", name="Workdays only", category="calendar",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.constraints.slots.legal_slots",
        data_source="src/constraints/slots.py (WERKDAGEN)",
        notes="Mon-Fri. Weekends are not candidate days.",
    ),
    Constraint(
        id="C5", name="Frozen days are untouchable", category="calendar",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.constraints.slots.legal_slots",
        backstop="src.plan_state.block_end",
        data_source="data/generated/plan_state.json (frozen_until)",
        notes="A previous run's commit window bars the DAYS, not just the orders on "
              "them — idle capacity inside a frozen week stays idle. Orders whose "
              "whole window falls inside it are reported as ARRIVED-AFTER-FREEZE.",
    ),

    # ── Lines ───────────────────────────────────────────────────────────────
    Constraint(
        id="L1", name="Line-only products (alleen_l1 / alleen_l2)", category="lijn",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.constraints.line_rules.permitted_lines",
        backstop="src.validator.validate_schedule",
        data_source="Planning info.xlsx (L1/L2 columns) -> line_rules.derive_line_info",
        notes="A product usable on only one line never gets a slot on the other.",
    ),
    Constraint(
        id="L2", name="Explicit line exclusion (kan_niet)", category="lijn",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.constraints.line_rules.permitted_lines",
        backstop="src.validator.validate_schedule",
        data_source="Planning info.xlsx (L1/L2 = kan_niet) -> line_rules.derive_line_info",
        notes="Beats every other signal including the optimizer's own line pick. "
              "Re-checked at BUILD by resolve_lijn so a hand-set order.lijn cannot "
              "override it either. Both lines marked kan_niet is read as no "
              "restriction, not as impossible.",
    ),
    Constraint(
        id="L3", name="Line preference (voorkeur) and load balancing", category="lijn",
        severity=Severity.SOFT, point=Point.BUILD,
        impl="src.constraints.line_rules.resolve_lijn",
        data_source="Planning info.xlsx (prio columns)",
        notes="Only consulted when the optimizer left the line unset; it normally "
              "picks the line itself. Falls back to balancing production minutes.",
    ),
    Constraint(
        id="L4", name="Maximum productions per day", category="lijn",
        severity=Severity.HARD, point=Point.NONE,
        data_source="Product.maximum_per_dag (parsed, never read)",
        notes="NOT ENFORCED. The field exists on the model and is always 0.",
    ),
    Constraint(
        id="L5", name="Spiral (freezer) availability per line", category="lijn",
        severity=Severity.HARD, point=Point.NONE,
        data_source="Product.spiraal_l1 / spiraal_l2 (parsed, never read)",
        notes="NOT ENFORCED. Both default to True and nothing consults them.",
    ),

    # ── Capacity ────────────────────────────────────────────────────────────
    Constraint(
        id="K1", name="Daily planning capacity per line", category="capaciteit",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.constraints.slots.planning_cap",
        data_source="src/config.py (derived: shift window - planning reserve)",
        notes="The solver books against the cap, which is deliberately smaller than "
              "the shift window; the reserve absorbs the gap between its approximate "
              "per-order setup buffer and the real sequence-dependent cost.",
    ),
    Constraint(
        id="K2", name="Real capacity under actual changeovers", category="capaciteit",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.scheduler._schedule_lijn",
        data_source="src/setup_times.json",
        outside_because="This IS the oracle - the real day build is what defines real capacity. There is no smaller thing to extract.",
        notes="The oracle. What the solver believed fits is re-checked against real "
              "sequence-dependent setups; anything that no longer fits is dropped and "
              "becomes a no-good cut for the next solve.",
    ),

    # ── Changeovers ─────────────────────────────────────────────────────────
    Constraint(
        id="O1", name="Changeover time between consecutive orders", category="ombouw",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.changeover.calculate_setup_time",
        data_source="data/setup_times.json",
        notes="MAX (never sum) of the applicable scenarios: same scale format 20, "
              "different format 30, kapvorm change 15, same-machine floor (Leonard 40, "
              "multifill 30), cleaning 30. Steps overlap physically, so the stoppage "
              "is the longest component.",
    ),
    Constraint(
        id="O2", name="Scale format families", category="ombouw",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.changeover.schaal_formaat",
        data_source="src/constraints/changeover.py (kop equivalence table)",
        notes="Groups scales by which head they share. An unknown or blank scale is "
              "never 'the same format' as anything, including itself.",
    ),
    Constraint(
        id="O3", name="Daesang fixed-time exception", category="ombouw",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.changeover.calculate_setup_time",
        data_source="data/setup_times.json (daesang_uitzondering)",
        notes="Named article pairs (same risotto) get a fixed 20 min instead of the "
              "computed time.",
    ),
    Constraint(
        id="O4", name="Minimize total changeover per day", category="ombouw",
        severity=Severity.SOFT, point=Point.BUILD,
        impl="src.day_sequencer.solve_optimal_sequence",
        outside_because="An optimizer, not a rule: it searches for a better sequence and its proposal is re-verified through the builder.",
        data_source="—",
        notes="CP-SAT prize-collecting TSP over one line's day. Runs on every "
              "committed-window day at final render, and as drop-recovery elsewhere. "
              "Its proposal is always re-verified through the real walker. Setup "
              "minutes are one term of its objective; G1/G2 adjacency and O6 idle "
              "time are the others, and day_quality.line_score applies the same "
              "weights when the builder chooses between two builds.",
    ),
    Constraint(
        id="O5", name="Cluster same-format orders onto the same line", category="ombouw",
        severity=Severity.SOFT, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="A CP-SAT objective term; it exists only as a weight inside the solver model.",
        data_source="env FRIGI_FORMAAT_SPREAD_PENALTY",
        notes="Objective penalty per active (day, line, format family). Gives the "
              "sequencer more 20-min transitions to find.",
    ),
    Constraint(
        id="O6", name="No idle holes inside a line-day", category="ombouw",
        severity=Severity.SOFT, point=Point.BUILD,
        impl="src.day_sequencer.solve_optimal_sequence",
        outside_because="A term in the CP-SAT day sequencer's objective, next to O4.",
        data_source="env FRIGI_SEQ_IDLE_WEIGHT",
        notes="Planner, 02/10: plan to the end of the shift, no gaps. Every minute "
              "between the line start and its last order that is neither production "
              "nor changeover costs the sequence one setup-minute, so an order that "
              "would wait hours on a machine the other line holds runs before the "
              "block instead of after it.",
    ),

    # ── Allergens ───────────────────────────────────────────────────────────
    Constraint(
        id="A1", name="Allergen level must not drop without cleaning", category="allergenen",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.allergen_rules.allergie_daling",
        backstop="src.validator.validate_schedule",
        data_source="data/setup_times.json (reiniging.allergie_daling)",
        notes="Levels run ascending through the day. A drop forces a cleaning "
              "changeover; the validator errors if one ever appears without it.",
    ),
    Constraint(
        id="A2", name="Cross-contamination cleaning", category="allergenen",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.allergen_rules.check_kruiscontaminatie",
        data_source="Planning info.xlsx (allergen columns)",
        notes="Introducing an allergen the previous product did not carry requires "
              "the same cleaning time as a level drop.",
    ),
    Constraint(
        id="A3", name="Allergen level from X-count", category="allergenen",
        severity=Severity.INFO, point=Point.BUILD,
        impl="src.constraints.allergen_rules.allergie_niveau_from_allergenen",
        data_source="Planning info.xlsx (column 7) -> legende database row 10",
        notes="The label IS the level: '-' 0, '!' 1 .. '!!!!!' 5, 'X' (sate) 6. "
              "Only when a row carries no label at all is the level inferred from "
              "the cross-contamination marker count (0 X -> 0 .. 7+ -> 5).",
    ),
    Constraint(
        id="A5", name="Sate ('X') stops the other line and closes its own", category="allergenen",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.scheduler._build_line_pair",
        backstop="src.constraints.allergen_rules.check_sate_conflict",
        data_source="Planning info.xlsx (allergenen classificatie = 'X') -> allergie_niveau 6",
        outside_because="Same shape as M1 and inseparable from the same walk: one line's block is placed, the other line's day is cut off in front of it, and only the builder knows either timeline.",
        notes="Peanut carries through the hall, so level 6 is the one allergen rule "
              "with a cross-line dimension: while sate runs, the other line produces "
              "nothing else. Sate beside sate is allowed since the second Gernal "
              "(planner 22/10): two sate blocks on one day run side by side and open "
              "their windows on the same minute (scheduler._synchronise_sate; the "
              "validator warns as SATE_NIET_GELIJKTIJDIG when their starts end up more "
              "than FRIGI_SATE_SYNC_TOLERANCE_MIN apart). FRIGI_SATE_PARALLEL=0 "
              "restores the old queue, one block behind the other. Sate CLOSES its "
              "line — hard since 22/10, after sate nothing else can run there: the "
              "walker puts it last (sequence_rules.sate_last), the sequencer has no "
              "arc out of it but to more sate, a different sate article included "
              "(sequence_rules.sluit_samen), and a product that must close the day "
              "cannot share the line-day (A6). The block is slid towards closing "
              "time: the rule costs the other line the block's length wherever it "
              "sits, but placed anywhere earlier it also costs everything that line "
              "would have run afterwards, since a stopped line does not restart. So "
              "the other line works a full day and loses only the last hours. Only as "
              "late as the other line needs, though: when that line runs out of work "
              "earlier, the sate starts the moment it stops (the handover, planner "
              "02/10). The window counts the changeover in front of the block, which "
              "is when the peanut arrives at the line. Orders it costs are reported "
              "as SATE_DROP, not CAPACITY_DROP: more minutes will not fix them, only a "
              "day without sate on the other line. "
              "Set FRIGI_SATE_EXCLUSIVE=0 to fall back to plain level-6 handling.",
    ),
    Constraint(
        id="A6", name="Sate reserves the other line's day in the assignment model",
        category="allergenen",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="A capacity term in the CP-SAT model; it exists only as minutes inside the solve.",
        data_source="—",
        notes="A5's cost, in the units the assignment model works in: on a day "
              "carrying sate, the other line's ORDINARY minutes are its capacity "
              "minus that sate. A line's own sate runs beside the other block "
              "(FRIGI_SATE_PARALLEL), so it only has to fit the line's own capacity; "
              "in the old queue it is ordinary booked minutes and two blocks charge "
              "each other. Also here, as a set constraint: sate and a non-sate "
              "product that must close the day never share a line-day, and two sate "
              "articles that must close are one closing claim. Sate an earlier phase "
              "already placed counts as a "
              "constant (_reserved_sate_minutes) — sate products are nearly all "
              "single-line, so they are hard orders, so they are placed in phase 1 "
              "and are not variables any more by the time the day gets filled "
              "around them. Without this the model books both lines full, the "
              "builder stops one at the cut-off, and everything behind it returns "
              "as a drop for the loop to re-place.",
    ),
    Constraint(
        id="A4", name="Halal isolation", category="allergenen",
        severity=Severity.HARD, point=Point.NONE,
        data_source="Product.halal (parsed, never read)",
        notes="NOT ENFORCED. Halal-first-of-day, line separation and the no-pork-on "
              "-the-other-line rule are all absent from the running system.",
    ),

    # ── Production days ─────────────────────────────────────────────────────
    Constraint(
        id="D1", name="Allowed production weekdays per product", category="productiedagen",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.constraints.slots.legal_slots",
        backstop="src.validator.validate_schedule",
        data_source="Planning info.xlsx (day columns) -> day_rules.allowed_days_for",
        notes="Chicken Tue/Wed/Fri, fish not on Monday, etc. Also refused at BUILD "
              "(constraints.day_rules.check_dag_toegelaten) so a day can never be "
              "rendered with an illegal order on it, whatever put it there.",
    ),
    Constraint(
        id="D2", name="MEP lead time (D-1 / D-2)", category="productiedagen",
        severity=Severity.SOFT, point=Point.BUILD,
        impl="src.constraints.day_rules.get_mep_date",
        backstop="src.validator.validate_schedule",
        data_source="Planning info.xlsx (MEP columns)",
        notes="Emits the MEP date per order and warns when it lands on a weekend. "
              "Advisory — it does not move production.",
    ),
    Constraint(
        id="D3", name="Parameterized MEP lead days (>D-1)", category="productiedagen",
        severity=Severity.SOFT, point=Point.NONE,
        data_source="Product.mep_lead_days (parsed, never read)",
        notes="NOT ENFORCED. Only the fixed D-1 / D-2 split above exists.",
    ),
    Constraint(
        id="D4", name="Special day restrictions string", category="productiedagen",
        severity=Severity.HARD, point=Point.NONE,
        data_source="Product.restricted_days (parsed, never read)",
        notes="NOT ENFORCED. Superseded in practice by D1's day columns.",
    ),

    # ── Equipment ───────────────────────────────────────────────────────────
    Constraint(
        id="M1", name="Shared single-unit equipment", category="toestellen",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.device_rules.stagger_around",
        backstop="src.constraints.device_rules.check_toestel_conflict",
        data_source="data/capacity.json (toestellen_beschikbaarheid)",
        notes="One line is scheduled first; the other is staggered around its use of "
              "any machine with fewer than 2 units, so a collision is impossible by "
              "construction rather than detected afterwards. Machines with 2+ units "
              "are never staggered — that would waste real capacity. WHICH line goes "
              "first is not fixed: it decides which line absorbs the whole cost of "
              "sharing, so on a day where both want the same machine the day is built "
              "both ways round and the one that carries more orders wins (a tie keeps "
              "L1 first). See scheduler._build_line_pair. Gernal and Leonard have 2 "
              "units since the 02/10 meeting, so they are never staggered.",
    ),
    Constraint(
        id="M2", name="Device conflicts become solver cuts", category="toestellen",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="A no-good cut added to the CP-SAT model between iterations; it has no meaning outside the solve loop.",
        data_source="—",
        notes="Evidence comes from two places. A pair caught OVERLAPPING in time on "
              "one machine is forbidden from sharing any day for the rest of the run "
              "(day-scoped under FRIGI_DEVICE_CUT_DAY_SCOPED) — but that almost never "
              "fires, because M1's staggering is precisely what removes the overlap. "
              "The real source is a pair where the day builder LOST an order to the "
              "other line's hold on the machine (DaySchedule.device_dropped, proven by "
              "counterfactual: the order fits the same line with the block removed). "
              "Those are always scoped to the day observed — it is a statement about "
              "that day's timing, not about the pair. See docs/toestel-cut-gap.md.",
    ),

    # ── Staffing ────────────────────────────────────────────────────────────
    Constraint(
        id="P1", name="Headcount ramp between consecutive orders", category="personeel",
        severity=Severity.SOFT, point=Point.BUILD,
        impl="src.constraints.staffing_rules.check_bezetting_continuity",
        data_source="data/capacity.json (staffing.max_bezetting_delta)",
        notes="Only INCREASES are capped — you cannot conjure staff mid-shift. A drop "
              "of any size is fine. A big jump is allowed anyway if the incoming "
              "order runs longer than 4h, since staff can arrive gradually within it. "
              "The day sequencer prunes the arc entirely rather than paying for it.",
    ),
    Constraint(
        id="P2", name="FTE limits per shift", category="personeel",
        severity=Severity.HARD, point=Point.NONE,
        data_source="removed from capacity.json (was staffing.fte_per_shift)",
        notes="NOT ENFORCED. The config block was dead and has been deleted.",
    ),
    Constraint(
        id="P3", name="Skill level requirements", category="personeel",
        severity=Severity.SOFT, point=Point.NONE,
        data_source="Product.skill_required (computed by loader, never read)",
        notes="NOT ENFORCED.",
    ),

    # ── Speed ───────────────────────────────────────────────────────────────
    Constraint(
        id="V1", name="Production duration from line speed", category="snelheid",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.changeover.calculate_duration",
        data_source="Planning info.xlsx (speed per line)",
        notes="aantal / snelheid, rounded up to 5 minutes.",
    ),
    Constraint(
        id="V2", name="Speed caps (e.g. 800g on L2)", category="snelheid",
        severity=Severity.HARD, point=Point.NONE,
        data_source="Product.speed_cap / weight_grams (parsed, never read)",
        notes="NOT ENFORCED. Duration always uses the raw sheet speed.",
    ),

    # ── Sequencing / grouping ───────────────────────────────────────────────
    Constraint(
        id="S1", name="Identical products, then kapvorm, then ascending allergen level", category="sequentie",
        severity=Severity.SOFT, point=Point.BUILD,
        impl="src.constraints.sequence_rules.sort_orders_for_lijn",
        data_source="—",
        notes="The fixed greedy order: identical-product blocks (G1, smallest first), "
              "grouped by kapvorm (G2; no kapvorm falls through), then ascending "
              "allergen level. Also decides which orders die first when a day "
              "overflows: smaller ones, not whatever happened to be last in the list.",
    ),
    Constraint(
        id="S2", name="Must / cannot be first or last of day", category="sequentie",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.constraints.sequence_rules.apply_positie_regels",
        backstop="src.validator.validate_schedule",
        data_source="Planning info.xlsx cols 28-31 -> Product.opstart_moet / "
                    "opstart_kan_niet / afsluit_moet / afsluit_kan_niet",
        notes="Prohibitions AND requirements are hard (planner 15/10: 'set in stone'). "
              "A 'moet' product must sit in the run of one article that opens (closes) "
              "the day, so two orders of one article may share the end, two articles "
              "may not. Enforced in the assignment model (at most one claiming article "
              "per line-day and end, FRIGI_POSITIE_MOET_HARD), the greedy sort, the "
              "sequencer (missing arcs) and scheduler._enforce_positie_regels, which "
              "gives up an order rather than the rule; the validator errors on any "
              "survivor. Can reject an order, which is the trade the planner chose.",
    ),
    Constraint(
        id="S3", name="A line-day's set can supply both its ends", category="sequentie",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="A constraint over the assignment model's own presence variables; it exists only inside that solve.",
        backstop="src.scheduler._enforce_positie_regels",
        data_source="Planning info.xlsx cols 28-31 (same flags as S2)",
        notes="S2 read as a property of the SET on a (day, line), so the solver cannot "
              "propose a day the builder would have to break up. A non-empty line-day "
              "needs an order allowed to open it and one allowed to close it, and — "
              "since those are two different orders — a line-day carrying two or more "
              "orders needs TWO that can take an end. Both are necessary conditions, so "
              "neither can exclude a feasible day. Orders an earlier phase already "
              "placed count toward this (they reach the model as a minutes budget, with "
              "their end capability alongside it); without that the rule used to be "
              "switched off for every day an earlier phase had touched.",
    ),
    Constraint(
        id="G1", name="Identical products produced together, smallest first", category="groepering",
        severity=Severity.SOFT, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="A CP-SAT objective term; the group is a weight inside the solver model.",
        data_source="Planning info.xlsx col 'identiek product' -> grouping_rules.identiek_groepen",
        notes="Planner, 02/10. Column 15 is a link list; the groups are its connected "
              "components (Product.identiek_groep). The assignment model pays "
              "FRIGI_IDENTIEK_SPLIT_PENALTY per (day, line) a group is spread over, "
              "the day sort keeps a group contiguous smallest first, the sequencer "
              "pays a bonus per back-to-back arc. Soft everywhere: a group that cannot "
              "share a day is split, never rejected.",
    ),
    Constraint(
        id="G2", name="Same kapvorm together, within a day and over days", category="groepering",
        severity=Severity.SOFT, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="A CP-SAT objective term; the kapvorm is a weight inside the solver model.",
        data_source="Planning info.xlsx (kapvorm) -> grouping_rules.kapvorm_sleutel",
        notes="Planner, 02/10 and 15/10, second layer under G1. FRIGI_KAPVORM_SPREAD_PENALTY "
              "per active (day, line, kapvorm), FRIGI_KAPVORM_CAMPAIGN_PENALTY per "
              "campaign start (active where the line's previous workday was not), "
              "kapvorm groups in the day sort, a sequencer bonus per back-to-back arc "
              "and for opening on the kapvorm the line closed on the day before. 'NVT' "
              "(nearly all of L2) has no kapvorm, so there the layer falls through. "
              "On L1, the automatic packing line, it outranks every other preference "
              "(planner 22/10): FRIGI_L1_KAPVORM_CHANGE_PENALTY (60, above the line "
              "preference) per extra kapvorm on an L1 line-day and per L1 campaign "
              "start across a seam; FRIGI_SEQ_L1_KAPVORM_BONUS (2000, above identical "
              "pairs and any idle or changeover) per arc in the sequencer; and the "
              "regroup pass (campaign_regroup) minimises L1 kapvorm changes over the "
              "committed window before anything else, NVT counted as a kapvorm of its "
              "own. Hard rules and the order count still come first.",
    ),
    Constraint(
        id="G3", name="Same schaal together, within a day and over days", category="groepering",
        severity=Severity.SOFT, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="A CP-SAT objective term; the schaal is a weight inside the solver model.",
        data_source="Planning info.xlsx (schaal) -> grouping_rules.schaal_sleutel",
        notes="Planner, 15/10, next to G2 and priced just below it: "
              "FRIGI_SCHAAL_SPREAD_PENALTY and FRIGI_SCHAAL_CAMPAIGN_PENALTY in the "
              "assignment model, schaal sub-groups inside each kapvorm group in the day "
              "sort, FRIGI_SEQ_SCHAAL_BONUS in the sequencer. The exact tray, not the "
              "changeover family O2 uses. The layer that counts on L2.",
    ),

    # ── Plan integrity ──────────────────────────────────────────────────────
    Constraint(
        id="X1", name="No partial delivery of a split order", category="integriteit",
        severity=Severity.HARD, point=Point.FILTER,
        impl="src.scheduler_ortools_v3.assign_production_dates_feedback",
        outside_because="An all-or-nothing constraint over the chunk variables the solver itself creates.",
        backstop="src.plan_state.build_new_state",
        data_source="—",
        notes="An order counts as scheduled only if every chunk survives the oracle, "
              "and chunks are committed all-or-nothing.",
    ),
    Constraint(
        id="X2", name="Every order is scheduled or rejected, exactly once", category="integriteit",
        severity=Severity.HARD, point=Point.VERIFY,
        impl="src.totals.compute",
        backstop="src.totals.report_imbalance",
        outside_because="Plan bookkeeping, not a production rule - it counts orders, it does not constrain the floor.",
        data_source="—",
        notes="The accounting invariant. A run that loses an order says so loudly "
              "instead of shipping a plan whose numbers do not add up.",
    ),
    Constraint(
        id="X3", name="Frozen days replay byte-for-byte", category="integriteit",
        severity=Severity.HARD, point=Point.BUILD,
        impl="src.scheduler.replay_locked_days",
        outside_because="Plan mechanics: a frozen day is copied from its persisted render rather than re-derived, so no rule is applied at all.",
        data_source="data/generated/plan_state.json (render)",
        notes="A committed day is copied from its persisted time slots, never "
              "re-derived — re-running the greedy walk on a shrunk order set is not "
              "guaranteed to reproduce the same result.",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Self-check and rendering
# ─────────────────────────────────────────────────────────────────────────────

def _resolve(dotted: str):
    """Import 'a.b.c' and return c, or raise."""
    module_path, _, attr = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def verify_registry() -> list[str]:
    """
    Every claim in this file, checked. Returns a list of problems; empty is good.
    `tests/test_registry.py` asserts it stays empty.
    """
    problems: list[str] = []
    seen: set[str] = set()

    for c in REGISTRY:
        if c.id in seen:
            problems.append(f"{c.id}: duplicate id")
        seen.add(c.id)

        if c.point is Point.NONE:
            if c.impl:
                problems.append(f"{c.id}: point=NONE but claims impl {c.impl!r}")
            continue

        if not c.impl:
            problems.append(f"{c.id}: point={c.point.value} but no impl given")
            continue

        for dotted, label in ((c.impl, "impl"), (c.backstop, "backstop")):
            if not dotted:
                continue
            try:
                _resolve(dotted)
            except (ImportError, AttributeError, ValueError) as e:
                problems.append(f"{c.id}: {label} {dotted!r} does not resolve ({e})")

        if not c.inside_package and not c.outside_because:
            problems.append(
                f"{c.id}: impl {c.impl!r} lives outside src/constraints/ without an "
                f"`outside_because`. Either move it into the package, or say why it "
                f"belongs where it is."
            )

        if c.point is Point.VERIFY and c.severity is Severity.HARD and not c.backstop:
            problems.append(
                f"{c.id}: HARD rule enforced only at VERIFY — VERIFY reports, it "
                f"does not prevent. It needs a FILTER or BUILD owner."
            )

    return problems


def by_point() -> dict[Point, list[Constraint]]:
    out: dict[Point, list[Constraint]] = {p: [] for p in Point}
    for c in REGISTRY:
        out[c.point].append(c)
    return out


def render_markdown() -> str:
    """Generate docs/constraints.md. The doc is output, never a source."""
    counts = {p: len(v) for p, v in by_point().items()}
    total = len(REGISTRY)
    enforced = total - counts[Point.NONE]

    categories: dict[str, list[Constraint]] = {}
    for c in REGISTRY:
        categories.setdefault(c.category, []).append(c)

    lines = [
        "# Production scheduling constraints",
        "",
        "<!-- GENERATED FILE — do not edit.",
        "     Source: src/constraints/registry.py",
        "     Regenerate: py -m src.constraints.registry -->",
        "",
        f"**{total} constraints — {enforced} enforced, {counts[Point.NONE]} not.**",
        "",
        "Each rule is enforced at exactly one point:",
        "",
        "| Point | Meaning | Count |",
        "|---|---|---|",
        f"| `FILTER` | Removes candidate (day, line) slots before the solver — cannot be violated by construction | {counts[Point.FILTER]} |",
        f"| `BUILD` | Shapes the day timeline and may refuse an order | {counts[Point.BUILD]} |",
        f"| `VERIFY` | Asserts over a built day; reports only | {counts[Point.VERIFY]} |",
        f"| `NONE` | **Not enforced anywhere** | {counts[Point.NONE]} |",
        "",
    ]

    not_enforced = [c for c in REGISTRY if c.point is Point.NONE]
    if not_enforced:
        lines += [
            "## Not enforced",
            "",
            "These have a field on `Product` and a column in the spreadsheet, which",
            "makes them look implemented. They are not. Nothing reads them.",
            "",
            "| ID | Rule | Where the data goes |",
            "|---|---|---|",
        ]
        for c in not_enforced:
            lines.append(f"| `{c.id}` | {c.name} | {c.data_source} |")
        lines.append("")

    for category, items in categories.items():
        lines += [f"## {category.capitalize()}", ""]
        for c in items:
            flag = "" if c.implemented else "  **NOT ENFORCED**"
            lines += [
                f"### `{c.id}` — {c.name}{flag}",
                "",
                f"- **Severity:** {c.severity.value}",
                f"- **Enforced at:** `{c.point.value}`"
                + (f" — `{c.impl}`" if c.impl else ""),
            ]
            if c.backstop:
                lines.append(f"- **Backstop:** `{c.backstop}`")
            if c.data_source:
                lines.append(f"- **Data source:** {c.data_source}")
            lines += ["", c.notes, ""]

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    from pathlib import Path

    problems = verify_registry()
    for p in problems:
        print(f"[REGISTRY] {p}")
    if problems:
        raise SystemExit(1)

    out = Path(__file__).resolve().parents[2] / "docs" / "constraints.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(), encoding="utf-8")
    print(f"wrote {out} ({len(REGISTRY)} constraints)")


if __name__ == "__main__":
    main()
