"""
date_optimizer.py — Feedback-loop CP-SAT scheduling (real sequence-dependent setups)

This is the "Option C" scheduler. It closes the two-stage gap between the
optimizer and the daily scheduler by using the REAL daily scheduler as an oracle.

Why a feedback loop instead of a single monolithic model?
  - The setup time between two orders is sequence-dependent
    (scale change + kapvorm change + allergen cleaning).
  - But the real daily scheduler (`schedule_day`) does NOT search for an optimal
    sequence — it uses a fixed deterministic sort and re-assigns lines itself.
  - So the amount of time a day actually consumes is a deterministic function of
    the SET of orders placed on that day. Encoding that messy function inside
    CP-SAT is intractable at 445 orders; calling the real scheduler is exact.

Algorithm (logic-based Benders / cut generation):
  1. Solve a light assignment model: which day does each order go to?
     (capacity proxy = sum of durations + per-order buffer <= day capacity)
  2. For each day, run the REAL `schedule_day`. It computes real setups and
     drops any orders that don't fit in the shift window.
  3. For every day that dropped ≥1 order, add a "no-good" cut:
        sum(presence[o, day] for o in that_day's_set) <= |set| - 1
     forbidding that exact over-full combination.
  4. Re-solve. Dropped orders migrate to other days.
  5. Repeat until no day drops any order (convergence) or max_iters reached.

At convergence: the optimizer's assignment == the daily plan, with zero phantom
rejections and utilization maximized under the real setup costs.

Order splitting (chunking):
  A large order may be produced as up to K "chunks", each on its own (day,
  line), instead of forced onto one day. K is sized per order from its own
  duration (dur // MIN_CHUNK, capped) — small orders that could never need
  more than one chunk get K=1 and cost nothing extra in the model. Each
  active chunk carries its own MIN_CHUNK-minute floor and its own
  _PROXY_BUFFER_MIN setup buffer in the capacity constraint (splitting a real
  order really does cost an extra real changeover), plus a smaller objective
  penalty per extra chunk so the solver only splits when it has to. An order
  counts as scheduled only if ALL of its chunks the solver placed actually
  survive the oracle that iteration — partial delivery of an order is not a
  valid outcome, so if any one chunk gets shed, the whole order rolls back.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from dataclasses import replace

from ortools.sat.python import cp_model

from frigilunch.adapters import config
from frigilunch.domain.models import Order, Product, RejectionDiagnostic, SolveOutcome
from frigilunch.application.plan_score import calculate_plan_score
from frigilunch.domain.constraints import (
    SATE_EXCLUSIVE as _SATE_EXCLUSIVE,
    SATE_PARALLEL as _SATE_PARALLEL,
    WEEKDAG_MAP, WERKDAGEN, calculate_duration, is_sate, mag_eerst, mag_laatst,
    kapvorm_sleutel, moet_eerst, moet_laatst, preferred_line, schaal_formaat,
    schaal_sleutel,
)
from frigilunch.domain.constraints import slots as slots_mod
from frigilunch.domain.constraints.device import scarce_devices_needed
from frigilunch.application.scheduler import schedule_day
from frigilunch.application.multi_day_scheduler import schedule_all_days
from frigilunch.domain.state import order_identity


# Per-order buffer used as the capacity proxy in the assignment model. Higher =
# fewer per-iteration time-drops (faster convergence) at the cost of slightly
# more conservative day-packing. The feedback loop still corrects any residual.
# Overridable via env FRIGI_PROXY_BUFFER for experiments.
import os as _os
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed — fall back to whatever's already in the environment

# Default 30 since 2026-10-09 (was 60). The real changeover in a committed plan
# averages 19.5 min per order (26 per actual changeover), so 60 made the model
# believe days were full hours before they were — the planner's "plan to the
# end of the shift" complaint (02/10). .env already set 30 (measured on wk32,
# see there); this makes the code default agree. Week 45, --dry-run --reset
# --warmup 0, 2 runs per arm, buffer the only difference ("real" rejections
# exclude the 10 orders with no product row or no line speed):
#
#            scheduled   real rejections   end-of-shift gap   idle   L1 / L2 util
#     60     319 / 318       16 / 17         7240 / 7595 min   275/405  66 / 30 %
#     40     328 / 320        7 / 15         5790 / 5855 min   165/485  74 / 35 %
#     30     331 / 325        4 / 10         4990 / 5080 min    70/35   73 / 36 %
#
# 30 wins on every column; 60 and 30 do not overlap on any of them. The
# remaining end-of-shift gap is mostly week 45 having little demand.
_PROXY_BUFFER_MIN = int(_os.environ.get("FRIGI_PROXY_BUFFER", "30"))

# Learned per-day capacity cut: converges the loop's drops but REGRESSES throughput
# (a single per-day scalar is biased low and can't separate L1 from L2). Off by
# default; enable with FRIGI_LEARNED_CAP=1 for experiments. See git history / analysis.
_USE_LEARNED_CAP = _os.environ.get("FRIGI_LEARNED_CAP", "0") == "1"

# ── Order splitting ──
# Reserved for orders that CANNOT physically fit in a single day even in the
# best case (see max_single_day_cap below) — not "big relative to a day".
# MAX_CHUNKS caps the per-order blow-up; observed real splits are 2-way, so
# keep this modest rather than paying for headroom nothing uses.
_MIN_CHUNK_MIN = int(_os.environ.get("FRIGI_MIN_CHUNK_MIN", "60"))
_MAX_CHUNKS = int(_os.environ.get("FRIGI_MAX_CHUNKS", "5"))
# Escape hatch to force whole-order-only behaviour for A/B comparison.
_DISABLE_SPLIT = _os.environ.get("FRIGI_DISABLE_SPLIT", "0") == "1"
# Objective penalty per chunk beyond the first — discourages gratuitous
# splitting (the capacity constraint already charges an extra buffer per
# chunk) while staying well below the 10,000 unscheduled penalty so the
# solver always prefers splitting an order over dropping it.
_SPLIT_PENALTY = int(_os.environ.get("FRIGI_SPLIT_PENALTY", "0"))

# Per-day objective penalties. days_late is a hard-ish deadline signal (should
# stay >> early) — never relaxed by the grace window below, only days_early is.
#
# days_early used to be penalized flat from day 0 (capacity.json's own
# description calls this out as intentional: "vroege productie wordt bestraft
# met 10 per dag te vroeg"), which meant the solver avoided idle EARLY-day
# capacity even when doing so would've relieved pressure on congested days
# near a cluster of deadlines. Measured effect of that flat penalty on the
# April dataset: fewer orders fit (372 vs 377) and the loop never fully
# converged. Now: free for the first FRIGI_EARLY_GRACE_DAYS days early (no
# reason to avoid using that room), a small ramp after that up to the
# max_days_before_delivery window — kept far below the late/unscheduled
# weights so a tight deadline still always wins; this is about USING slack
# capacity, not about deprioritizing which orders get scheduled at all.
_LATE_PENALTY = int(_os.environ.get("FRIGI_LATE_PENALTY", "100"))
_EARLY_PENALTY = int(_os.environ.get("FRIGI_EARLY_PENALTY", "1"))
_EARLY_GRACE_DAYS = int(_os.environ.get(
    "FRIGI_EARLY_GRACE_DAYS",
    str(config.production_timing()["proximity_grace_days"]),
))

# Size-weights the flat 10,000 unscheduled penalty by order.aantal, so a large
# order is worth more to schedule than a small one instead of the solver
# treating "one order dropped" as equally bad regardless of how much volume
# that order represents. Every order still carries the base 10,000 (so small
# orders are never fully starved).
#
# The weight has to be read PER MINUTE OF LINE TIME, not per order — that flat
# 10,000 is what actually decides things, and a tiny order buys it with almost
# no capacity. Measured drop-cost per minute over the real order book:
#
#     size band      <1k    1-5k   5-10k  10-20k   >20k
#     W=1 (old)      318     192     101      70      79
#     W=10           487     501     456     439     591
#
# At W=1 a sub-1,000-unit order was worth 4x more per minute than a 26,810-unit
# one, so the solver systematically dropped big orders to keep small ones —
# the opposite of the intended policy. W=10 puts the largest band on top.
#
# The old comment here kept this "well below the per-day-late weight (100) so a
# tight deadline still always wins over raw size". That rail does not bind:
# capacity.json sets min_days_before_delivery=0, so slots.legal_slots caps
# candidate days at lever_datum and days_late is 0 for every slot the model can
# even see. _LATE_PENALTY never fires, and raising this weight cannot push an
# order past its delivery date.
#
# NOTE this changes WHICH orders are dropped, not HOW MANY minutes are. A/B on
# wk32: W=1 -> 418 and 423 scheduled on two identical runs, W=10 -> 417. The
# feedback loop's own run-to-run spread is larger than the effect. Big-first is
# a commercial policy here, not a throughput fix.
_UNSCHEDULED_UNIT_WEIGHT = int(_os.environ.get("FRIGI_UNSCHEDULED_UNIT_WEIGHT", "10"))

# Consolidation penalties: a flexible order has no inherent preference
# between L1/L2, so without these the solver's line choice on a light day is
# an arbitrary tie-break — it'll happily put one order on each line and
# leave both half-empty rather than filling one and leaving the other
# untouched. _LINE_ACTIVATION_PENALTY charges per activated (day, line)
# slot; _DAY_ACTIVATION_PENALTY charges per day with any activity at all
# (on top of that day's line charges), for the weaker "prefer fewer days
# too" preference. Line consolidation is the priority, so its weight is
# higher. Both are tiny next to the late (100/day) and unscheduled (10,000+)
# weights — this only ever wins when the real objective is indifferent.
_LINE_ACTIVATION_PENALTY = int(_os.environ.get("FRIGI_LINE_ACTIVATION_PENALTY", "5"))
_DAY_ACTIVATION_PENALTY = int(_os.environ.get("FRIGI_DAY_ACTIVATION_PENALTY", "2"))

# Per (day, line, schaal-formaat-family) activation penalty — pushes the
# solver to concentrate same-formaat orders onto the same (day, line)
# instead of spreading formaats arbitrarily, which is what actually lets
# the per-day sequencer (day_sequencer.py) find more same-formaat (20 min)
# transitions instead of different-formaat (30 min) ones. Weighted lighter
# than line/day activation — this is a refinement on top of consolidation,
# not a replacement for it.
_FORMAAT_SPREAD_PENALTY = int(_os.environ.get("FRIGI_FORMAAT_SPREAD_PENALTY", "1"))

# G1 / G2 — the planner's two grouping layers (meeting 02/10): identical
# products (column "identiek product", see constraints/grouping_rules.py) on
# the same day and line, then same kapvorm together. Both are charged per
# active (day, line, group) exactly like _FORMAAT_SPREAD_PENALTY above, so a
# group spread over three line-days pays three times, over one line-day once.
#
# The order, all × efficiency_weight:
#   off-preferred line 50 > identical split 40 > line activation 5
#   > kapvorm spread 3 > day activation 2 > formaat spread 1.
#
# Identical sits just BELOW the line preference on purpose: identical products
# nearly always share their line rules, and when they do not, the 04/09
# meeting's "prio L1 only on L2 when L1 is full" is the older instruction.
# Kapvorm sits between line and day activation: it should beat a tidier
# calendar, not cost an extra line-day. Both are far below the 10,000 an
# unscheduled order costs — grouping can split a group, never reject an order.
_IDENTIEK_SPLIT_PENALTY = int(_os.environ.get("FRIGI_IDENTIEK_SPLIT_PENALTY", "40"))
_KAPVORM_SPREAD_PENALTY = int(_os.environ.get("FRIGI_KAPVORM_SPREAD_PENALTY", "8"))

# G2 / G3 over days (planner 15/10): "kapvorm and schaal after each other as
# much as possible, within a day and also over days — even if that means
# producing an order weeks before it is due". The SPREAD terms only count
# (day, line, group) slots: H22 on Monday and Wednesday costs the same whether
# Tuesday runs H22 or H44. A CAMPAIGN term closes that gap — one start per
# (day, line, group) that is active while the same line's previous workday did
# not carry it. Five consecutive H22 days are one campaign, H22-H44-H22 two.
#
# Weights, all x efficiency_weight, so each still outranks the whole
# delivery-proximity cost: pulling an order forward to join a campaign is
# always worth it, as asked. The order is now
#   off-preferred line 50 > identical split 40 > kapvorm spread 8
#   = kapvorm campaign 8 > schaal spread 6 = schaal campaign 6
#   > line activation 5 > day activation 2 > formaat spread 1.
# Far below the 10,000 an unscheduled order costs: grouping never rejects.
_KAPVORM_CAMPAIGN_PENALTY = int(_os.environ.get("FRIGI_KAPVORM_CAMPAIGN_PENALTY", "8"))
_SCHAAL_SPREAD_PENALTY = int(_os.environ.get("FRIGI_SCHAAL_SPREAD_PENALTY", "6"))
_SCHAAL_CAMPAIGN_PENALTY = int(_os.environ.get("FRIGI_SCHAAL_CAMPAIGN_PENALTY", "6"))
# G2 on L1, the automatic packing line (planner 22/10): "producties met
# dezelfde kapvorm zo veel mogelijk aansluitend — deze regel krijgt voorrang
# op alle andere instellingen". So on L1 every kapvorm CHANGE carries a premium
# at the TOP of the efficiency tier, over the line preference (50) too:
#   L1 kapvorm change 60 > off-preferred line 50 > identical split 40
#   > kapvorm 8 > schaal 6 > activation 5, 2 > formaat 1.
# Priced on changes, not on use, on top of the ordinary terms above:
#   * a second, third ... kapvorm on one L1 line-day (sum of kapvorm flags
#     minus the line-day flag) — one change inside the day each;
#   * a kapvorm campaign starting on L1 while L1 also ran the previous
#     workday — a change across the seam.
# Pricing the active flags themselves at 60 would make running L1 at all
# dearer than the line preference: an order that prefers L1 went to L2 in
# test_lijn_voorkeur with that version. Still far below the 10,000 an
# unscheduled order costs: hard rules and the order count stay above it. The
# soft stage rarely reaches its bound (see campaign_regroup), so the regroup
# pass carries the same priority.
_L1_KAPVORM_CHANGE_PENALTY = int(_os.environ.get("FRIGI_L1_KAPVORM_CHANGE_PENALTY", "60"))

# Real transition cost: price the actual changeover savings from keeping
# same-format and same-kapvorm orders on the same (day, line).
#
# `calculate_setup_time` (constraints/changeover.py):
#   same schaal-formaat family → 20 min base
#   different schaal-formaat   → 30 min base  (+10 min per cross-format transition)
#   different kapvorm           → +15 min on top of the base
#
# Both terms approximate (n_distinct - 1) per (day, line) — a lower bound on
# the actual cross-format / cross-kapvorm transition count. They sit between
# kapvorm spread/campaign (8) and identiek-split (40):
#   L1 kapvorm change 60 > off-preferred line 50 > identical split 40
#   > kapvorm transition 15 > format transition 10 > kapvorm spread 8
#   > schaal 6 > line/day activation 5, 2 > formaat spread 1.
# L1 also carries _L1_KAPVORM_CHANGE_PENALTY on top of kapvorm transition.
_FORMAT_TRANSITION_PENALTY = int(_os.environ.get("FRIGI_FORMAT_TRANSITION_PENALTY", "50"))
_KAPVORM_TRANSITION_PENALTY = int(_os.environ.get("FRIGI_KAPVORM_TRANSITION_PENALTY", "50"))

# O7 — fill the committed days from the front (planner 16/10: lines stop hours
# before shift end on 13/10, 15/10, 20/10 while later days are booked full).
# Nothing in the model preferred an earlier day over a later one: inside the
# grace window early production is free, so where a day's work lands among
# legal, equally-grouped options was a tie-break, and an empty end of shift
# cost nothing. Total idle over the window is fixed by the order book, so a
# flat idle penalty would be a constant; what the planner asks for is WHERE
# the idle sits — at the back of the window, where a late order can still use
# it, not at 12:05 on a Tuesday.
#
# So: a reward per booked block of _FRONTLOAD_BLOCK_MIN minutes, larger the
# earlier the committed day it lands on. Lexicographically BELOW everything
# else, by construction (see frontload_upper_bound in the objective):
#   * below the grouping / consolidation tier, so it never buys a split group,
#     an extra line-day or a kapvorm / schaal change — it only breaks ties;
#   * below the early-production ramp, and only paid on slots inside the grace
#     window, so it never moves an order further ahead of its delivery than
#     the grace window already allowed for free.
# 0 switches it off and leaves every other coefficient exactly as before.
_FRONTLOAD_WEIGHT = int(_os.environ.get("FRIGI_FRONTLOAD_WEIGHT", "0"))
_FRONTLOAD_BLOCK_MIN = 30

# Time cap per identical-group neighbourhood in _regroup_identical.
_REGROUP_SECONDS = float(_os.environ.get("FRIGI_REGROUP_SECONDS", "1.0"))

# Per chunk-slot placed on a line the product does not prefer (line_rules.
# preferred_line). The product database states a preference per product and
# nothing here used to read it: among the legal (day, line) slots the line fell
# out of the consolidation weights below, which is an arbitrary tie-break. Two
# wk36 runs without this term put 55 of 90 and 48 of 94 line-flexible
# placements on the non-preferred line, and only ~30 of those were on a day
# where the preferred line was even 90% busy — the rest simply went wherever.
# The rule being missed is the 04/09 meeting's "producten prio L1 enkel op L2
# inplannen indien geen plaats op L1".
#
# A penalty, not a filter, is what makes that "indien geen plaats" work: the
# preference has to LOSE to capacity. The weight is bounded on both sides.
#   * above the consolidation cluster (5 + 2 + 1, max realistic swing ~8), so a
#     preference beats a tidier day rather than the other way round;
#   * above the early-production ramp (1/day past the grace window), so a
#     wrong-line day cannot buy itself in with a few days of slack;
#   * two orders of magnitude BELOW the 10,000 + 10/unit an unscheduled order
#     costs, so a full preferred line always spills onto the other line rather
#     than dropping work. Honouring a preference must never cost an order.
_OFF_PRIO_LINE_PENALTY = int(_os.environ.get("FRIGI_OFF_PRIO_LINE_PENALTY", "50"))

# Per S2 "moet opstarten"/"moet afsluiten" requirement that lands on a line-day
# already carrying one. A day has exactly one first and one last order, so the
# second such requirement on it cannot be met. Two orders of magnitude below
# the 10,000 an unscheduled order costs — this moves an order to another day,
# it never buys a position by rejecting one.
#
# Only used with FRIGI_POSITIE_MOET_HARD=0. Since 15/10 the planner's word is
# that the product database's first/last rules are "set in stone", so by
# default two different articles with the same requirement may never share a
# line-day at all (two orders of ONE article may: they open the day as a run).
# That CAN reject an order, which is the trade the planner chose.
_POSITIE_MOET_PENALTY = int(_os.environ.get("FRIGI_POSITIE_MOET_PENALTY", "500"))
_POSITIE_MOET_HARD = _os.environ.get("FRIGI_POSITIE_MOET_HARD", "1") == "1"

# Two ends, two different orders: a line-day carrying two or more orders needs
# two of them able to take an end, not just "someone may open and someone may
# close" (one order satisfies that and still cannot run both first and last).
# Necessary, so it can never exclude a feasible day. ON by default; set to 0
# for the old behaviour, where the day builder discovered it and dropped an
# order for it.
#
# Measured together with _PHASE_CONTEXT, FRIGI_S2_SEQUENCER_REPAIR,
# FRIGI_SYMMETRIC_DEVICE_PRIORITY and FRIGI_POSITIE_MOET_PENALTY — they touch
# the same failures and were switched as one arm. Week 36, full warm-up chain,
# --reset, 2 runs per arm, whole-horizon totals:
#
#                  scheduled   real rejections   COMBINED-DROP   POSITIE-REGEL
#     all off       413-423        16, 21            6, 6            1, 4
#     all on        420-427        10, 10            0, 0            0, 1
#
# NEVER-PLACED is 2-3 in both arms: that bucket is real oversubscription and
# nothing here was ever going to move it. Scheduled UNITS are 4.209-4.253M off
# vs 4.226-4.229M on — inside the run-to-run spread, so the gain is in orders
# placed, not volume. Ombouw is 3-5% higher (7,830-8,040 -> 8,080-8,420): more
# orders sharing a day costs more changeover, which is the trade. Runtime drops
# from ~8:00 to ~5:40, because the reconciliation rounds it makes unnecessary
# were the expensive part.
_S2_DISTINCT_ENDS = _os.environ.get("FRIGI_S2_DISTINCT_ENDS", "1") == "1"

# Let a later phase see the day an earlier phase built, not just the minutes it
# left behind: its orders count toward S2's ends in the model, and the oracle
# sequences against them instead of against an empty shift. ON by default; set
# to 0 for the old split where each phase validated its own orders alone and
# everything that only appears when the two sets run together was left for
# reconciliation to reject.
#
# This is the term that carries the arm above. Reconciliation ROUNDS over the
# whole warm-up chain go 30, 30 -> 0, 0: with the phases validating against
# each other's real days, the final reconciliation finds nothing left to fix.
# That is the whole COMBINED-SCHEDULE-DROP bucket, and it is not that those
# orders now fail elsewhere — they are placed. A failure caught inside the
# phase that caused it can still be answered by moving the order; the same
# failure caught at reconciliation can only be answered by rejecting it.
_PHASE_CONTEXT = _os.environ.get("FRIGI_PHASE_CONTEXT", "1") == "1"

# Constrain single-unit machines as a shared per-day resource in the CP-SAT
# model itself, instead of discovering the collision afterwards through
# no-good cuts.
#
# OFF by default: measured and it does not pay. A/B on wk36 (full warm-up
# chain, --reset, 2 runs per arm):
#
#                 scheduled   rejected      units (avg)
#     cuts only    413-424      62-73        4,237,947
#     device cap   412-422      61-68        4,256,474
#
# Ranges overlap completely; +0.4% units is inside the run-to-run spread.
#
# WHY it doesn't help, which is the useful part: this bounds device VOLUME per
# day, and volume is not what binds. Gernal — the tightest machine — averages
# 62% of its window with only 7 of 20 committed days above 80%, so the
# constraint rarely fires. What actually blocks an order is device TIMING: L1
# holding the Gernal 16:10-19:50 while L2 has a 5-hour gap. That is a
# sequencing problem the day builder already solves near-optimally (measured
# at 93% of the theoretical minimum), not a volume problem the assignment
# solver can fix.
#
# It is kept because it is physically true and it shifts failures earlier and
# more honestly (oracle_dropped 1-5 -> 0-2: fewer orders die at day-build
# time), which is worth having if the timing side is ever modelled properly.
_DEVICE_CAPACITY = _os.environ.get("FRIGI_DEVICE_CAPACITY", "1") == "1"

# Large-neighborhood-search-style incremental re-solve: once a day has zero
# drops, zero new cuts, and zero device conflicts in an iteration, lock it in
# (remove it from every order's candidate slots for the rest of the run)
# instead of re-deciding it from scratch every iteration. ON by default; set
# FRIGI_INCREMENTAL_RESOLVE=0 to fall back to the full-rebuild baseline.
_INCREMENTAL_RESOLVE = _os.environ.get("FRIGI_INCREMENTAL_RESOLVE", "0") == "1"

# Most-constrained-first: solve line-restricted orders (alleen_l1/alleen_l2 —
# no alternative line, ever) in a full pass against FULL capacity first, THEN
# solve flexible (either-line) orders against whatever capacity is left.
# Flexible orders can always fall back to the other line; restricted orders
# can't, so giving them first claim on their only line's capacity avoids the
# case where a flexible order happens to claim room a restricted order had
# no other way to reach. ON by default; set FRIGI_TWO_PHASE_HARD_FIRST=0 for
# the single-pass baseline.
_TWO_PHASE_HARD_FIRST = _os.environ.get("FRIGI_TWO_PHASE_HARD_FIRST", "0") == "1"

# After phases 1+2, whatever's still NEVER-PLACED (genuinely contested, not
# structurally impossible — see rejection_reasons) gets one more attempt as
# a third pass: force split-eligible, against whatever real capacity phases
# 1+2 left behind. Splitting a medium order across two days' worth of thin,
# scattered leftover can succeed where finding one whole contiguous day
# can't — but only applied to this small already-failed-twice batch, not
# the whole order book, so it can't reintroduce the old blanket-split cost.
# Only takes effect together with FRIGI_TWO_PHASE_HARD_FIRST.
_RETRY_SPLIT_NEVER_PLACED = _os.environ.get("FRIGI_RETRY_SPLIT_NEVER_PLACED", "1") == "1"

# Iteration ceiling. Meant as a safety cap (the real binding limit is supposed
# to be feedback_timeout_seconds) but in practice this is often what actually
# stops the loop first — e.g. 15 iters * ~11s/iter ~= 165s, well under the
# usual 240s global budget. Overridable for search-budget experiments.
# A device clash observed on one day is evidence about that day's TIMING (the
# single Gernal held 16:10-19:50 on L1 while L2 had a gap), not proof the two
# orders can never coexist — sequenced differently on another day they often
# can. The default cut nevertheless bans the pair from every common day for
# the rest of the run and is never retracted, which is far stronger than the
# evidence for it. Set FRIGI_DEVICE_CUT_DAY_SCOPED=1 to ban the pair only on
# the day the clash was actually seen.
_DEVICE_CUT_DAY_SCOPED = _os.environ.get("FRIGI_DEVICE_CUT_DAY_SCOPED", "1") == "1"

# Spend more search effort on the COMMIT window (the weeks this run freezes)
# than on the open tail, which next week's run re-decides from scratch anyway.
# Two effects:
#
#   * The phase-to-phase capacity handoff is measured with the changeover-
#     minimizing sequencer instead of the greedy walk. `_reserved_capacity` is
#     built from each day's werk_min, so every minute of ombouw the greedy walk
#     leaves on the table is capacity phase 2/3 is told it does not have. This
#     is where forcing the sequencer actually pays: NOT in the oracle, where it
#     cannot change the verdict (it only reduces ombouw on days where nothing
#     dropped, and fit_units is identical either way) — only where the result
#     is converted into a minutes budget for a later phase.
#
#   * A clean commit-window day is never locked by _INCREMENTAL_RESOLVE, so
#     later iterations can still use the room left on it. Clean is not full.
_COMMIT_EFFORT = _os.environ.get("FRIGI_COMMIT_EFFORT", "0") == "1"

# Orders due INSIDE the commit window first (planner 15/10). Once every order
# may be produced on any open day up to its delivery (one joint solve, no
# tiered split), an order due in week 50 competes with one due this week for
# the same capacity — and the plain "fewest rejected orders" count would
# happily reject the one due now to fit two due later. In a rolling cycle that
# is backwards: the later orders get another chance next week, the one due
# inside the window is frozen out for good. So one rejected order due inside
# the window outweighs every later order together, in the model's
# completeness stage and when the feedback loop keeps its best iteration.
# Producing ahead can then only use room the due orders do not need.
_URGENT_FIRST = _os.environ.get("FRIGI_URGENT_FIRST", "1") == "1"

_MAX_ITERS = int(_os.environ.get("FRIGI_MAX_ITERS", "15"))
# Stop early once this many consecutive iterations fail to beat the best
# fitted count. Overridable for search-budget experiments.
_NO_IMPROVE_STOP = int(_os.environ.get("FRIGI_NO_IMPROVE_STOP", "6"))

# Defaults for the open tail beyond the committed window (create_plan.py calls
# assign_production_dates_feedback a second time with these via `_max_iters`
# and `_feedback_timeout_seconds`, below): every week gets re-decided from
# scratch next run anyway, so it does not need the committed window's full
# search budget — a quick pass beats spending minutes on days nothing will
# freeze yet.
_FAR_MAX_ITERS = int(_os.environ.get("FRIGI_FAR_MAX_ITERS", "3"))
_FAR_TIMEOUT_SECONDS = float(_os.environ.get("FRIGI_FAR_TIMEOUT_SECONDS", "60"))


_load_solver_config = config.solver_config
# Bound at import: inside assign_production_dates_feedback the name `config` is
# rebound to the solver-settings dict, so the module is unreachable from there.
_window_minutes = config.window_minutes
_CAPACITY = config.planning_caps()
_day_type = config.day_type


def _orders_overlap(po1, po2) -> bool:
    return po1.start < po2.eind and po2.start < po1.eind


def _positie_reason(order: Order, product: Product | None) -> str:
    """
    S2 (position rule) rejection, as its own cause.

    This used to surface as ORACLE-DROPPED — a capacity story — with the
    scarce-device note appended, which is actively misleading: such an order
    typically fits with the line most of the day idle, and the machines named
    in that note were never contended for. What actually happened is that the
    day builder could not arrange the day so both its ends were legal, and
    dropped the order to keep the rule (see scheduler._enforce_positie_regels).

    Deliberately does NOT claim the order is impossible. Checked on a real
    case (63811000, deadline 2026-11-06): other days in its window had a
    perfectly legal arrangement — first this order, then a day-mate allowed
    to close — that the day builder finds the moment both are handed to it
    together. What is true is narrower and is what this says: on the day it
    was ASSIGNED, no legal arrangement existed, and more search budget alone
    will not fix that, because the assignment model has no way to express
    "this order needs a companion" and so never looks for one.
    """
    if product is None:
        end = "open or close"
        naam = order.art_nr
    else:
        naam = product.naam
        may_open, may_close = mag_eerst(product), mag_laatst(product)
        end = (
            "open" if (not may_open and may_close)
            else "close" if (may_open and not may_close)
            else "open or close"
        )
    return (
        f"POSITIE-REGEL: this order fit its assigned day on capacity, but the day builder "
        f"removed it to keep that day's ends legal: {naam} may not {end} the day, so it "
        f"needs another order on the same line-day to take that end, and the day it was "
        f"assigned to retained no usable companion after exact time, device and saté "
        f"validation. Other unfrozen days before {order.lever_datum} may still support "
        f"a legal pair; this assignment must be repaired rather than treated as a plain "
        f"capacity rejection."
    )


def _sate_reason(order: Order, product: Product | None, lijn: int) -> str:
    """
    A5 rejection, as its own cause.

    Reported as a capacity drop it reads as "the day was full", and the answer
    to that is more line time — which will never place this order. The day had
    the minutes. What it did not have was a hall free of peanut: the other line
    was running sate, and while it does, this line produces nothing.
    """
    naam = product.naam if product is not None else order.art_nr
    other = 2 if lijn == 1 else 1
    return (
        f"SATE: {naam} fit its assigned day on capacity, but L{other} was running sate "
        f"(peanut) and the whole hall stops for that — L{lijn} may not produce anything "
        f"else while it runs. The sate is planned against closing time so the rest of the "
        f"day stays usable, and this order was what still ran into it. More line time does "
        f"not fix it; a day inside its window (deadline {order.lever_datum}) without sate "
        f"on the other line does."
    )


def _run_oracle_day(unit_list, unit_order_obj, products, setup_matrix, toestellen, dd,
                    context=None):
    """
    Run the REAL daily scheduler for one day and report:
      - fit_units: set of unit ids that actually got planned (fit in time)
      - conflict_pairs: set of (unit_a, unit_b) that clash on a scarce device
                        while overlapping in time (cross-line L1<->L2)
      - device_pairs: set of (unit_a, unit_b) where a is an order the day
                      builder LOST to b's line holding a single-unit machine.
                      The overlap detector above can essentially never fire —
                      M1's staggering is what removes the overlap — so this is
                      where real evidence about machines comes from. Always
                      scoped to this day by the caller: it is a statement about
                      today's timing, not about the pair in general.
      - s2_units: the unit ids among the drops that were removed to keep the
                  day's ends legal (S2), not for want of time. Same cut either
                  way — the day still can't carry that set — but the REASON
                  differs, and an S2 drop is structural: it is not waiting on
                  capacity and cannot be split or re-searched into fitting.

    `unit_list` is a list of unit ids scheduled on this day; `unit_order_obj[u]`
    is the already-built Order copy for that unit (chunk-sized aantal, correct
    productie_datum/lijn).

    `context` is what an EARLIER phase already put on this day. Without it a
    later phase validates a fiction: it is told the day has, say, 300 minutes
    left, and then sequences its own orders into an otherwise EMPTY shift —
    no changeover from the orders already there, no allergen ordering against
    them, no shared machine held by them, and no idea which end of the day
    they take. Everything that only shows up when the two sets are produced
    together was left for `reconcile_combined_schedule` to discover at the very
    end, where the only remaining answer is to reject. Context orders are
    scheduled alongside but never counted as fitted — they are not this
    phase's to place. If the day builder drops one of THEM, the day is
    overloaded by what this phase added, and none of its units may stay.

    This is the ground-truth oracle: it uses the same schedule_day code path as
    production, so nothing here can disagree with the final exported plan.
    """
    copies = []
    id_to_unit: dict[int, int] = {}
    for u in unit_list:
        c = unit_order_obj[u]
        copies.append(c)
        id_to_unit[id(c)] = u

    context_ids = set()
    for o in context or ():
        c = replace(o)
        copies.append(c)
        context_ids.add(id(c))

    ds = schedule_day(copies, products, setup_matrix, dd, toestellen)
    planned_ids = {id(po.order) for po in (ds.lijn1 + ds.lijn2)}
    if context_ids - planned_ids:
        # This phase pushed an order an earlier phase had already placed off
        # the day. That order is not ours to give up, so the verdict is that
        # none of our units fit here.
        return set(), set(), set(), set(), set()
    fit_units = {id_to_unit[i] for i in planned_ids if i in id_to_unit}
    s2_units = {
        id_to_unit[id(o)] for o in ds.positie_dropped if id(o) in id_to_unit
    }
    sate_units = {
        id_to_unit[id(o)] for o, _line in ds.sate_dropped if id(o) in id_to_unit
    }

    # Detect cross-line device conflicts between planned L1 and L2 orders
    tb = toestellen or {}
    conflict_pairs: set[tuple[int, int]] = set()
    for po1 in ds.lijn1:
        for po2 in ds.lijn2:
            if not _orders_overlap(po1, po2):
                continue
            for toestel, nodig1 in po1.product.toestellen.items():
                if not nodig1 or not po2.product.toestellen.get(toestel, False):
                    continue
                if tb.get(toestel, 1) < 2:
                    a = id_to_unit.get(id(po1.order))
                    b = id_to_unit.get(id(po2.order))
                    if a is not None and b is not None:
                        conflict_pairs.add((min(a, b), max(a, b)))
                    break

    # ── M1: who lost an order to whom over a single-unit machine ──
    # `device_dropped` carries the counterfactual the day builder established
    # while it still could: this order fits the day, and fails only alongside
    # the other line's claim. Pair it with each order on that other line
    # actually holding one of the named machines.
    device_pairs: set[tuple[int, int]] = set()
    for order, lijn, devs in ds.device_dropped:
        a = id_to_unit.get(id(order))
        if a is None:
            continue
        others = ds.lijn2 if lijn == 1 else ds.lijn1
        for po in others:
            if not any(po.product.toestellen.get(dev, False) for dev in devs):
                continue
            b = id_to_unit.get(id(po.order))
            # b is None for an earlier phase's order: real, but not this
            # model's to move, so there is no pair to forbid.
            if b is not None and b != a:
                device_pairs.add((min(a, b), max(a, b)))

    return fit_units, conflict_pairs, s2_units, device_pairs, sate_units


def _dedup_vars(variables: list) -> list:
    """
    Same list without repeats. A product allowed to take EITHER end appears in
    both the may-open and the may-close list, and summing the two would count
    its one presence variable twice — enough to satisfy "two orders can take an
    end" with a single order.
    """
    seen: dict[int, object] = {}
    for v in variables:
        seen.setdefault(id(v), v)
    return list(seen.values())


def _reserved_orders_from(schedules: list) -> dict[tuple[date, int], list[Order]]:
    """
    What an earlier phase's built days actually hold, per (day, line) — the
    orders themselves, taken from the built schedules rather than from the
    solver's output, so it is what the floor would really be running.
    """
    out: dict[tuple[date, int], list[Order]] = {}
    for s in schedules:
        for lijn, planned in ((1, s.lijn1), (2, s.lijn2)):
            if planned:
                out[(s.datum, lijn)] = [po.order for po in planned]
    return out


def _reserved_ends_from(
    schedules: list,
) -> dict[tuple[date, int], tuple[bool, bool, int, int]]:
    """
    What an earlier phase's built days already contribute to S2, per
    (day, line): (holds an order allowed to open, one allowed to close, how
    many orders, how many of them can take either end).

    The later phase sees those orders only as a minutes budget. Without this it
    has to either ignore S2 on every day the earlier phase touched — which is
    what it used to do, switching the rule off precisely where it matters — or
    demand ends that are in fact already there and refuse legal days.
    """
    out: dict[tuple[date, int], tuple[bool, bool, int, int]] = {}
    for s in schedules:
        for lijn, planned in ((1, s.lijn1), (2, s.lijn2)):
            if not planned:
                continue
            out[(s.datum, lijn)] = (
                any(mag_eerst(po.product) for po in planned),
                any(mag_laatst(po.product) for po in planned),
                len(planned),
                sum(1 for po in planned if mag_eerst(po.product) or mag_laatst(po.product)),
            )
    return out


def _cap_l1(d: date) -> int:
    return config.planning_cap(d, 1)


def _cap_l2(d: date) -> int:
    return config.planning_cap(d, 2)


def _reserved_sate_minutes(
    reserved_orders: dict[tuple[date, int], list[Order]] | None,
    products: dict[str, Product],
) -> dict[tuple[date, int], int]:
    """
    A5 — how many minutes of sate an EARLIER phase already put on each (day,
    line), as a constant this phase has to plan the other line around.

    Sate products are nearly all restricted to one line, which makes them hard
    orders, which puts them in phase 1 — so by the time the phases that fill
    the day around them run, the sate is not a variable any more. Without this
    they are just minutes in `_reserved_capacity`, indistinguishable from any
    other order, and the other line gets booked straight through them.
    """
    out: dict[tuple[date, int], int] = {}
    for (dd, lijn), orders in (reserved_orders or {}).items():
        minutes = sum(
            calculate_duration(o, products[o.art_nr]) + _PROXY_BUFFER_MIN
            for o in orders
            if o.art_nr in products and is_sate(products[o.art_nr])
        )
        if minutes:
            out[(dd, lijn)] = minutes
    return out


def _device_note(
    order: Order,
    products: dict[str, Product],
    toestellen: dict[str, int] | None,
    hits: int,
) -> str:
    """
    The machine half of a rejection reason, appended to the capacity reasons.

    Without this a device-starved order is reported as NEVER-PLACED and reads
    as ordinary oversubscription — "add capacity" — when the actual blocker is
    one shared machine that no amount of extra line time would free up.
    """
    product = products.get(order.art_nr)
    devs = sorted(scarce_devices_needed(product, toestellen)) if product else []
    if not devs:
        return ""
    note = (
        f" TOESTELLEN: dit product heeft {', '.join(devs)} nodig — daarvan is er "
        f"1 exemplaar, dus L1 en L2 kunnen het nooit tegelijk gebruiken."
    )
    if hits:
        note += (
            f" Tijdens het zoeken is deze order {hits}x geweigerd op precies zo'n "
            f"toestelconflict; meer lijntijd lost dat niet op."
        )
    return note


def _regroup_identical(
    model: cp_model.CpModel,
    solver: cp_model.CpSolver,
    presence: dict[tuple[int, int, int], cp_model.IntVar],
    groups: dict[str, list[int]],
) -> cp_model.CpSolver:
    """
    G1 — a small neighbourhood search the main solve does not manage itself:
    for each identical group still spread over several (day, line) slots,
    re-solve with EVERY other chunk held where it is and only that group's
    members free. The model is the full one — every constraint, cut and
    objective term — so whatever comes back is legal by construction, and it is
    kept only when the objective improves.

    Why it is needed: measured on week 45 (2026-10-09), the soft-objective
    stage ended ~3x above its own bound with identical pairs split across
    half-empty L2 days, and 2.5x more time made the objective worse, not
    better. Moving two orders at once is one step here and a long detour for
    the general search. A regrouped HINT was tried first and made the stage
    return nothing at all (the hint broke constraints it could not see).

    Each neighbourhood is a handful of free booleans, so a solve takes well
    under a second; capped by _REGROUP_SECONDS per group.

    What it does NOT fix, measured on the same week by forcing pairs together
    and deleting constraints until the forced model became feasible: in the
    committed window the pairs that stayed apart were blocked by real
    constraints, not by search — a line-day already full in the model's own
    minutes (duration + proxy buffer), or a twin the iteration had left
    unplaced, which the completeness lock (`rejection_expr == ...`) rightly
    refuses to place here. It improved the open-tail objective (31.4M ->
    26.6M in one run) and changed nothing in that week's committed window.
    """
    if not groups:
        return solver
    current = {key: solver.Value(var) for key, var in presence.items()}
    best = solver.ObjectiveValue()
    for members in groups.values():
        free = set(members)
        slots = {
            (di, lijn) for (u, di, lijn), val in current.items() if val and u in free
        }
        if len(slots) < 2:
            continue
        model.ClearAssumptions()
        model.AddAssumptions([
            var if current[key] else var.Not()
            for key, var in presence.items() if key[0] not in free
        ])
        model.ClearHints()
        for key, var in presence.items():
            model.AddHint(var, current[key])
        lns = cp_model.CpSolver()
        lns.parameters.max_time_in_seconds = _REGROUP_SECONDS
        lns.parameters.num_search_workers = 8
        status = lns.Solve(model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and lns.ObjectiveValue() < best:
            best = lns.ObjectiveValue()
            current = {key: lns.Value(var) for key, var in presence.items()}
            solver = lns
    model.ClearAssumptions()
    return solver


def assign_production_dates_feedback(
    orders: list[Order],
    products: dict[str, Product],
    start_date: date,
    weeks: int = 10,
    setup_matrix: dict | None = None,
    toestellen: dict[str, int] | None = None,
    _reserved_capacity: dict[tuple[date, int], int] | None = None,
    _reserved_ends: dict[tuple[date, int], tuple[bool, bool, int, int]] | None = None,
    _reserved_orders: dict[tuple[date, int], list[Order]] | None = None,
    _force_split_eligible: bool = False,
    frozen_until: date | None = None,
    commit_end: date | None = None,
    _mandatory_orders: bool = True,
    _max_iters: int | None = None,
    _feedback_timeout_seconds: float | None = None,
) -> SolveOutcome:
    """
    Feedback-loop scheduler (Option C). Returns (scheduled, rejected, stats).

    `setup_matrix` is required (the oracle needs it to compute real setups).

    `_reserved_capacity` is internal — minutes already claimed per (day,
    lijn) by an earlier phase, subtracted from that day/line's capacity here.
    `_reserved_ends` carries what those same orders already do for S2 (see
    `_reserved_ends_from`), so the position rule stays on for the days an
    earlier phase touched instead of being switched off there.
    `_reserved_orders` carries the orders themselves, so the oracle builds
    each day with them on it instead of sequencing this phase's orders into
    an empty shift that does not exist.
    `_force_split_eligible` is internal — makes every order in THIS call
    split-eligible regardless of size, for the small never-placed retry
    batch (see FRIGI_RETRY_SPLIT_NEVER_PLACED below). Safe in bulk callers
    never use it on the full order set — only ever on the tiny subset that
    already failed twice.
    `_max_iters` / `_feedback_timeout_seconds` override the module-level
    search budget (`_MAX_ITERS`, `feedback_timeout_seconds`) for this call
    only — used by create_plan.py to give the open tail beyond the committed
    window a cheaper pass than the committed window itself gets.
    """
    if setup_matrix is None:
        raise ValueError("setup_matrix is required for the feedback scheduler (v3)")

    if _TWO_PHASE_HARD_FIRST and _reserved_capacity is None:
        return _assign_two_phase_hard_first(
            orders, products, start_date, weeks, setup_matrix, toestellen, frozen_until,
            commit_end,
        )

    config = _load_solver_config()
    # V3 needs a larger budget than the single-pass solver to converge conflicts.
    global_timeout = (
        _feedback_timeout_seconds if _feedback_timeout_seconds is not None
        else config.get("feedback_timeout_seconds", config.get("timeout_seconds", 300))
    )
    iter_timeout = config.get("feedback_iter_timeout_seconds", 10)
    max_iters = _max_iters if _max_iters is not None else _MAX_ITERS

    end_date = start_date + timedelta(weeks=weeks)

    # A5 — sate an earlier phase already placed is a fixed block on that line
    # and a hole in the other line's day. See _reserved_sate_minutes.
    reserved_sate = (
        _reserved_sate_minutes(_reserved_orders, products) if _SATE_EXCLUSIVE else {}
    )
    # A5 + S2 — line-days an earlier phase already gave a product that MUST
    # close and is not sate. Sate closes its line (planner 22/10), so no sate
    # may join those; see the exclusion below.
    reserved_plain_closer = {
        key for key, orders in (_reserved_orders or {}).items()
        if _SATE_EXCLUSIVE and any(
            o.art_nr in products
            and moet_laatst(products[o.art_nr]) and not moet_eerst(products[o.art_nr])
            and not is_sate(products[o.art_nr])
            for o in orders
        )
    }

    # All workdays in the planning window. end_date = start_date + N weeks
    # lands exactly on the NEXT window's Monday (start_date is always a
    # Monday), so it's an exclusive boundary — d < end_date, not <=, or a
    # spurious 1-day 11th week sneaks into both the schedulable window and
    # every report built from it.
    workdays = slots_mod.workdays_between(start_date, end_date)

    # Filter valid orders (with known products)
    valid_orders: list[Order] = []
    invalid_orders: list[tuple[Order, str]] = []
    for order in orders:
        if order.art_nr not in products:
            invalid_orders.append((order, f"Product {order.art_nr} unknown"))
        elif products[order.art_nr].constraint_provenance.get("speed") == "inferred":
            invalid_orders.append((
                order,
                f"MISSING-SPEED: Product {order.art_nr} has no speed on either production line",
            ))
        else:
            valid_orders.append(order)

    # Which (day, line) each order may legally occupy. Every "may not run
    # there" rule — production window, workdays, allowed weekdays, line
    # permission and exclusion, the frozen zone — lives in
    # constraints/slots.legal_slots, so the solver picks only from legal
    # options and cannot violate them by construction. This used to be
    # forty lines inline here, which is why "which days is this order allowed
    # on" was not something you could read in one place.
    allowed_slots: dict[int, list[tuple[int, int]]] = {}
    allowed_days: dict[int, list[int]] = {}
    order_dur: dict[int, int] = {}
    order_formaat: dict[int, str | None] = {}
    # G1 / G2 keys, None when the order has no partner in this solve (a group
    # of one has nothing to be spread from, so it gets no variables).
    order_ident: dict[int, str | None] = {}
    order_kap: dict[int, str | None] = {}
    order_schaal: dict[int, str | None] = {}
    order_devices: dict[int, list[str]] = {}   # single-unit machines this order occupies
    order_sate: dict[int, bool] = {}           # A5: stops the OTHER line while it runs
    # The line this product prefers when that line has room, or None when it
    # states no preference (line_rules.preferred_line). Soft — priced in the
    # objective, never filtered on, so a full preferred line still spills.
    order_voorkeur_lijn: dict[int, int | None] = {}
    # S2, as a property of the SET of orders on a line-day (see the constraint
    # below). Static per product, so it is known here rather than discovered
    # by the oracle one failed day at a time.
    order_may_open: dict[int, bool] = {}
    order_may_close: dict[int, bool] = {}
    order_moet_open: dict[int, bool] = {}
    order_moet_close: dict[int, bool] = {}
    max_single_day_cap: dict[int, int] = {}   # best case: this order's largest possible single day
    no_slot_cause: dict[int, str] = {}        # o_idx -> why it has zero legal slots
    for o_idx, order in enumerate(valid_orders):
        product = products[order.art_nr]
        order_dur[o_idx] = calculate_duration(order, product)
        order_formaat[o_idx] = schaal_formaat(product.schaal)
        order_ident[o_idx] = product.identiek_groep
        order_kap[o_idx] = kapvorm_sleutel(product)
        order_schaal[o_idx] = schaal_sleutel(product)
        order_devices[o_idx] = scarce_devices_needed(product, toestellen)
        order_sate[o_idx] = is_sate(product)
        order_voorkeur_lijn[o_idx] = preferred_line(product)
        order_may_open[o_idx] = mag_eerst(product)
        order_may_close[o_idx] = mag_laatst(product)
        order_moet_open[o_idx] = moet_eerst(product)
        # A product that must do both takes the head (apply_positie_regels),
        # so it does not also claim the tail.
        order_moet_close[o_idx] = moet_laatst(product) and not moet_eerst(product)

        slot_set = slots_mod.legal_slots(
            order, product, workdays, start_date, frozen_until=frozen_until
        )
        allowed_days[o_idx] = slot_set.days
        allowed_slots[o_idx] = slot_set.slots
        max_single_day_cap[o_idx] = max(_CAPACITY["norm"][lijn] for lijn in slot_set.lines)
        if slot_set.cause:
            no_slot_cause[o_idx] = slot_set.cause

    for keys in (order_ident, order_kap, order_schaal):
        counts: dict[str, int] = defaultdict(int)
        for key in keys.values():
            if key is not None:
                counts[key] += 1
        for o_idx, key in keys.items():
            if key is not None and counts[key] < 2:
                keys[o_idx] = None

    # Chunk budget per order: split is now reserved for orders that CANNOT
    # physically fit in a single day even in the best case (duration exceeds
    # the largest day this order could ever land on) — not just "kind of
    # big relative to a day". The old rule (>=35% of the SMALLEST day's
    # capacity) flagged 412 of 1,251 orders as split-eligible; checked
    # against real duration data, only ~4 orders actually can't fit any
    # single day at all. Every other "big" order competes for room exactly
    # like every other order — that contention is what the day/line
    # assignment + oracle feedback loop already exists to resolve, and it
    # doesn't need chunk machinery to do it. This keeps the multi-chunk
    # CP-SAT overhead (extra variables, symmetry-breaking) confined to the
    # handful of orders that are genuinely, physically impossible to place
    # whole, instead of taxing a third of the order book for almost no
    # orders that actually end up using it.
    order_chunks: dict[int, int] = {}
    for o_idx in range(len(valid_orders)):
        dur = order_dur[o_idx]
        mandatory_split = dur > max_single_day_cap[o_idx]
        # A 2-chunk split needs the order to actually support two
        # >=MIN_CHUNK_MIN pieces — force-eligible is a fallback attempt, not
        # a guarantee, and a too-small order forced to K=2 would just make
        # its own chunk-sum-equals-duration constraint infeasible (silently
        # unplaceable), which defeats the point of retrying it at all.
        can_split = dur >= 2 * _MIN_CHUNK_MIN
        if _DISABLE_SPLIT or not can_split or not (mandatory_split or _force_split_eligible):
            order_chunks[o_idx] = 1
        else:
            order_chunks[o_idx] = max(
                2, min(_MAX_CHUNKS, dur // _MIN_CHUNK_MIN, len(allowed_days[o_idx]))
            )

    # Flatten to "units" = (order, chunk index). Units are the atoms the model
    # places; an order with K=1 has exactly one unit and behaves exactly like
    # the old one-slot-per-order model.
    units: list[tuple[int, int]] = []
    order_units: dict[int, list[int]] = defaultdict(list)
    for o_idx in range(len(valid_orders)):
        for k in range(order_chunks[o_idx]):
            u = len(units)
            units.append((o_idx, k))
            order_units[o_idx].append(u)

    # No-good cuts for time overflow, now per (day, line), at UNIT granularity:
    #   (day_idx, lijn, frozenset(unit_id), max_allowed)
    cuts: list[tuple[int, int, frozenset[int], int]] = []
    # Learned capacity per (day, line): (day_idx, lijn) -> max production-minutes
    # (excl. buffer) empirically observed to fit once real setups are applied.
    learned_cap: dict[tuple[int, int], int] = {}
    # Device-conflict pairs (unit ids): once observed, forbid them sharing a day.
    # Entries are (day_idx | None, frozenset(pair)); day_idx is None under the
    # default global scope (the pair is banned from every common day) and the
    # observing day under _DEVICE_CUT_DAY_SCOPED (banned only from that day).
    global_conflict_pairs: set[tuple[int | None, frozenset]] = set()
    # How often each ORDER was one half of such a pair. A scarce machine losing
    # an order its room is invisible in the rejection reason otherwise: the
    # order reads as NEVER-PLACED, indistinguishable from plain oversubscription,
    # even though what actually blocked it was the single Gernal or Leonard
    # already being in use on the other line. Counted per order (not per unit)
    # so a split order's chunks don't inflate it.
    device_conflict_hits: dict[int, int] = defaultdict(int)

    # Memoize oracle results by (day, exact per-line-and-size assignment).
    _oracle_cache: dict[tuple, tuple] = {}

    # Incremental-resolve state (FRIGI_INCREMENTAL_RESOLVE only). Once a day
    # is verified clean, it's excluded from every order's candidate slots for
    # the rest of the run — day-level (not per-line), so a stable day can
    # never later develop a cross-line device conflict with a newly-added
    # unit on its other line. Only single-chunk orders are ever stabilized:
    # a multi-chunk order's sibling chunks can land on other days, and
    # partially locking one chunk while its sibling is still being searched
    # is not safely representable in this scheme.
    stable_units: dict[int, tuple[int, int, int]] = {}   # unit -> (day_idx, lijn, amt_val)
    stable_orders: set[int] = set()
    stable_days: set[int] = set()
    # Best oracle-validated placement from the preceding iteration. Rebuilt
    # models receive it as a hint so a new cut repairs the bad part of the day
    # instead of rediscovering hundreds of already-good assignments.
    incumbent_slots: dict[int, tuple[int, int]] = {}

    def build_and_solve(time_left: float):
        """Build the assignment model with current cuts and solve it.

        Orders with K=1 (now the vast majority — only orders that can't
        physically fit a single day get K>1, see max_single_day_cap)
        use the plain boolean-presence-with-fixed-weight formulation, exactly
        like the pre-splitting model: no extra `amt` variable, no equality
        constraint. That machinery is real overhead for CP-SAT's search even
        when mathematically redundant, so it's reserved for orders that
        actually have >1 chunk to place.
        """
        model = cp_model.CpModel()

        # presence[(u, day_idx, lijn)] — is chunk u produced on this day/line?
        # amt[(u, day_idx, lijn)]      — minutes of it, if so (multi-chunk only).
        presence: dict[tuple[int, int, int], cp_model.IntVar] = {}
        amt: dict[tuple[int, int, int], cp_model.IntVar] = {}
        order_used: dict[int, object] = {}   # BoolVar (K>1) or LinearExpr (K=1)
        chunk_used: dict[int, cp_model.IntVar] = {}   # multi-chunk units only

        by_line: dict[tuple[int, int], list] = defaultdict(list)
        # Plain presence BoolVars per (day, line) — used only to derive
        # line_active/day_active below, kept separate from `by_line`'s
        # capacity-weighted terms (dur+buffer or amt+buffer).
        presence_by_line: dict[tuple[int, int], list] = defaultdict(list)
        # The subsets of presence_by_line that MAY take each end of the day.
        may_open_by_line: dict[tuple[int, int], list] = defaultdict(list)
        may_close_by_line: dict[tuple[int, int], list] = defaultdict(list)
        # The subsets that MUST take an end. A day has one of each, so a second
        # such order on the same line-day means one requirement is broken
        # whatever the sequencer does — see the objective term below.
        moet_open_by_line: dict[tuple[int, int], list] = defaultdict(list)
        moet_close_by_line: dict[tuple[int, int], list] = defaultdict(list)
        # Same, but split further by schaal-formaat family — derives
        # formaat_active below (clusters same-formaat orders together to cut
        # ombouwtijd), an aggregate per-family signal, not per-order-pair
        # cost, so it stays cheap regardless of order count.
        presence_by_line_formaat: dict[tuple[int, int, object], list] = defaultdict(list)
        # G1 / G2: same idea, per identical group and per kapvorm.
        presence_by_line_ident: dict[tuple[int, int, str], list] = defaultdict(list)
        presence_by_line_kap: dict[tuple[int, int, str], list] = defaultdict(list)
        presence_by_line_schaal: dict[tuple[int, int, str], list] = defaultdict(list)
        # S2 "moet" claims per line-day, split by article: two orders of one
        # article share the claim, two articles cannot.
        moet_open_art: dict[tuple[int, int], dict[str, list]] = defaultdict(lambda: defaultdict(list))
        moet_close_art: dict[tuple[int, int], dict[str, list]] = defaultdict(lambda: defaultdict(list))
        # Production minutes per (day, single-unit machine), summed across BOTH
        # lines — see the capacity constraint below. Unlike by_line these terms
        # carry no proxy buffer: a machine is occupied while the order runs,
        # not while the line is being changed over.
        by_device: dict[tuple[int, str], list] = defaultdict(list)
        # A5: the sate half of by_line, which is what costs the OTHER line its
        # minutes. A line's own sate is already in by_line and needs no special
        # treatment — the hall stopping is what the other line pays for.
        sate_by_line: dict[tuple[int, int], list] = defaultdict(list)
        # A5 + S2: presence of sate, and of a non-sate product that MUST close,
        # per line-day — the two cannot share one (sate closes its line).
        sate_presence: dict[tuple[int, int], list] = defaultdict(list)
        plain_closer_presence: dict[tuple[int, int], list] = defaultdict(list)

        for o_idx in range(len(valid_orders)):
            if _INCREMENTAL_RESOLVE and o_idx in stable_orders:
                continue
            us = order_units[o_idx]
            dur = order_dur[o_idx]
            formaat = order_formaat[o_idx]
            ident, kap = order_ident[o_idx], order_kap[o_idx]
            sch = order_schaal[o_idx]
            art = valid_orders[o_idx].art_nr
            devices = order_devices[o_idx] if _DEVICE_CAPACITY else ()
            sate = order_sate[o_idx] and _SATE_EXCLUSIVE
            may_open, may_close = order_may_open[o_idx], order_may_close[o_idx]
            must_open, must_close = order_moet_open[o_idx], order_moet_close[o_idx]
            # Two sate articles that must close are one closing claim: after
            # sate the line runs only more sate (sequence_rules.sluit_samen).
            close_key = "__sate__" if sate else art
            slots = (
                [s for s in allowed_slots[o_idx] if s[0] not in stable_days]
                if _INCREMENTAL_RESOLVE else allowed_slots[o_idx]
            )

            if len(us) == 1:
                # ── Single-chunk order: original simple formulation ──
                u = us[0]
                for (di, lijn) in slots:
                    p = model.NewBoolVar(f"p_{u}_{di}_{lijn}")
                    presence[(u, di, lijn)] = p
                    by_line[(di, lijn)].append(p * (dur + _PROXY_BUFFER_MIN))
                    if sate:
                        sate_by_line[(di, lijn)].append(p * (dur + _PROXY_BUFFER_MIN))
                        sate_presence[(di, lijn)].append(p)
                    elif must_close and _SATE_EXCLUSIVE:
                        plain_closer_presence[(di, lijn)].append(p)
                    presence_by_line[(di, lijn)].append(p)
                    if may_open:
                        may_open_by_line[(di, lijn)].append(p)
                    if may_close:
                        may_close_by_line[(di, lijn)].append(p)
                    if must_open:
                        moet_open_by_line[(di, lijn)].append(p)
                        moet_open_art[(di, lijn)][art].append(p)
                    if must_close:
                        moet_close_by_line[(di, lijn)].append(p)
                        moet_close_art[(di, lijn)][close_key].append(p)
                    presence_by_line_formaat[(di, lijn, formaat)].append(p)
                    if ident is not None:
                        presence_by_line_ident[(di, lijn, ident)].append(p)
                    if kap is not None:
                        presence_by_line_kap[(di, lijn, kap)].append(p)
                    if sch is not None:
                        presence_by_line_schaal[(di, lijn, sch)].append(p)
                    for dev in devices:
                        by_device[(di, dev)].append(p * dur)
                terms = [presence[(u, di, lijn)] for (di, lijn) in slots]
                if terms:
                    model.Add(sum(terms) <= 1)
                order_used[o_idx] = sum(terms) if terms else 0
                if _mandatory_orders and terms:
                    model.Add(order_used[o_idx] == 1)
                continue

            # ── Multi-chunk order: full amt/chunk_used machinery ──
            for u in us:
                for (di, lijn) in slots:
                    p = model.NewBoolVar(f"p_{u}_{di}_{lijn}")
                    presence[(u, di, lijn)] = p
                    presence_by_line_formaat[(di, lijn, formaat)].append(p)
                    if ident is not None:
                        presence_by_line_ident[(di, lijn, ident)].append(p)
                    if kap is not None:
                        presence_by_line_kap[(di, lijn, kap)].append(p)
                    if sch is not None:
                        presence_by_line_schaal[(di, lijn, sch)].append(p)
                    a = model.NewIntVar(0, dur, f"a_{u}_{di}_{lijn}")
                    amt[(u, di, lijn)] = a
                    model.Add(a <= dur * p)
                    model.Add(a >= _MIN_CHUNK_MIN * p)
                    by_line[(di, lijn)].append(a + p * _PROXY_BUFFER_MIN)
                    if sate:
                        sate_by_line[(di, lijn)].append(a + p * _PROXY_BUFFER_MIN)
                        sate_presence[(di, lijn)].append(p)
                    elif must_close and _SATE_EXCLUSIVE:
                        plain_closer_presence[(di, lijn)].append(p)
                    presence_by_line[(di, lijn)].append(p)
                    if may_open:
                        may_open_by_line[(di, lijn)].append(p)
                    if may_close:
                        may_close_by_line[(di, lijn)].append(p)
                    if must_open:
                        moet_open_by_line[(di, lijn)].append(p)
                        moet_open_art[(di, lijn)][art].append(p)
                    if must_close:
                        moet_close_by_line[(di, lijn)].append(p)
                        moet_close_art[(di, lijn)][close_key].append(p)
                    for dev in devices:
                        by_device[(di, dev)].append(a)
                terms = [presence[(u, di, lijn)] for (di, lijn) in slots]
                cu = model.NewIntVar(0, 1, f"cu_{u}")
                if terms:
                    model.Add(sum(terms) <= 1)
                    model.Add(cu == sum(terms))
                else:
                    model.Add(cu == 0)
                chunk_used[u] = cu

            # order_used = is the order produced at all (any chunk active)?
            # Chunks fill in index order (symmetry break) and must sum to the
            # full order duration iff the order is used — no partial delivery.
            ou = model.NewBoolVar(f"ou_{o_idx}")
            order_used[o_idx] = ou
            model.AddMaxEquality(ou, [chunk_used[u] for u in us])
            if _mandatory_orders and slots:
                model.Add(ou == 1)
            for i in range(1, len(us)):
                model.Add(chunk_used[us[i]] <= chunk_used[us[i - 1]])
            amt_terms = [amt[(u, di, lijn)] for u in us for (di, lijn) in slots]
            if amt_terms:
                model.Add(sum(amt_terms) == dur * ou)

        for (di, lijn), terms in by_line.items():
            dd = workdays[di]
            cap = _cap_l1(dd) if lijn == 1 else _cap_l2(dd)
            if _reserved_capacity:
                cap = max(0, cap - _reserved_capacity.get((dd, lijn), 0))
            model.Add(sum(terms) <= cap)

        # ── A5: sate on one line is time the OTHER line does not have ──
        # The day builder gives the other line everything up to the sate and
        # nothing after it (the block sits against closing time), so what that
        # line can still run is its own capacity minus the sate. Stated here as
        # minutes, the same proxy the rest of this model works in.
        #
        # A line's own sate is on the left as ordinary booked minutes, because
        # it is: what makes sate different is what it costs the OTHER side.
        # In the old queue two sate blocks on one day therefore charge each
        # other. Side by side (SATE_PARALLEL, planner 22/10) they open
        # together, so a line's ORDINARY work has to fit in front of the
        # other line's block, and its own sate only in front of closing time
        # (the plain capacity constraint above): own sate comes off the left.
        #
        # Without it the model books both lines full on a sate day, the builder
        # then stops one of them at the cut-off, and every order behind it
        # comes back as a drop the loop has to re-place — the same shape of
        # blindness `by_device` was added for. A cut can only ever forbid the
        # one combination it saw; this covers the day.
        if _SATE_EXCLUSIVE:
            sate_days = {di for (di, _l) in sate_by_line} | {
                workdays.index(dd) for (dd, _l) in reserved_sate if dd in workdays
            }
            for di in sate_days:
                dd = workdays[di]
                for lijn in (1, 2):
                    other = 2 if lijn == 1 else 1
                    sate_other = sate_by_line.get((di, other), [])
                    fixed_sate_other = reserved_sate.get((dd, other), 0)
                    if not sate_other and not fixed_sate_other:
                        continue
                    own = by_line.get((di, lijn), [])
                    if not own:
                        continue
                    cap = _cap_l1(dd) if lijn == 1 else _cap_l2(dd)
                    if _reserved_capacity:
                        cap = max(0, cap - _reserved_capacity.get((dd, lijn), 0))
                    # Clamped at zero, and not as tidiness: every term on the
                    # left is non-negative, so a negative right-hand side is
                    # not "this line-day is full", it is UNSATISFIABLE — and
                    # one such day makes the whole phase infeasible, which
                    # loses every order in it, not just that day's. Measured
                    # before the clamp: phase 2 returned INFEASIBLE and 0 of
                    # 168 flexible orders were scheduled. Zero says the same
                    # thing safely: nothing may be booked here.
                    if _SATE_PARALLEL:
                        # The line's own sate, variable or reserved, is not
                        # in front of the other block — it runs beside it.
                        own_sate = sate_by_line.get((di, lijn), [])
                        fixed_own = min(
                            reserved_sate.get((dd, lijn), 0),
                            _reserved_capacity.get((dd, lijn), 0) if _reserved_capacity else 0,
                        )
                        model.Add(
                            sum(own) - sum(own_sate) + sum(sate_other)
                            <= max(0, cap + fixed_own - fixed_sate_other)
                        )
                    else:
                        model.Add(sum(own) + sum(sate_other) <= max(0, cap - fixed_sate_other))

            # Sate closes its line — hard since the planner's 22/10 note:
            # after sate nothing else can run. A product that MUST close the
            # day can then never share a line-day with sate: one of the two
            # loses the last slot whatever the sequence. Stated on the set,
            # like S2 below, so the oracle never has to discover it.
            for key in set(sate_presence) | set(plain_closer_presence):
                di, lijn = key
                dd = workdays[di]
                sates = sate_presence.get(key, [])
                closers = plain_closer_presence.get(key, [])
                if (dd, lijn) in reserved_sate:
                    for p in closers:
                        model.Add(p == 0)
                    continue
                if (dd, lijn) in reserved_plain_closer:
                    for p in sates:
                        model.Add(p == 0)
                    continue
                if not sates or not closers:
                    continue
                any_sate = model.NewBoolVar(f"sate_on_{di}_{lijn}")
                any_closer = model.NewBoolVar(f"plain_closer_on_{di}_{lijn}")
                model.AddMaxEquality(any_sate, sates)
                model.AddMaxEquality(any_closer, closers)
                model.Add(any_sate + any_closer <= 1)

        # ── Single-unit machines are a shared resource, not a line property ──
        # Without this the model knows only "L1 has 990 minutes on Tuesday" and
        # has no idea the Gernal is ONE machine both lines draw on. It would
        # happily book 1,400 minutes of Gernal work into a 990-minute day; the
        # day builder then staggers L2 around L1 (M1), pushes an order past
        # closing time, and the loop learns one blunt "not these exact orders
        # together" cut. The cause — the machine — is never recorded, which is
        # why `toestelconflicten` reads 0 in every iteration while devices are
        # the actual bottleneck (Gernal runs at 62% of its window, 7 of 20
        # committed days above 80%).
        #
        # A cut covers one observed combination; this covers all of them.
        for (di, dev), terms in by_device.items():
            dd = workdays[di]
            units_available = max(1, toestellen.get(dev, 1) if toestellen else 1)
            # The window a machine can run in spans from the earliest line
            # start to closing time — L1's window, since L1 starts first.
            model.Add(sum(terms) <= _window_minutes(dd, 1) * units_available)

        # line_active[(di, lijn)] / day_active[di] — consolidation signal for
        # the objective below. Nothing here forces anything; it only gives
        # the solver a reason to prefer fewer activated (day, line) slots
        # when it's otherwise indifferent (a flexible order has zero
        # preference between L1/L2 today, so ties break arbitrarily and
        # produce e.g. one order on L1 + one on L2 on an otherwise-quiet day
        # instead of both on the same line, leaving the other untouched).
        line_active: dict[tuple[int, int], cp_model.IntVar] = {}
        for (di, lijn), plist in presence_by_line.items():
            la = model.NewBoolVar(f"line_active_{di}_{lijn}")
            model.AddMaxEquality(la, plist)
            line_active[(di, lijn)] = la

        # ── S2 as a SET constraint, not something the oracle discovers ──
        # A non-empty line-day needs at least one order allowed to OPEN it and
        # at least one allowed to CLOSE it. That is a property of the set of
        # orders placed there — it carries no sequencing information at all —
        # and mag_eerst/mag_laatst are static product attributes, so it can be
        # stated here directly instead of being learned one failed day at a
        # time. It is a NECESSARY condition (whatever runs last must be allowed
        # to close), so it can never exclude a genuinely feasible day; the
        # oracle still checks the rest, including whether a legal arrangement
        # also fits in the window.
        #
        # This is what the no-good cut could never express. A cut says "this
        # set is too big" (<= |S| - 1); an S2 failure means "this set is
        # MISSING something". Answering it by removing orders empties the day
        # further and makes the next iteration MORE likely to fail the same
        # way, which is why these orders never recovered no matter how long
        # the loop ran (measured: 18 of 21 such rejections had a day in their
        # own window where a legal pairing existed and fit).
        #
        # A line-day another phase has already put orders on is not exempt —
        # it is a line-day whose ends are PARTLY already supplied. Those orders
        # are a minutes budget here, not variables, so what they can do for the
        # ends is carried alongside the budget in `_reserved_ends`: whether the
        # set already holds an order allowed to open, one allowed to close, and
        # how many orders it is. Skipping the whole constraint there (which is
        # what this used to do) switched S2 off for exactly the days phases 2
        # and 3 are actually filling — every day phase 1 touched — which is
        # where the position-rule rejections were coming from. Without the
        # reserved information at all, the old skip is still the safe fallback:
        # the constraint would otherwise demand an end this phase cannot see.
        for (di, lijn), la in line_active.items():
            dd = workdays[di]
            reserved_min = _reserved_capacity.get((dd, lijn), 0) if _reserved_capacity else 0
            if reserved_min > 0 and (_reserved_ends is None or not _PHASE_CONTEXT):
                continue
            res_open, res_close, res_n, res_capable = (
                _reserved_ends.get((dd, lijn), (False, False, 0, 0))
                if (_reserved_ends and _PHASE_CONTEXT) else (False, False, 0, 0)
            )
            ends = (
                (may_open_by_line[(di, lijn)], res_open),
                (may_close_by_line[(di, lijn)], res_close),
            )
            for candidates, already_supplied in ends:
                if already_supplied:
                    continue          # the reserved set already takes that end
                if candidates:
                    model.Add(sum(candidates) >= la)
                else:
                    # Nothing eligible for that end can go here at all, so the
                    # line-day cannot be used: forcing la == 0 also forces
                    # every presence var behind it to 0 (AddMaxEquality).
                    model.Add(la == 0)

            # Two ends, two different orders. "Someone may open and someone may
            # close" is satisfied by a single order able to do both, but the day
            # that order is on also holds others, and it cannot run first and
            # last at once. So a line-day carrying two or more orders needs at
            # least TWO of them able to take an end. Also necessary, never
            # exclusionary: the real first and last orders are always distinct
            # and always end-capable.
            if not _S2_DISTINCT_ENDS:
                continue
            end_capable = _dedup_vars(
                may_open_by_line[(di, lijn)] + may_close_by_line[(di, lijn)]
            )
            here = presence_by_line[(di, lijn)]
            if not here:
                continue
            two_plus = model.NewBoolVar(f"two_plus_{di}_{lijn}")
            model.Add(sum(here) + res_n >= 2).OnlyEnforceIf(two_plus)
            model.Add(sum(here) + res_n <= 1).OnlyEnforceIf(two_plus.Not())
            model.Add(sum(end_capable) + min(2, res_capable) >= 2).OnlyEnforceIf(two_plus)

        by_day_lines: dict[int, list] = defaultdict(list)
        for (di, lijn), la in line_active.items():
            by_day_lines[di].append(la)
        day_active: dict[int, cp_model.IntVar] = {}
        for di, las in by_day_lines.items():
            da = model.NewBoolVar(f"day_active_{di}")
            model.AddMaxEquality(da, las)
            day_active[di] = da

        # formaat_active[(di, lijn, formaat)] — per-family clustering signal.
        # A (day, line) with orders from 3 distinct schaal-formaat families
        # trips 3 of these; one with a single family trips only 1. Penalizing
        # each active flag gives the solver a genuine reason (not just a
        # search hint) to concentrate same-formaat orders together, which is
        # what actually lets the same-day sequencer land more 20-min
        # same-formaat transitions instead of 30-min different-formaat ones.
        # Aggregate per-family, not per-order-pair, so it stays cheap.
        formaat_active: dict[tuple[int, int, object], cp_model.IntVar] = {}
        for (di, lijn, formaat), plist in presence_by_line_formaat.items():
            fa = model.NewBoolVar(f"formaat_active_{di}_{lijn}_{formaat}")
            model.AddMaxEquality(fa, plist)
            formaat_active[(di, lijn, formaat)] = fa

        # G1 / G2 — one flag per (day, line, group) that carries any of it.
        ident_active: list[cp_model.IntVar] = []
        for (di, lijn, ident), plist in presence_by_line_ident.items():
            ia = model.NewBoolVar(f"ident_active_{di}_{lijn}_{ident}")
            model.AddMaxEquality(ia, plist)
            ident_active.append(ia)
        kap_active: dict[tuple[int, int, str], cp_model.IntVar] = {}
        for (di, lijn, kap), plist in presence_by_line_kap.items():
            ka = model.NewBoolVar(f"kap_active_{di}_{lijn}_{kap}")
            model.AddMaxEquality(ka, plist)
            kap_active[(di, lijn, kap)] = ka
        schaal_active: dict[tuple[int, int, str], cp_model.IntVar] = {}
        for (di, lijn, sch), plist in presence_by_line_schaal.items():
            sa = model.NewBoolVar(f"schaal_active_{di}_{lijn}_{sch}")
            model.AddMaxEquality(sa, plist)
            schaal_active[(di, lijn, sch)] = sa

        # G2 / G3 campaigns: a start wherever a group is active on a line-day
        # and was not on that line's previous workday. Only the lower bound is
        # needed: the objective pushes every start var down onto it.
        def _campaign_starts(active: dict, label: str) -> list:
            starts = []
            for (di, lijn, key), act in active.items():
                prev = active.get((di - 1, lijn, key))
                st = model.NewBoolVar(f"{label}_start_{di}_{lijn}_{key}")
                model.Add((st >= act - prev) if prev is not None else (st >= act))
                starts.append((di, lijn, st))
            return starts

        kap_starts = _campaign_starts(kap_active, "kap")
        schaal_starts = _campaign_starts(schaal_active, "schaal")

        # No-good cuts: forbid known over-full combinations per (day, line)
        for (di, lijn, uset, max_allowed) in cuts:
            terms = [presence[(u, di, lijn)] for u in uset
                     if (u, di, lijn) in presence]
            if terms:
                model.Add(sum(terms) <= max_allowed)

        # Learned capacity cuts per (day, line): total production-minutes (excl.
        # buffer) on that line can't exceed what actually fit with real setups.
        for (di, lijn), tcap in learned_cap.items():
            terms = [
                amt[(u, di, lijn)]
                for u in range(len(units)) if (u, di, lijn) in amt
            ]
            if terms:
                model.Add(sum(terms) <= tcap)

        # Device-conflict cuts: a clashing pair may not share a day (summed
        # across both lines). Scope is either every common day (default) or
        # only the day the clash was actually observed on.
        for scope_di, pair in global_conflict_pairs:
            a, b = tuple(pair)
            oa, _ = units[a]
            ob, _ = units[b]
            common_days = set(allowed_days.get(oa, [])) & set(allowed_days.get(ob, []))
            if scope_di is not None:
                common_days &= {scope_di}
            for di in common_days:
                a_terms = [presence[(a, di, lj)] for lj in (1, 2) if (a, di, lj) in presence]
                b_terms = [presence[(b, di, lj)] for lj in (1, 2) if (b, di, lj) in presence]
                if a_terms and b_terms:
                    model.Add(sum(a_terms) + sum(b_terms) <= 1)

        # G1 — single-chunk units per identical group, for _regroup_identical.
        ident_groups_units: dict[str, list[int]] = defaultdict(list)
        for o_idx, key in order_ident.items():
            us = order_units[o_idx]
            if key is not None and len(us) == 1:
                ident_groups_units[key].append(us[0])


        # Objective: minimize lateness + early production + unscheduled + splits
        # (stable orders are excluded entirely — they have no variables here,
        # and their contribution to the true objective is a fixed constant
        # that doesn't affect which assignment is optimal for the free part)
        penalties = []
        rejection_terms = []
        # One efficiency improvement must dominate the entire possible
        # delivery-proximity cost of this model. Completeness is mandatory in
        # the primary solve and receives its own still-larger weight in the
        # diagnostic relaxation below.
        proximity_upper_bound = 1 + sum(
            max(0, (order.lever_datum - start_date).days - _EARLY_GRACE_DAYS)
            * _EARLY_PENALTY
            * max(1, order_chunks[o_idx])
            for o_idx, order in enumerate(valid_orders)
        )
        # O7 — front-load reward per (unit, slot), and its bound. Only for
        # committed days inside the grace window; single-chunk units only (the
        # handful of mandatory splits keep their chunks where they fall).
        n_commit = (
            sum(1 for dd in workdays if dd < commit_end)
            if (_FRONTLOAD_WEIGHT and commit_end is not None) else 0
        )
        frontload_reward: dict[tuple[int, int, int], int] = {}
        for u, (o_idx, _k) in enumerate(units):
            if n_commit == 0 or len(order_units[o_idx]) != 1:
                continue
            if _INCREMENTAL_RESOLVE and o_idx in stable_orders:
                continue
            order = valid_orders[o_idx]
            blocks = -(-(order_dur[o_idx] + _PROXY_BUFFER_MIN) // _FRONTLOAD_BLOCK_MIN)
            for (di, lijn) in allowed_slots[o_idx]:
                if di >= n_commit or (u, di, lijn) not in presence:
                    continue
                if (order.lever_datum - workdays[di]).days > _EARLY_GRACE_DAYS:
                    continue
                frontload_reward[(u, di, lijn)] = blocks * (n_commit - di) * _FRONTLOAD_WEIGHT
        best_reward: dict[int, int] = defaultdict(int)
        for (u, _di, _l), r in frontload_reward.items():
            best_reward[u] = max(best_reward[u], r)
        # The reward spans [-frontload_upper_bound, 0]. One unit of proximity
        # is scaled past that span, and efficiency past all of proximity, so
        # O7 can only decide between plans the other terms rate equal. With
        # O7 off the scale is 1 and every weight is what it was before.
        frontload_upper_bound = sum(best_reward.values())
        proximity_scale = frontload_upper_bound + 1
        efficiency_weight = proximity_upper_bound * proximity_scale
        total_order_units = sum(item.aantal for item in valid_orders)
        n_later = sum(
            1 for item in valid_orders
            if commit_end is None or item.lever_datum >= commit_end
        )
        for u, (o_idx, k) in enumerate(units):
            if _INCREMENTAL_RESOLVE and o_idx in stable_orders:
                continue
            order = valid_orders[o_idx]
            cand_slots = (
                [s for s in allowed_slots[o_idx] if s[0] not in stable_days]
                if _INCREMENTAL_RESOLVE else allowed_slots[o_idx]
            )
            voorkeur = order_voorkeur_lijn[o_idx]
            for (di, lijn) in cand_slots:
                dd = workdays[di]
                days_late = max(0, (dd - order.lever_datum).days)
                days_early = max(0, (order.lever_datum - dd).days)
                days_early_penalized = max(0, days_early - _EARLY_GRACE_DAYS)
                proximity_penalty = (
                    days_late * _LATE_PENALTY
                    + days_early_penalized * _EARLY_PENALTY
                )
                # L3 — this slot is on the line the product does not prefer.
                # Charged per chunk-slot, like lateness above: a split order
                # pays for each chunk that lands off-preference, which is
                # right, since each chunk occupies the wrong line on its own.
                if voorkeur is not None and lijn != voorkeur:
                    penalties.append(
                        presence[(u, di, lijn)]
                        * _OFF_PRIO_LINE_PENALTY
                        * efficiency_weight
                    )
                if proximity_penalty:
                    penalties.append(
                        presence[(u, di, lijn)] * proximity_penalty * proximity_scale
                    )
                reward = frontload_reward.get((u, di, lijn))
                if reward:
                    penalties.append(-presence[(u, di, lijn)] * reward)

        for o_idx in range(len(valid_orders)):
            if _INCREMENTAL_RESOLVE and o_idx in stable_orders:
                continue
            order = valid_orders[o_idx]
            if not _mandatory_orders:
                # One dropped order outweighs every unit in the book, so this
                # compact first-stage objective is exactly: rejected order
                # count, then rejected units. Efficiency is solved only after
                # this value has been fixed below; no unsafe giant coefficient
                # is needed to combine all four business priorities at once.
                weight = total_order_units + 1 + order.aantal
                if _URGENT_FIRST and commit_end is not None and order.lever_datum < commit_end:
                    weight *= n_later + 1
                rejection_terms.append((1 - order_used[o_idx]) * weight)
            us = order_units[o_idx]
            if len(us) > 1:
                extra_chunks = sum(chunk_used[u] for u in us) - order_used[o_idx]
                penalties.append(extra_chunks * _SPLIT_PENALTY * efficiency_weight)

        # Consolidation: small cost per activated (day, line) and per active
        # day, so the solver prefers concentrating a light day's orders onto
        # one line (and light weeks onto fewer days) over spreading them
        # thin, whenever nothing else forces the split. Kept far below the
        # lateness/unscheduled weights so a real deadline or capacity need
        # always overrides this.
        for la in line_active.values():
            penalties.append(la * _LINE_ACTIVATION_PENALTY * efficiency_weight)
        for da in day_active.values():
            penalties.append(da * _DAY_ACTIVATION_PENALTY * efficiency_weight)
        for fa in formaat_active.values():
            penalties.append(fa * _FORMAAT_SPREAD_PENALTY * efficiency_weight)
        for ia in ident_active:
            penalties.append(ia * _IDENTIEK_SPLIT_PENALTY * efficiency_weight)
        for ka in kap_active.values():
            penalties.append(ka * _KAPVORM_SPREAD_PENALTY * efficiency_weight)
        for _di, _lijn, st in kap_starts:
            penalties.append(st * _KAPVORM_CAMPAIGN_PENALTY * efficiency_weight)
        for sa in schaal_active.values():
            penalties.append(sa * _SCHAAL_SPREAD_PENALTY * efficiency_weight)
        for _di, _lijn, st in schaal_starts:
            penalties.append(st * _SCHAAL_CAMPAIGN_PENALTY * efficiency_weight)

        # L1 kapvorm changes, the planner's top rule (22/10). See
        # _L1_KAPVORM_CHANGE_PENALTY.
        if _L1_KAPVORM_CHANGE_PENALTY:
            l1_kaps: dict[int, list] = defaultdict(list)
            for (di, lijn, _kap), ka in kap_active.items():
                if lijn == 1:
                    l1_kaps[di].append(ka)
            for di, kas in l1_kaps.items():
                if len(kas) > 1 and (di, 1) in line_active:
                    penalties.append(
                        (sum(kas) - line_active[(di, 1)])
                        * _L1_KAPVORM_CHANGE_PENALTY * efficiency_weight
                    )
            for di, lijn, st in kap_starts:
                prev_la = line_active.get((di - 1, 1))
                if lijn != 1 or prev_la is None:
                    continue
                seam = model.NewBoolVar(f"l1_kap_seam_{di}_{st.Index()}")
                model.Add(seam >= st + prev_la - 1)
                penalties.append(seam * _L1_KAPVORM_CHANGE_PENALTY * efficiency_weight)

        # Real transition costs per (day, line): charge (n_distinct - 1) for
        # format families and kapvormen — each extra group beyond the first
        # means at least one cross-format/cross-kapvorm transition on that day,
        # priced at the actual extra minutes from calculate_setup_time.
        if _FORMAT_TRANSITION_PENALTY:
            fmt_by_line: dict[tuple[int, int], list] = defaultdict(list)
            for (di, lijn, _fmt), fa in formaat_active.items():
                fmt_by_line[(di, lijn)].append(fa)
            for (di, lijn), fvars in fmt_by_line.items():
                if len(fvars) > 1 and (di, lijn) in line_active:
                    penalties.append(
                        (sum(fvars) - line_active[(di, lijn)])
                        * _FORMAT_TRANSITION_PENALTY * efficiency_weight
                    )

        if _KAPVORM_TRANSITION_PENALTY:
            kap_by_line: dict[tuple[int, int], list] = defaultdict(list)
            for (di, lijn, _kap), ka in kap_active.items():
                kap_by_line[(di, lijn)].append(ka)
            for (di, lijn), kvars in kap_by_line.items():
                if len(kvars) > 1 and (di, lijn) in line_active:
                    penalties.append(
                        (sum(kvars) - line_active[(di, lijn)])
                        * _KAPVORM_TRANSITION_PENALTY * efficiency_weight
                    )

        # ── S2 "moet": one opener and one closer per line-day, so stacking
        # two products that MUST open on the same line-day breaks one of them
        # no matter how the day is sequenced. Nothing used to notice: the day
        # sequencer pays a bonus for honouring a requirement it happens to be
        # able to honour, and the assignment model — the only place that
        # decides which requirements land together — had no term at all. The
        # plan then shipped a day whose "moet opstarten" product runs fourth,
        # silently.
        #
        # Charged per requirement beyond the first, and deliberately soft: a
        # rejected order costs 10,000 plus its units, twenty times this, so the
        # solver spreads these across days when it can and swallows the clash
        # when the alternative is losing an order. The day is still built and
        # still ships; `validator.validate_schedule` reports the requirement
        # that could not be honoured instead of it passing unremarked.
        if _POSITIE_MOET_HARD:
            # Hard (planner 15/10): at most one article per line-day may claim
            # each end. Two orders of that article are one claim.
            for label, by_art in (("open", moet_open_art), ("close", moet_close_art)):
                for (di, lijn), arts in by_art.items():
                    if len(arts) < 2:
                        continue
                    claims = []
                    for art_nr, plist in arts.items():
                        c = model.NewBoolVar(f"moet_{label}_{di}_{lijn}_{art_nr}")
                        model.AddMaxEquality(c, plist)
                        claims.append(c)
                    model.Add(sum(claims) <= 1)
        else:
            for by_line in (moet_open_by_line, moet_close_by_line):
                for (di, lijn), plist in by_line.items():
                    if len(plist) < 2:
                        continue
                    excess = model.NewIntVar(0, len(plist), f"moet_excess_{di}_{lijn}")
                    model.Add(excess >= sum(plist) - 1)
                    penalties.append(excess * _POSITIE_MOET_PENALTY * efficiency_weight)

        if _mandatory_orders:
            if penalties:
                model.Minimize(sum(penalties))
        elif rejection_terms:
            model.Minimize(sum(rejection_terms))

        # The source workbook's production date is only a search hint. It is
        # never fixed and can be moved freely on every unfrozen legal day, but
        # it gives the long-horizon model a realistic incumbent immediately.
        for o_idx, us in order_units.items():
            if len(us) != 1 or (_INCREMENTAL_RESOLVE and o_idx in stable_orders):
                continue
            u = us[0]
            candidates = [
                (di, lijn) for di, lijn in allowed_slots[o_idx]
                if (u, di, lijn) in presence
            ]
            if not candidates:
                continue
            order = valid_orders[o_idx]
            preferred = order_voorkeur_lijn[o_idx]
            incumbent = incumbent_slots.get(u)
            if incumbent in candidates:
                di, lijn = incumbent
            else:
                di, lijn = min(
                    candidates,
                    key=lambda slot: (
                        abs((workdays[slot[0]] - order.productie_datum).days),
                        0 if preferred is None or slot[1] == preferred else 1,
                        -slot[0],
                    ),
                )
            model.AddHint(presence[(u, di, lijn)], 1)

        solve_budget = (
            min(iter_timeout, time_left)
            if _mandatory_orders
            else min(time_left, max(iter_timeout, time_left * 0.75))
        )
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(
            1.0, solve_budget if _mandatory_orders else solve_budget * 0.80
        )
        solver.parameters.num_search_workers = 8
        solver.parameters.log_search_progress = False
        status = solver.Solve(model)

        # Stage two cannot sacrifice a single placed order or unit: freeze the
        # best completeness value found, then spend the remaining budget on
        # scale/line/day consolidation and finally delivery proximity.
        if (
            not _mandatory_orders
            and rejection_terms
            and penalties
            and status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        ):
            rejection_expr = sum(rejection_terms)
            model.Add(rejection_expr == int(round(solver.ObjectiveValue())))
            model.Minimize(sum(penalties))
            if hasattr(model, "ClearHints"):
                model.ClearHints()
            for variable in presence.values():
                model.AddHint(variable, solver.Value(variable))
            refined = cp_model.CpSolver()
            refined.parameters.max_time_in_seconds = max(1.0, solve_budget * 0.20)
            refined.parameters.num_search_workers = 8
            refined.parameters.log_search_progress = False
            refined_status = refined.Solve(model)
            if refined_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                solver, status = refined, refined_status
                solver = _regroup_identical(model, solver, presence, ident_groups_units)
        return solver, status, presence, amt

    # ── Feedback iterations ──
    import time as _time
    t_start = _time.monotonic()
    iterations = 0
    last_solver_status = None
    final_scheduled: list[Order] = []
    final_scheduled_order_idx: set[int] = set()
    final_chunk_count: dict[int, int] = {}   # o_idx -> #chunks actually used, best iter
    converged_flag = False
    best_fitted_count = -1          # keep the BEST iteration, not the last
    best_iteration_score = None

    def _iteration_key(score, rejected_orders):
        """Fewest rejections due inside the commit window first, then the score."""
        urgent = 0
        if _URGENT_FIRST and commit_end is not None:
            urgent = len({
                order_identity(o) for o in rejected_orders if o.lever_datum < commit_end
            })
        return (urgent, score)
    best_never_placed = 0           # rejections: optimizer never placed them
    best_oracle_dropped = 0         # rejections: placed but rolled back (chunk didn't fit)
    best_rejection_cause: dict[int, str] = {}   # o_idx -> "never_placed" | "oracle_dropped"
    # Orders the BEST iteration lost to the position rule rather than to time.
    # Captured per best-iteration (not accumulated across all of them) so the
    # reason always describes the run actually being reported.
    best_s2_orders: set[int] = set()
    best_sate_orders: set[int] = set()
    no_improve_streak = 0

    for it in range(max_iters):
        iterations = it + 1
        elapsed = _time.monotonic() - t_start
        time_left = global_timeout - elapsed
        if time_left <= 1:
            break

        solver, status, presence, amt = build_and_solve(time_left)
        last_solver_status = status
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break

        # Extract per-(day, line) assignment at UNIT (chunk) granularity.
        # Stable units (FRIGI_INCREMENTAL_RESOLVE) are seeded directly from
        # their locked-in placement — they have no CP-SAT variables this
        # iteration, so the solver was never asked about them.
        assigned_by_day: dict[int, list[tuple[int, int]]] = defaultdict(list)  # di -> [(u, lijn)]
        assigned_by_line: dict[tuple[int, int], list[int]] = defaultdict(list)  # (di,lijn) -> [u]
        placed_slot: dict[int, tuple[int, int]] = {}
        unit_amt_val: dict[int, int] = {}
        for u, (di, lijn, amt_val) in stable_units.items():
            assigned_by_day[di].append((u, lijn))
            assigned_by_line[(di, lijn)].append(u)
            placed_slot[u] = (di, lijn)
            unit_amt_val[u] = amt_val
        for u, (o_idx, k) in enumerate(units):
            if _INCREMENTAL_RESOLVE and o_idx in stable_orders:
                continue
            for (di, lijn) in allowed_slots[o_idx]:
                if _INCREMENTAL_RESOLVE and di in stable_days:
                    continue
                if solver.Value(presence[(u, di, lijn)]) > 0.5:
                    assigned_by_day[di].append((u, lijn))
                    assigned_by_line[(di, lijn)].append(u)
                    placed_slot[u] = (di, lijn)
                    # K=1 units have no `amt` var — their duration is the full order.
                    unit_amt_val[u] = (
                        solver.Value(amt[(u, di, lijn)])
                        if (u, di, lijn) in amt else order_dur[o_idx]
                    )
                    break

        # Build the concrete Order copy for each placed unit. A split order's
        # units share its aantal proportional to their minutes (remainder to
        # the last chunk, so the total always equals the original exactly).
        unit_order_obj: dict[int, Order] = {}
        for o_idx in range(len(valid_orders)):
            us = [u for u in order_units[o_idx] if u in placed_slot]
            if not us:
                continue
            order = valid_orders[o_idx]
            if len(us) == 1:
                u = us[0]
                di, lijn = placed_slot[u]
                unit_order_obj[u] = replace(order, productie_datum=workdays[di], lijn=lijn)
                continue
            total_minutes = sum(unit_amt_val[u] for u in us) or 1
            remaining = order.aantal
            for i, u in enumerate(us):
                di, lijn = placed_slot[u]
                if i == len(us) - 1:
                    qty = remaining
                else:
                    qty = max(1, round(order.aantal * unit_amt_val[u] / total_minutes))
                    qty = min(qty, remaining - (len(us) - 1 - i))
                    remaining -= qty
                unit_order_obj[u] = replace(order, aantal=qty, productie_datum=workdays[di], lijn=lijn)

        # Run the REAL daily scheduler as oracle (per day — the two lines are
        # coupled by device staggering). Drops and conflicts become cuts.
        new_cuts: list[tuple[int, int, frozenset[int], int]] = []
        dropped_units: set[int] = set()
        s2_dropped_units: set[int] = set()
        sate_dropped_units: set[int] = set()
        conflict_count = 0
        days_with_new_conflict: set[int] = set()
        for di, slot_list in assigned_by_day.items():
            dd = workdays[di]
            unit_list = [u for u, _ in slot_list]
            cache_key = (di, frozenset((u, unit_order_obj[u].aantal, lijn) for u, lijn in slot_list))
            cached = _oracle_cache.get(cache_key)
            if cached is None:
                context = (
                    (_reserved_orders.get((dd, 1), []) + _reserved_orders.get((dd, 2), []))
                    if (_reserved_orders and _PHASE_CONTEXT) else None
                )
                cached = _run_oracle_day(
                    unit_list, unit_order_obj, products, setup_matrix, toestellen, dd,
                    context=context,
                )
                _oracle_cache[cache_key] = cached
            fit_units, conflict_pairs, s2_units, device_pairs, sate_units = cached
            s2_dropped_units |= s2_units
            sate_dropped_units |= sate_units

            day_assigned = set(unit_list)
            day_dropped = day_assigned - fit_units
            if day_dropped:
                dropped_units |= day_dropped
                # Attribute drops to their line -> precise per-(day, line) cuts
                for lijn in (1, 2):
                    line_units = assigned_by_line.get((di, lijn), [])
                    if not line_units or not (set(line_units) & day_dropped):
                        continue
                    new_cuts.append(
                        (di, lijn, frozenset(line_units), len(line_units) - 1)
                    )
                    if _USE_LEARNED_CAP:
                        learned_cap[(di, lijn)] = sum(
                            unit_amt_val[u] for u in line_units if u in fit_units
                        )

            # Device conflicts: forbid each conflicting pair from sharing a day
            # — every day (default), or just this one under day-scoped cuts.
            for (a, b) in conflict_pairs:
                conflict_count += 1
                scope_di = di if _DEVICE_CUT_DAY_SCOPED else None
                global_conflict_pairs.add((scope_di, frozenset((a, b))))
                for side in (a, b):
                    device_conflict_hits[units[side][0]] += 1

            # A pair the day builder actually lost an order over. ALWAYS scoped
            # to this day, whatever _DEVICE_CUT_DAY_SCOPED says about the
            # overlap-derived cuts above: the evidence is that TODAY's sequence
            # could not fit them around one machine, and on another day, with
            # other day-mates and other changeovers, they often can. Banning
            # the pair everywhere would be far more than this observation
            # supports — and unlike a generic "this set is too big" cut, this
            # one is never retracted, so it has to stay narrow.
            for (a, b) in device_pairs:
                conflict_count += 1
                global_conflict_pairs.add((di, frozenset((a, b))))
                for side in (a, b):
                    device_conflict_hits[units[side][0]] += 1

            if conflict_pairs or device_pairs:
                days_with_new_conflict.add(di)

        # Incremental-resolve: lock in any day that came back clean (no drops,
        # no cuts, no conflicts) and where every order on it is single-chunk.
        # Locked days are excluded from every order's candidate slots from the
        # NEXT iteration on, so this day's oracle verdict can never regress —
        # nothing new can ever be assigned to it again.
        if _INCREMENTAL_RESOLVE:
            cut_days = {di for (di, _lj, _u, _c) in new_cuts}
            for di, slot_list in assigned_by_day.items():
                if di in stable_days or di in cut_days or di in days_with_new_conflict:
                    continue
                if _COMMIT_EFFORT and commit_end and workdays[di] <= commit_end:
                    continue  # clean != full — keep the committed weeks open
                unit_list = [u for u, _ in slot_list]
                if any(u in dropped_units for u in unit_list):
                    continue
                if not all(len(order_units[units[u][0]]) == 1 for u in unit_list):
                    continue  # multi-chunk order present — leave this day free
                stable_days.add(di)
                for u, lijn in slot_list:
                    stable_units[u] = (di, lijn, unit_amt_val[u])
                    stable_orders.add(units[u][0])

        # Roll up to order granularity: an order counts as fitted only if ALL
        # of its placed chunks survived the oracle — no partial delivery.
        order_placed_units: dict[int, list[int]] = defaultdict(list)
        for u in placed_slot:
            o_idx, _k = units[u]
            order_placed_units[o_idx].append(u)

        fitted_order_idx = {
            o_idx for o_idx, us in order_placed_units.items()
            if not any(u in dropped_units for u in us)
        }

        candidate_scheduled = [
            unit_order_obj[u]
            for o_idx in fitted_order_idx
            for u in order_placed_units[o_idx]
        ]
        candidate_rejected = [
            order for o_idx, order in enumerate(valid_orders)
            if o_idx not in fitted_order_idx
        ] + [order for order, _reason in invalid_orders]
        candidate_rendered = schedule_all_days(
            candidate_scheduled,
            products,
            setup_matrix,
            toestellen,
            force_optimal_sequence=True,
        )
        # The exported plan uses this exact sequencer. Treat anything it
        # cannot render as an oracle drop now (and learn a cut), rather than
        # selecting a high-scoring incumbent that later loses orders during
        # Excel/dashboard generation.
        rendered_ids = {
            id(po.order)
            for day in candidate_rendered
            for po in (day.lijn1 + day.lijn2)
        }
        render_dropped_units = {
            u
            for o_idx in fitted_order_idx
            for u in order_placed_units[o_idx]
            if id(unit_order_obj[u]) not in rendered_ids
        }
        if render_dropped_units:
            dropped_units |= render_dropped_units
            for (di, lijn), line_units in assigned_by_line.items():
                if set(line_units) & render_dropped_units:
                    new_cuts.append(
                        (di, lijn, frozenset(line_units), len(line_units) - 1)
                    )
            fitted_order_idx = {
                o_idx for o_idx, us in order_placed_units.items()
                if not any(u in dropped_units for u in us)
            }
            candidate_scheduled = [
                unit_order_obj[u]
                for o_idx in fitted_order_idx
                for u in order_placed_units[o_idx]
            ]
            candidate_rejected = [
                order for o_idx, order in enumerate(valid_orders)
                if o_idx not in fitted_order_idx
            ] + [order for order, _reason in invalid_orders]

        elapsed_now = _time.monotonic() - t_start
        fitted_now = len(fitted_order_idx)
        candidate_score = calculate_plan_score(
            candidate_rendered,
            candidate_rejected,
            proximity_grace_days=_EARLY_GRACE_DAYS,
        )
        candidate_key = _iteration_key(candidate_score, candidate_rejected)
        is_best = best_iteration_score is None or candidate_key < best_iteration_score
        split_now = sum(1 for o_idx in fitted_order_idx if len(order_placed_units[o_idx]) > 1)
        print(
            f"      [feedback] iter {iterations:2}: "
            f"geplande={len(order_placed_units):3} - drops={len(order_placed_units) - fitted_now:2} "
            f"=> PAST={fitted_now:3}{' *beste*' if is_best else '        '}  "
            f"niet-geplaatst={len(valid_orders) - len(order_placed_units):3}  "
            f"gesplitst={split_now:3}  "
            f"toestelconflicten={conflict_count:2}  "
            f"timecuts={len(cuts) + len(new_cuts):4}  "
            f"({elapsed_now:.0f}s)"
        )

        if not dropped_units and conflict_count == 0:
            # Fully converged: every assigned unit fits AND no device conflicts
            final_scheduled = [unit_order_obj[u] for u in placed_slot]
            final_scheduled_order_idx = set(order_placed_units)
            final_chunk_count = {o: len(us) for o, us in order_placed_units.items()}
            best_rejection_cause = {
                o_idx: "never_placed"
                for o_idx in range(len(valid_orders))
                if o_idx not in final_scheduled_order_idx
            }
            # Convergence means dropped_units is empty by definition, so
            # oracle_dropped is genuinely 0 here — not "whatever it was at
            # the last non-converged best", which is what this looked like
            # before (this branch used to skip updating these entirely,
            # silently leaving stale counts from an earlier iteration).
            best_never_placed = len(valid_orders) - len(order_placed_units)
            best_oracle_dropped = 0
            best_s2_orders = set()   # converged: nothing was dropped at all
            best_sate_orders = set()
            converged_flag = True
            best_iteration_score = candidate_key
            break

        # Keep the BEST iteration (most orders that actually fit), not the last.
        if is_best:
            best_fitted_count = fitted_now
            best_iteration_score = candidate_key
            final_scheduled = candidate_scheduled
            final_scheduled_order_idx = set(fitted_order_idx)
            final_chunk_count = {o: len(order_placed_units[o]) for o in fitted_order_idx}
            # Split the rejections by CAUSE, per order (for reporting/analysis):
            #   never_placed   = optimizer found no room in the order's eligible days
            #   oracle_dropped = placed, but a chunk didn't survive the real oracle
            best_rejection_cause = {
                o_idx: ("oracle_dropped" if o_idx in order_placed_units else "never_placed")
                for o_idx in range(len(valid_orders))
                if o_idx not in final_scheduled_order_idx
            }
            best_never_placed = len(valid_orders) - len(order_placed_units)
            best_oracle_dropped = len(order_placed_units) - fitted_now
            best_s2_orders = {
                units[u][0] for u in s2_dropped_units
                if units[u][0] not in fitted_order_idx
            }
            best_sate_orders = {
                units[u][0] for u in sate_dropped_units
                if units[u][0] not in fitted_order_idx
            }
            incumbent_slots = {
                u: placed_slot[u]
                for o_idx in fitted_order_idx
                for u in order_placed_units[o_idx]
            }
            no_improve_streak = 0
        elif conflict_count > 0:
            # This iteration's fitted count didn't improve, but it wasn't a
            # genuine plateau either: a device-conflict cut was just learned
            # (a pair banned from sharing a day, or a day-scoped device-drop
            # pair). That cut changes next iteration's search space, so the
            # solver hasn't actually tried the new information yet — counting
            # it against patience would let a resolvable device clash get
            # abandoned before the cut it just earned was ever exploited.
            no_improve_streak = 0
        else:
            no_improve_streak += 1

        cuts.extend(new_cuts)

        # Early stop: fitted count has plateaued (can't place all orders — the
        # remaining rejects genuinely don't fit given real capacity/devices).
        if no_improve_streak >= _NO_IMPROVE_STOP:
            print(
                f"      [feedback] plateau na {iterations} iteraties "
                f"(beste={best_fitted_count} orders passen) — gestopt"
            )
            break

    # Determine rejected orders
    rejected_orders: list[Order] = [order for order, _ in invalid_orders]
    rejection_reasons: dict[str, str] = {}
    for order, reason in invalid_orders:
        rejection_reasons[order_identity(order)] = reason

    # Any valid order whose index didn't make the final cut is rejected.
    for o_idx, order in enumerate(valid_orders):
        if o_idx in final_scheduled_order_idx:
            continue
        rejected_orders.append(order)
        rkey = order_identity(order)
        if not allowed_slots.get(o_idx, []):
            rejection_reasons[rkey] = slots_mod.describe_no_slot(
                order,
                no_slot_cause.get(o_idx, slots_mod.LINE_DAY_RESTRICTION),
                start_date,
                frozen_until,
            )
        elif o_idx in best_s2_orders:
            rejection_reasons[rkey] = _positie_reason(order, products.get(order.art_nr))
        elif o_idx in best_sate_orders:
            rejected_line = next(
                (unit_order_obj[u].lijn for u in order_units[o_idx] if u in unit_order_obj),
                order.lijn or 1,
            )
            rejection_reasons[rkey] = _sate_reason(
                order, products.get(order.art_nr), rejected_line
            )
        elif best_rejection_cause.get(o_idx) == "oracle_dropped":
            rejection_reasons[rkey] = (
                f"ORACLE-DROPPED: the optimizer assigned this order to a day, but the "
                f"real daily schedule (actual sequence-dependent setup times) could not "
                f"fit it once built — capacity looked available in the approximate model "
                f"but wasn't once real changeovers were applied. Deadline "
                f"{order.lever_datum}, {iterations} feedback iterations, "
                f"up to {order_chunks[o_idx]} chunks."
                + _device_note(order, products, toestellen, device_conflict_hits.get(o_idx, 0))
            )
        else:
            rejection_reasons[rkey] = (
                f"NEVER-PLACED: the optimizer could not find ANY day/line slot with room "
                f"for this order within its delivery window before {order.lever_datum}, "
                f"across {iterations} feedback iterations, even split across "
                f"up to {order_chunks[o_idx]} chunks — genuinely oversubscribed, not a "
                f"sequencing artifact."
                + _device_note(order, products, toestellen, device_conflict_hits.get(o_idx, 0))
            )

    orders_split = sum(1 for n in final_chunk_count.values() if n > 1)

    def _reason_count(prefix: str) -> int:
        return sum(
            reason.startswith(prefix) for reason in rejection_reasons.values()
        )

    total_elapsed = _time.monotonic() - t_start
    converged = converged_flag

    primary_infeasible = last_solver_status == cp_model.INFEASIBLE
    if _mandatory_orders and primary_infeasible:
        diagnostic = assign_production_dates_feedback(
            orders,
            products,
            start_date,
            weeks=weeks,
            setup_matrix=setup_matrix,
            toestellen=toestellen,
            _reserved_capacity=_reserved_capacity,
            _reserved_ends=_reserved_ends,
            _reserved_orders=_reserved_orders,
            _force_split_eligible=_force_split_eligible,
            frozen_until=frozen_until,
            commit_end=commit_end,
            _mandatory_orders=False,
        )
        diagnostic.stats["primary_solver_status"] = "INFEASIBLE"
        return replace(diagnostic, status="infeasible")

    stats = {
        "total_orders": len(orders),
        "scheduled_orders": len(final_scheduled_order_idx),
        "rejected_orders": len(rejected_orders),
        "scheduled_units": sum(o.aantal for o in final_scheduled),
        "rejected_units": sum(o.aantal for o in rejected_orders),
        "orders_split": orders_split,
        "solver_status": str(last_solver_status),
        "solver_time_seconds": round(total_elapsed, 2),
        "solver_gap": None,
        "rejection_reasons": rejection_reasons,
        "planning_window": f"{start_date} to {end_date}",
        "feedback": {
            "iterations": iterations,
            "converged": converged,
            "cuts_added": len(cuts),
            "never_placed": best_never_placed,
            "oracle_dropped": best_oracle_dropped,
            "arrived_after_freeze": _reason_count("ARRIVED-AFTER-FREEZE"),
            "window_passed": _reason_count("WINDOW-PASSED"),
            "line_day_restriction": _reason_count("LINE-DAY-RESTRICTION"),
            "positie_regel": _reason_count("POSITIE-REGEL"),
            "sate": _reason_count("SATE"),
            "note": "Real schedule_day used as oracle; scheduled orders are guaranteed to fit the daily plan.",
        },
        "solver_config": {
            "timeout_seconds": global_timeout,
            "optimization_objective": "feedback_loop_v3_real_setups",
        },
    }

    diagnostics: list[RejectionDiagnostic] = []
    for order in rejected_orders:
        key = order_identity(order)
        reason = rejection_reasons.get(key, "")
        if reason.startswith("ARRIVED-AFTER-FREEZE"):
            category = "late_after_freeze"
        elif reason.startswith(("Product ", "MISSING-SPEED")):
            category = "invalid_master_data"
        elif reason.startswith(("WINDOW-PASSED", "LINE-DAY-RESTRICTION")):
            category = "proven_infeasible"
        elif last_solver_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            category = "search_limit"
        else:
            category = "model_mismatch"
        diagnostics.append(RejectionDiagnostic(key, category, detail=reason))

    if not rejected_orders and converged:
        outcome_status = "complete"
    elif diagnostics and all(
        item.category in {"late_after_freeze", "proven_infeasible", "invalid_master_data"}
        for item in diagnostics
    ):
        outcome_status = "infeasible"
    else:
        outcome_status = "unknown"

    rendered = schedule_all_days(
        final_scheduled, products, setup_matrix, toestellen,
        force_optimal_sequence=True,
    )
    score = calculate_plan_score(
        rendered,
        rejected_orders,
        proximity_grace_days=_EARLY_GRACE_DAYS,
    )
    stats["outcome_status"] = outcome_status
    stats["rejection_diagnostics"] = [
        {
            "order_key": item.order_key,
            "category": item.category,
            "attempted_slots": list(item.attempted_slots),
            "blocking_constraints": list(item.blocking_constraints),
            "detail": item.detail,
        }
        for item in diagnostics
    ]
    stats["plan_score"] = {
        name: getattr(score, name) for name in score.__dataclass_fields__
    }
    return SolveOutcome(
        status=outcome_status,
        scheduled=tuple(final_scheduled),
        rejected=tuple(rejected_orders),
        score=score,
        diagnostics=tuple(diagnostics),
        stats=stats,
    )


def reconcile_combined_schedule(
    locked: list[Order],
    free: list[Order],
    rejected: list[Order],
    rejection_reasons: dict[str, str],
    products: dict[str, Product],
    start_date: date,
    weeks: int,
    setup_matrix: dict,
    toestellen: dict[str, int] | None,
    max_rounds: int = 3,
    commit_end: date | None = None,
) -> tuple[list[Order], list[Order], dict[str, str]]:
    """
    Verify that `locked` (untouchable — carried forward from a previous
    run's commit) and `free` (this run's own fresh solve, possibly itself
    assembled from separately-solved phases) actually survive being
    produced TOGETHER for real.

    Every batch was verified by its OWN oracle against its own orders only;
    other batches were represented to it as a reserved-minutes budget, never
    as real orders (see _assign_two_phase_hard_first's `reserved_capacity`).
    So an order "confirmed" within its own batch can still get dropped once
    `schedule_all_days` walks the REAL combined order set for a day — real
    sequence-dependent setups, real kapvorm grouping, real cross-line device
    staggering, none of which the minutes-budget approximation captures.

    `locked` orders are NEVER eligible for the drop/retry machinery below —
    they were already committed to production/customers by an earlier run
    and their production window is frozen (walled off from the solver
    entirely, see create_plan.py's `solver_start`), so there is
    nowhere they even COULD be retried to. If the real combined schedule
    doesn't render one, that's surfaced as a loud warning (a residual
    non-monotonicity in the day-sequencing heuristic under a shrunk order
    set — see the warning text below), never as a silent drop or a
    reroute/rejection of an order that was supposed to be untouchable.

    Only `free` orders participate in detect-drop-and-retry, repeated until
    nothing drops or `max_rounds` is exhausted (whatever's still dropping
    after that is genuinely, physically contested against the rest of the
    confirmed plan, not a reconciliation artifact).
    """
    reasons = dict(rejection_reasons)
    confirmed_free = list(free)
    extra_rejected: list[Order] = []
    input_identities = {order_identity(o) for o in free}
    locked_ids = {id(o) for o in locked}

    def _expand_and_merge(raw_dropped: list[Order], pool: list[Order]) -> tuple[list[Order], list[Order], int]:
        """
        Pull EVERY chunk sharing an identity with any order in `raw_dropped`
        out of `pool`, and merge each identity's chunks back into one whole
        Order (summed aantal).

        A split order's chunks must be retried — or rejected — atomically,
        never independently: if only the specific dropped fragment(s) were
        retried on their own, the solver would treat each fragment as its
        own tiny standalone order (it has no way to know a 3,497-unit Order
        object is one quarter of a 26,810-unit whole), silently breaking the
        "no partial delivery" guarantee across rounds — part of the order
        could recover while the rest quietly stays lost. Merging back into
        one whole Order before retrying lets the solver's own chunk-or-
        nothing constraint decide on the ENTIRE order again, exactly as if
        it were being solved for the first time.
        """
        dropped_identities = {order_identity(o) for o in raw_dropped}
        all_chunks = [o for o in pool if order_identity(o) in dropped_identities]
        remaining_pool = [o for o in pool if order_identity(o) not in dropped_identities]
        by_identity: dict[str, list[Order]] = defaultdict(list)
        for o in all_chunks:
            by_identity[order_identity(o)].append(o)
        merged = [
            replace(chunks[0], aantal=sum(c.aantal for c in chunks))
            for chunks in by_identity.values()
        ]
        return merged, remaining_pool, len(all_chunks)

    for round_n in range(max_rounds):
        schedules = schedule_all_days(locked + confirmed_free, products, setup_matrix, toestellen)
        planned_ids = {id(po.order) for s in schedules for po in (s.lijn1 + s.lijn2)}

        locked_missing = [o for o in locked if id(o) not in planned_ids]
        if locked_missing:
            print(
                f"      [RECONCILE] WARNING: {len(locked_missing)} LOCKED (frozen) order(s) "
                f"did not come out of the real combined day-schedule even though they were "
                f"already committed by an earlier run. They are kept as scheduled regardless "
                f"(a locked order is never re-planned or rejected) — this indicates the "
                f"day-sequencing heuristic isn't fully stable when the set of orders sharing "
                f"a day shrinks, and is worth a closer look if it recurs: "
                f"{sorted(order_identity(o) for o in locked_missing)[:5]}"
            )

        raw_dropped = [o for o in confirmed_free if id(o) not in planned_ids]
        if not raw_dropped:
            break

        dropped, confirmed_free, n_chunks = _expand_and_merge(raw_dropped, confirmed_free)
        print(
            f"\n      [RECONCILE] round {round_n + 1}: {len(dropped)} order(s) "
            f"({n_chunks} chunk(s)) confirmed individually but dropped once produced "
            f"together with the rest of the day — retrying as whole orders against real "
            f"remaining capacity:"
        )

        so_far = schedule_all_days(
            locked + confirmed_free, products, setup_matrix, toestellen,
            force_optimal_sequence=_COMMIT_EFFORT and commit_end is not None,
            force_until=commit_end,
        )
        reserved: dict[tuple[date, int], int] = {}
        for s in so_far:
            reserved[(s.datum, 1)] = s.kpis.get("lijn1_werk_min", 0)
            reserved[(s.datum, 2)] = s.kpis.get("lijn2_werk_min", 0)

        recovered, still_rejected, stats_r = assign_production_dates_feedback(
            dropped, products, start_date, weeks=weeks,
            setup_matrix=setup_matrix, toestellen=toestellen,
            _reserved_capacity=reserved, _reserved_ends=_reserved_ends_from(so_far),
            _reserved_orders=_reserved_orders_from(so_far),
            _force_split_eligible=True,
            commit_end=commit_end,
        )
        print(
            f"      [RECONCILE] round {round_n + 1} done: {len(recovered)} chunk(s) recovered "
            f"across {len({order_identity(o) for o in recovered})} of {len(dropped)} order(s), "
            f"{len(still_rejected)} order(s) could not be placed at all"
        )
        confirmed_free.extend(recovered)
        reasons.update(stats_r.get("rejection_reasons", {}))
        for o in still_rejected:
            reasons.setdefault(
                order_identity(o),
                "COMBINED-SCHEDULE-DROP: fit when checked against its own batch's reserved "
                "capacity, but the real combined schedule had no room once retried directly.",
            )
        extra_rejected.extend(still_rejected)
    else:
        # Loop exhausted max_rounds without a clean round — whatever's still
        # dropping now is left in `confirmed_free` from the last
        # (never-reverified) extend(); one last check moves any stragglers to
        # rejected instead of silently shipping an unconfirmed order. Locked
        # orders are excluded from this straggler sweep for the same reason
        # as above — never eligible for rejection.
        schedules = schedule_all_days(locked + confirmed_free, products, setup_matrix, toestellen)
        planned_ids = {id(po.order) for s in schedules for po in (s.lijn1 + s.lijn2)}
        raw_stragglers = [
            o for o in confirmed_free if id(o) not in planned_ids and id(o) not in locked_ids
        ]
        if raw_stragglers:
            stragglers, confirmed_free, _ = _expand_and_merge(raw_stragglers, confirmed_free)
            for o in stragglers:
                reasons.setdefault(
                    order_identity(o),
                    f"COMBINED-SCHEDULE-DROP: still not placeable in the real combined "
                    f"schedule after {max_rounds} reconciliation rounds.",
                )
            extra_rejected.extend(stragglers)

    confirmed = locked + confirmed_free
    final_rejected = list(rejected) + extra_rejected
    accounted = {order_identity(o) for o in confirmed_free} | {order_identity(o) for o in final_rejected}
    lost = input_identities - accounted
    if lost:
        print(
            f"      [RECONCILE] WARNING: {len(lost)} order identit(y/ies) present in the "
            f"input went missing during reconciliation (neither scheduled nor rejected): "
            f"{sorted(lost)[:10]}"
        )

    return confirmed, final_rejected, reasons


def _assign_two_phase_hard_first(
    orders: list[Order],
    products: dict[str, Product],
    start_date: date,
    weeks: int,
    setup_matrix: dict,
    toestellen: dict[str, int] | None,
    frozen_until: date | None = None,
    commit_end: date | None = None,
) -> tuple[list[Order], list[Order], dict]:
    """
    Most-constrained-first: solve line-restricted orders against FULL
    capacity, then flexible (either-line) orders against whatever real
    capacity that left behind. See FRIGI_TWO_PHASE_HARD_FIRST above.

    Phase 1's REAL daily schedule (schedule_all_days, with actual sequence-
    dependent setups — not the light model's approximate proxy) determines
    how much room phase 2 actually has, so phase 2 never overcommits based
    on an optimistic estimate of what phase 1 used.
    """
    hard_orders, flex_orders = [], []
    for o in orders:
        product = products.get(o.art_nr)
        if product is not None and (
            product.alleen_l1 or product.alleen_l2
            or product.l1_uitgesloten or product.l2_uitgesloten
        ):
            hard_orders.append(o)
        else:
            flex_orders.append(o)  # includes unknown-product orders; phase 2 flags them same as always

    print(
        f"\n      [PHASE 1/3] Line-restricted orders only "
        f"({len(hard_orders)} of {len(orders)} orders) — solving against FULL capacity:"
    )
    scheduled_hard, rejected_hard, stats_hard = assign_production_dates_feedback(
        hard_orders, products, start_date, weeks=weeks,
        setup_matrix=setup_matrix, toestellen=toestellen,
        _reserved_capacity={},  # non-None: bypasses the two-phase dispatch above
        frozen_until=frozen_until, commit_end=commit_end,
    )
    print(
        f"      [PHASE 1/3] done: {stats_hard.get('scheduled_orders', 0)} of "
        f"{len(hard_orders)} line-restricted orders scheduled"
    )

    schedules_hard = schedule_all_days(
        scheduled_hard, products, setup_matrix, toestellen,
        force_optimal_sequence=_COMMIT_EFFORT and commit_end is not None, force_until=commit_end,
    )
    reserved: dict[tuple[date, int], int] = {}
    for s in schedules_hard:
        reserved[(s.datum, 1)] = s.kpis.get("lijn1_werk_min", 0)
        reserved[(s.datum, 2)] = s.kpis.get("lijn2_werk_min", 0)

    print(
        f"\n      [PHASE 2/3] Flexible (either-line) orders only "
        f"({len(flex_orders)} of {len(orders)} orders) — solving against what phase 1 left:"
    )
    scheduled_flex, rejected_flex, stats_flex = assign_production_dates_feedback(
        flex_orders, products, start_date, weeks=weeks,
        setup_matrix=setup_matrix, toestellen=toestellen,
        _reserved_capacity=reserved, _reserved_ends=_reserved_ends_from(schedules_hard),
        _reserved_orders=_reserved_orders_from(schedules_hard),
        frozen_until=frozen_until,
        commit_end=commit_end,
    )
    print(
        f"      [PHASE 2/3] done: {stats_flex.get('scheduled_orders', 0)} of "
        f"{len(flex_orders)} flexible orders scheduled"
    )

    reasons_so_far = dict(stats_hard.get("rejection_reasons", {}))
    reasons_so_far.update(stats_flex.get("rejection_reasons", {}))

    # Phase 3: whatever's left is either structurally impossible (no legal
    # day/line at all, or unknown product — retrying can't help) or genuinely
    # contested (NEVER-PLACED / ORACLE-DROPPED — splitting across two days'
    # worth of leftover might succeed where one whole day couldn't).
    def _retryable(o: Order) -> bool:
        # ARRIVED-AFTER-FREEZE / WINDOW-PASSED / LINE-DAY-RESTRICTION / unknown
        # product are all structural: the order has no legal slot at all, so
        # another solve against the same window cannot help.
        reason = reasons_so_far.get(order_identity(o), "")
        return reason.startswith("NEVER-PLACED") or reason.startswith("ORACLE-DROPPED")

    still_rejected = rejected_hard + rejected_flex
    retry_candidates = [o for o in still_rejected if _retryable(o)]
    permanently_rejected = [o for o in still_rejected if not _retryable(o)]

    if _RETRY_SPLIT_NEVER_PLACED and retry_candidates:
        print(
            f"\n      [PHASE 3/3] Retry with forced splitting "
            f"({len(retry_candidates)} of {len(orders)} orders — genuinely contested, "
            f"not structurally impossible; {len(permanently_rejected)} others already "
            f"permanently excluded) — solving against what phases 1+2 left:"
        )
        schedules_so_far = schedule_all_days(
            scheduled_hard + scheduled_flex, products, setup_matrix, toestellen,
            force_optimal_sequence=_COMMIT_EFFORT and commit_end is not None, force_until=commit_end,
        )
        reserved2: dict[tuple[date, int], int] = {}
        for s in schedules_so_far:
            reserved2[(s.datum, 1)] = s.kpis.get("lijn1_werk_min", 0)
            reserved2[(s.datum, 2)] = s.kpis.get("lijn2_werk_min", 0)
        scheduled_retry, rejected_retry, stats_retry = assign_production_dates_feedback(
            retry_candidates, products, start_date, weeks=weeks,
            setup_matrix=setup_matrix, toestellen=toestellen,
            _reserved_capacity=reserved2,
            _reserved_ends=_reserved_ends_from(schedules_so_far),
            _reserved_orders=_reserved_orders_from(schedules_so_far),
            _force_split_eligible=True,
            commit_end=commit_end,
        )
        print(
            f"      [PHASE 3/3] done: {stats_retry.get('scheduled_orders', 0)} of "
            f"{len(retry_candidates)} contested orders recovered "
            f"({stats_retry.get('orders_split', 0)} needed splitting)"
        )
    else:
        scheduled_retry, rejected_retry, stats_retry = [], retry_candidates, {}
        if retry_candidates:
            print(
                f"\n      [PHASE 3/3] skipped (FRIGI_RETRY_SPLIT_NEVER_PLACED=0) — "
                f"{len(retry_candidates)} contested orders left unretried"
            )

    scheduled = scheduled_hard + scheduled_flex + scheduled_retry
    rejected = permanently_rejected + rejected_retry
    reasons = dict(reasons_so_far)
    reasons.update(stats_retry.get("rejection_reasons", {}))

    # Each phase above only verified its own orders against a reserved-
    # minutes budget, never against the OTHER phases' real orders — close
    # that gap now by checking (and if needed, repairing) the real combined
    # schedule. See reconcile_combined_schedule's docstring.
    print(
        f"\n      [RECONCILE] verifying all {len(scheduled)} scheduled order-chunks "
        f"survive being produced together for real..."
    )
    scheduled, rejected, reasons = reconcile_combined_schedule(
        [], scheduled, rejected, reasons, products, start_date, weeks, setup_matrix, toestellen,
        commit_end=commit_end,
    )
    scheduled_orders_final = len({order_identity(o) for o in scheduled})

    # Recompute never_placed/oracle_dropped from the FINAL rejected set's own
    # reason tags — not summed from each phase's internal counters, which
    # count orders as rejected within that phase even if phase 3 later
    # rescued them (retry-recovered orders would otherwise get double-
    # counted: scheduled AND still tallied as a phase-1/2 rejection).
    def _count(prefix: str) -> int:
        return sum(1 for o in rejected if reasons.get(order_identity(o), "").startswith(prefix))

    never_placed_final = _count("NEVER-PLACED")
    oracle_dropped_final = _count("ORACLE-DROPPED")
    arrived_after_freeze = _count("ARRIVED-AFTER-FREEZE")
    window_passed = _count("WINDOW-PASSED")
    line_day_restriction = _count("LINE-DAY-RESTRICTION")
    combined_drop = _count("COMBINED-SCHEDULE-DROP")
    positie_regel = _count("POSITIE-REGEL")

    if arrived_after_freeze:
        print(
            f"\n      [FREEZE] {arrived_after_freeze} order(s) can never be produced: their "
            f"delivery window lies entirely inside the zone frozen by an earlier run's commit. "
            f"No future solve will pick these up — they need a planner decision."
        )

    fb_hard = stats_hard.get("feedback", {})
    fb_flex = stats_flex.get("feedback", {})
    fb_retry = stats_retry.get("feedback", {})
    stats = {
        "total_orders": len(orders),
        # NOT len(scheduled) — a split order becomes multiple rows (one real
        # production run per chunk), so the row count over-counts unique
        # orders. Counted post-reconciliation by unique identity, since
        # reconcile_combined_schedule can move orders between scheduled and
        # rejected relative to what each phase originally reported.
        "scheduled_orders": scheduled_orders_final,
        "rejected_orders": len(rejected),
        "scheduled_units": sum(o.aantal for o in scheduled),
        "rejected_units": sum(o.aantal for o in rejected),
        "orders_split": (
            stats_hard.get("orders_split", 0) + stats_flex.get("orders_split", 0)
            + stats_retry.get("orders_split", 0)
        ),
        "solver_status": (
            f"hard={stats_hard.get('solver_status')} flex={stats_flex.get('solver_status')} "
            f"retry={stats_retry.get('solver_status')}"
        ),
        "solver_time_seconds": round(
            stats_hard.get("solver_time_seconds", 0) + stats_flex.get("solver_time_seconds", 0)
            + stats_retry.get("solver_time_seconds", 0), 2
        ),
        "solver_gap": None,
        "rejection_reasons": reasons,
        "planning_window": stats_hard.get("planning_window"),
        "feedback": {
            "iterations": max(fb_hard.get("iterations", 0), fb_flex.get("iterations", 0), fb_retry.get("iterations", 0)),
            "converged": fb_hard.get("converged", False) and fb_flex.get("converged", False)
                         and (fb_retry.get("converged", True)),
            "cuts_added": fb_hard.get("cuts_added", 0) + fb_flex.get("cuts_added", 0) + fb_retry.get("cuts_added", 0),
            "never_placed": never_placed_final,
            "oracle_dropped": oracle_dropped_final,
            "arrived_after_freeze": arrived_after_freeze,
            "window_passed": window_passed,
            "line_day_restriction": line_day_restriction,
            "combined_drop": combined_drop,
            "positie_regel": positie_regel,
            "note": "Three-phase: line-restricted orders first (full capacity), then flexible "
                    "orders (remaining capacity), then a force-split retry on whatever's still "
                    "genuinely contested (not structurally impossible).",
        },
        "solver_config": {
            "two_phase_hard_first": True,
            "retry_split_never_placed": _RETRY_SPLIT_NEVER_PLACED,
            "hard_orders": len(hard_orders),
            "flex_orders": len(flex_orders),
            "retry_candidates": len(retry_candidates),
        },
    }
    print(
        f"\n      [PHASES 1+2+3 TOTAL] {stats_hard.get('scheduled_orders', 0)} (hard) + "
        f"{stats_flex.get('scheduled_orders', 0)} (flex) + {stats_retry.get('scheduled_orders', 0)} "
        f"(retry) = {stats['scheduled_orders']} of {len(orders)} orders scheduled\n"
    )
    return scheduled, rejected, stats
