# Blueprint: Planner feedback 02/10 — grouping, end-of-shift, sate handover, 2× Gernal/Leonard

## Executor Instructions

You are executing this blueprint. Follow these rules:

1. **Read this file first.** Every loop, re-read this file before doing anything.
   After context compaction, this file is your ground truth.
2. **Do the next unchecked step.** Find the first `- [ ]` item. Do that. Only that.
3. **Verify before checking off.** Run the verification described in the step.
   If it passes, change `- [ ]` to `- [x]` and commit.
4. **Commit after each step.** `git add -A && git commit -m "blueprint: [step title]"`
   — but NEVER stage `data/source_data/*.xlsx` / `*.xlsm` (the client edits those;
   they are modified in the working tree on purpose). Use
   `git add -A -- . ':!data/source_data'` instead of a bare `git add -A`.
5. **Don't skip ahead.** Steps are ordered by dependency.
6. **Follow existing patterns.** When the step references an existing file as an example,
   match its structure. Don't invent new patterns.
7. **If stuck, document and move on.** If a step is blocked, add a note under it explaining
   why, check it off as blocked, and move to the next step. Don't spiral.
8. **Update this file.** If you discover something during execution that future steps need
   to know, add a note in the relevant step. Keep the blueprint as the single source of truth.

**Note (Liam, 2026-10-09):** ignore the existing `output/` plans — they will be re-run for week 45. All measurement/verification runs use `--week 45 --year 2026 --dry-run --reset` instead of week 40; the week-40 planner examples (Step 21) are checked by analogous situations in the week-45 run, not by date.

Environment: Windows, no `python` on PATH — use `py`. Tests: `py -m pytest -q`.
The scheduler is NON-DETERMINISTIC run to run (wall-clock budgeted feedback loop):
never judge a change on one `create_plan.py` run — use ≥2 runs per arm and report ranges.

## Context

Frigilunch plans food-production orders on two lines (L1 retail/automated, L2 manual).
`create_plan.py --week N --year Y` runs a rolling weekly plan: an OR-Tools CP-SAT
**assignment model** (`src/scheduler_ortools_v3.py::assign_production_dates_feedback`)
chooses a (day, line) per order, and a **day builder** (`src/scheduler.py::schedule_day`)
turns each day into a timeline: it sorts each line (`src/constraints/sequence_rules.py::
sort_orders_for_lijn`), walks the list (`_schedule_lijn`), optionally asks a CP-SAT
sequencer (`src/day_sequencer.py::solve_optimal_sequence`) for a better order, staggers
the second line around single-unit machines (M1), and applies the sate rule (A5: while
sate/peanut runs on one line the other line stands still). Read `ARCHITECTURE.md` and
`docs/constraints.md` (generated from `src/constraints/registry.py`) for the full picture.

The production planner reviewed week 40 (meeting 02/10). Four changes come out of it:

1. **Sate handover.** On 2026-10-02 L1 stopped at 13:05 while L2's sate started 14:20
   (window opening 13:50 incl. its 30-min changeover): 45 minutes nobody produces. Cause:
   `_align_sate_tail` (`src/scheduler.py`) ALWAYS slides the trailing sate block to closing
   time, even when the other line already stopped. Same on 2026-09-30 (L1 ends 19:55, sate
   window opens 20:45). Wanted: the sate starts the moment the other line is done.
2. **Identical products grouped, then same kapvorm.** The product database column 15
   "identiek product" links products that are the same recipe in a different pack
   (e.g. VAT ↔ VAT EW, DMB ↔ DMB GESNEDEN, the Lidl pho/tom-kha triples). These must be
   planned on the same day+line, back to back, smallest first. Second layer: same kapvorm
   together. BOTH layers must act in the assignment model (choose the same day/line) and
   in the day sequence (adjacent). Today `identiek_product` is written to products.json and
   never read (constraint G1 "NOT ENFORCED"), and kapvorm only groups inside a day.
   On L2 kapvorm is almost always `NVT` → that layer simply falls through.
3. **Plan to the end of the shift, no gaps.** Line-days end early and contain long idle
   holes (2026-09-28 L2 idle 14:30→21:50; 2026-09-29 L2 starts 08:40; 2026-10-22 L2 idle
   09:30→18:20). Two causes, both confirmed in code:
   - The assignment model books `_PROXY_BUFFER_MIN = 60` minutes of changeover per order;
     the real average in the committed plan is **19.5 min per order** (26.1 per actual
     changeover, 118 changeovers). A day with 8 orders reserves ~320 phantom minutes, so
     the model believes the day is full while the built day ends hours early.
   - Nothing scores idle time. The CP-SAT sequencer minimises changeover only, so a
     sequence that pushes a gernal order past the other line's machine block to 21:50 is
     "as good" as one that runs it at 07:00. `_maybe_recover_drops(force=True)` also only
     accepts a proposal with less total ombouw.
4. **2 Gernals and 2 Leonards.** Just set the unit count to 2 (no date logic). A machine
   with ≥2 units is never staggered or capacity-limited across lines.

**Prerequisite discovered during planning (Step 1):** the client inserted a column
"Kliklok L2" at column 52 of `Planning info.xlsx` → `product database`. The loader
(`src/product_setup_loader.py`) reads every column by fixed index, so everything from
column 52 on is now off by one: measured on the new workbook, 560/627 products get wrong
allergens and 101 products silently lose Monday+Thursday (fish fallback reads the wrong
column). `create_plan.py` regenerates products.json from the Excel at the start of EVERY
run, so no verification run in this blueprint is meaningful until that is fixed.

## Scope

**In scope:**
- Header-based column lookup in `product_setup_loader.py` (prerequisite).
- Gernal and Leonard availability = 2.
- Identical-product groups parsed from column 15; grouped in assignment model + day sort + sequencer.
- Kapvorm as second grouping layer in assignment model + day sort + sequencer.
- Idle-time term in the sequencer; one shared day-quality score used by every "which build is better" comparison.
- Lower proxy buffer, chosen by a measured A/B.
- Sate handover: closing sate block starts right after the other line's last order.
- Layout metrics printed per run, registry/docs updates, tests.

**Out of scope:**
- Date-effective machine counts (W46), week-45 closure calendar, Kliklok L2 line logic.
- Shift-time changes (22:30 / 20:30 / L2 06:30 stay as in `src/config.py`).
- Karton-type grouping on L2, kitchen cross-contamination (A2) changes, line-preference vs "opstart moet" weights.
- Re-planning frozen weeks (plan_state `frozen_until` = 2026-10-26); measurements use `--dry-run --reset`.
- Days where BOTH lines carry sate: keep today's queueing behaviour unchanged.

## Architecture Decisions

- **Groups are a product property, computed once at load.** A new module
  `src/constraints/grouping_rules.py` owns parsing + union-find. `Product` gets
  `identiek_groep: str | None`. Every consumer (assignment model, sort, sequencer, metrics)
  reads the field — nobody re-parses the string. Follows how `line_rules.derive_line_info`
  is applied once in `src/loader.py`.
- **Soft, not hard.** Grouping is a penalty/bonus everywhere. A group that cannot share a
  day (weekdays, capacity, delivery window) is split, never rejected. Rejections stay
  governed by the existing lexicographic objective (unscheduled orders dominate everything).
- **Weight order (assignment model, all × `efficiency_weight` like the existing terms):**
  off-preferred-line 50 > **identical split 40** > line activation 5 > **kapvorm spread 3**
  > day activation 2 > formaat spread 1. Identical stays under the line-preference rule
  from the 04/09 meeting on purpose (identical products nearly always share line rules).
  Mirror `_FORMAAT_SPREAD_PENALTY` exactly: one BoolVar per active (day, line, key), penalised.
- **Weight order (sequencer objective):** included orders ×100 000 > positie bonus 1 000 >
  **identical adjacency 400 per arc** > **kapvorm adjacency 60 per arc** > idle minutes ×1
  ≈ setup minutes ×1. All new weights env-overridable (`FRIGI_SEQ_*`), like
  `FRIGI_SEQUENCE_TIME_LIMIT`.
- **One score for "better day".** Today `_maybe_recover_drops` compares ombouw and
  `_pair_quality` compares (orders, −ombouw). Both must use a new
  `src/day_quality.py::line_score`, computed with the SAME weights as the sequencer, or the
  builder will throw away the sequencer's grouped/compacted proposals.
- **Sate handover lives in the final re-align block of `schedule_day`** (after S2), because
  that is the only point both lines' final timelines are known. `_align_sate_tail` gains an
  `allow_earlier` flag; it may move the block earlier but never before the order that
  precedes it on its own line, and never onto a machine the other line holds.
- **Proxy buffer is a measured constant**, not a new model. The memory of earlier tuning
  (90→60 gained +8 orders) says lowering it is the right direction; the feedback loop's
  no-good cuts absorb the extra DAY-BUILD-DROPs. Pick by data in Step 17.

## Interfaces and Models

```python
# src/constraints/grouping_rules.py
def parse_identiek_refs(text: str) -> set[str]:
    """All 8-digit article numbers in a column-15 cell. '63319800 - 63963500 ' -> {..}.
    7-digit typos ('6392900') and junk ('f', '0', ' ') yield nothing."""

def identiek_groepen(refs_by_art: dict[str, set[str]]) -> dict[str, str]:
    """Union-find over art_nr -> referenced art_nrs (references to unknown art_nrs are
    ignored). Returns art_nr -> group id (the smallest art_nr in the component) for
    components of size >= 2 only."""

def kapvorm_sleutel(product: Product) -> str | None:
    """Normalised kapvorm (strip, upper) or None for '', '-', 'NVT'."""

# src/models.py  (Product)
identiek_groep: str | None = None

# src/day_quality.py
IDENTIEK_BONUS: int   # re-exported from day_sequencer weights
KAPVORM_BONUS: int
IDLE_WEIGHT: int
def idle_minutes(planned: list[PlannedOrder], lijn_nr: int) -> int:
    """Minutes between line start and the last order's end not spent producing or
    changing over: (first.start - ombouw_0 - line_start) + sum of gaps
    (start_i - ombouw_i - eind_{i-1})."""
def adjacency_counts(planned: list[PlannedOrder]) -> tuple[int, int]:
    """(identical pairs back to back, smaller-or-equal first; same-kapvorm pairs back to back)."""
def line_score(planned: list[PlannedOrder], lijn_nr: int) -> int:
    """IDENTIEK_BONUS*ident + KAPVORM_BONUS*kap - IDLE_WEIGHT*idle - total ombouw."""
def line_quality(planned, lijn_nr) -> tuple[int, int]:
    """(len(planned), line_score) — compare with > ."""

# src/layout_metrics.py
def line_day_metrics(planned: list[PlannedOrder], datum: date, lijn_nr: int) -> dict:
    """{'idle_min', 'end_gap_min' (shift_end - last eind, 0 if empty), 'orders'}"""
def plan_layout_summary(day_schedules: list[DaySchedule], until: date | None) -> dict:
    """Totals over line-days up to `until`: idle_min, end_gap_min, active_line_days,
    identiek_groups_split (groups with orders on >1 (day,line)),
    identiek_not_adjacent, kapvorm_breaks (same-kapvorm orders on one line-day that are
    not contiguous)."""
```

## Plan

- [x] **Step 1: Read the product database by header, not by column number**
      **Files:** `src/product_setup_loader.py` (modify)
      **What:** Replace the fixed indices (`DAY_COLUMNS`, `ALLERGEN_COLUMNS`,
      `TOESTEL_COLUMNS`, and every literal `_cell(row, N)` in `_product_from_row` /
      `_grouped_setup_from_row`) with a column map resolved from the sheet's two header rows.
      Add `_resolve_columns(section_row: tuple, name_row: tuple) -> dict[str, int]`:
      forward-fill row 1 (section headers like `Prio Lijn`, `Spiraal `, `Planning`,
      `Toestellen Keuken`, `Toestellen Assemblage`, `Kartonnage`, `Kruiscontaminatie…`),
      normalise both texts (lower, strip, collapse whitespace, drop a trailing `(…)` count
      such as `Gernal (2)`), and map logical keys to 1-based indices. Logical keys:
      `art_nr, naam, schaal, kapvorm, karton, flowpack, allergeen, assemblage_l1,
      assemblage_l2, kartonnage, keuken, snelheid_l1, snelheid_l2, opmerking,
      identiek, prio_l1, prio_l2, prio_cfs, spiraal_l1, spiraal_l2, ma, di, wo, do, vr,
      mep_d1, mep_dplus, opstart_moet, opstart_kan_niet, afsluit_moet, afsluit_kan_niet,
      vis_verzagen, oude_kliklok`, one per toestel in `TOESTEL_COLUMNS`' keys, one per
      allergen in `ALLERGEN_COLUMNS`' keys. `L1`/`L2` are disambiguated by section
      (`prio lijn` vs `spiraal`). `oude_kliklok` matches name starting with `oude kliklok`.
      Missing key → `ValueError` naming the key and the headers seen (fail loudly, like
      the missing-sheet error already does). Read rows 1–2 in `_load_products_from_workbook`,
      pass the map into `_product_from_row(row, row_idx, cols)` and replace each
      `_cell(row, N)` with `_cell(row, cols["key"])`; the `range(21, 26)` / `range(28, 32)`
      provenance checks become loops over the 5 day keys / 4 position keys. Keep the
      `*_COLUMNS` dicts' KEY sets (they define the output JSON) but drop their numbers
      (turn them into tuples of key names).
      **Verify:** `py -I -c "import sys; sys.path.insert(0,'.'); from src.product_setup_loader import load_product_setup_from_excel as L; p=L('data/source_data/Planning info.xlsx'); x=[q for q in p['products'] if q['product_informatie']['product_nummer']=='64224100'][0]; print(x['kruiscontaminatie']['allergenen'])"`
      prints `gluten: aanwezig, lupine: kruiscontaminatie, melk: aanwezig, mosterd: kruiscontaminatie, soja: kruiscontaminatie, vis: niet_aanwezig` (the pre-shift values),
      and `py -m pytest -q tests/test_loader.py` passes.

- [x] **Step 2: Test the header lookup survives an inserted column**
      **Files:** `tests/test_product_setup_columns.py` (create)
      **What:** Build two tiny in-memory workbooks with openpyxl (sheet `product database`,
      header rows 1–2, one product row): one in the old layout, one with an extra column
      `Kliklok L2` inserted before `Hand nipak`. Save to `tmp_path`, load both with
      `load_product_setup_from_excel`, assert the product dicts are identical except
      nothing. Second test: a workbook missing the `Vis` header raises `ValueError`
      mentioning `vis`. Keep the header text exactly as in the real sheet (see the
      header list in Step 1).
      **Verify:** `py -m pytest -q tests/test_product_setup_columns.py` passes.

- [x] **Step 3: Two Gernals and two Leonards**
      **Files:** `data/capacity.json`, `src/product_setup_loader.py` (modify)
      **What:** Set `toestellen_beschikbaarheid.gernal` and `.leonard` to `2` in
      `data/capacity.json` (this is the source `create_plan.py` uses — "capacity.json wins
      over products.json"). Update the `_beschrijving`/`_opmerkingen` text: add
      `"gernal": "2 stuks (planner 02/10)", "leonard": "2 stuks (planner 02/10)"`. Set the
      same two values in `TOESTELLEN_BESCHIKBAARHEID` in `src/product_setup_loader.py` so
      the two sources agree.
      **Verify:** `py -c "from src import config; a=config.device_availability(); print(a['gernal'], a['leonard'])"` prints `2 2`; `py -m pytest -q` still passes (if a test hard-codes gernal as scarce, give that test its own availability dict `{"gernal": 1, ...}` instead of changing the assertion, and note it here).
      **Note:** `tests/test_config.py::test_device_availability_excludes_comment_keys` asserted the REAL config's leonard == 1; it now asserts multifill == 1 (still scarce) and gernal/leonard == 2.

- [x] **Step 4: Grouping rules module**
      **Files:** `src/constraints/grouping_rules.py` (create), `src/constraints/__init__.py` (modify)
      **What:** Implement `parse_identiek_refs`, `identiek_groepen`, `kapvorm_sleutel`
      exactly as in *Interfaces and Models* (regex `r"\b\d{8}\b"`; union-find with path
      compression; group id = `min(component)`). Module docstring in the style of
      `src/constraints/line_rules.py` ("grouping_rules.py — G1, G2. …"), explaining that
      column 15 is a link list, links are symmetric but the sheet does not always state
      both directions (`63963500` lists only two of its three partners), hence union-find.
      Export the three names from `src/constraints/__init__.py` like the other rules.
      **Verify:** `py -c "from src.constraints.grouping_rules import parse_identiek_refs as p; print(p(\"'63319800 - 63963500 '\"), p('6392900'), p('f'))"` prints `{'63319800', '63963500'} set() set()`.

- [x] **Step 5: Carry the group onto Product**
      **Files:** `src/models.py`, `src/loader.py` (modify)
      **What:** Add `identiek_groep: str | None = None` to `Product` next to the existing
      `identiek_product` field (G1 block). In `src/loader.py::_load_grouped_product` pass
      `identiek_product=info.get("identiek_product", "")`. At the end of `load_products`,
      after the `for p in data["products"]` loop: build
      `refs = {a: parse_identiek_refs(p.identiek_product) for a, p in products.items()}`,
      `groups = identiek_groepen(refs)`, and `products[a] = replace(products[a], identiek_groep=g)`
      for each `a, g in groups.items()`.
      **Verify:** `py -c "from src.loader import load_products; P,_=load_products('data/generated/products.json'); print(P['63901900'].identiek_groep, P['63931900'].identiek_groep, P['67110500'].identiek_groep)"` — regenerate products.json first with `py -m src.product_setup_loader` if its CLI writes `data/generated/products.json` (check `main()`); expect `63901900 63901900 67110100`.

- [x] **Step 6: Tests for grouping rules**
      **Files:** `tests/test_grouping_rules.py` (create), `tests/conftest.py` (modify)
      **What:** Add `identiek_groep: str | None = None` keyword to `make_product` in
      `tests/conftest.py` and pass it through. Tests: pair links both ways → one group;
      triple where one member lists only two partners → one group of 3; reference to an
      unknown art_nr is ignored (no singleton group); 7-digit typo ignored; `kapvorm_sleutel`
      returns None for `NVT`, `nvt `, `-`, `''` and `H22` for ` h22`.
      **Verify:** `py -m pytest -q tests/test_grouping_rules.py` passes.

- [x] **Step 7: Day sort — identical blocks first, then kapvorm**
      **Files:** `src/constraints/sequence_rules.py` (modify)
      **What:** Rewrite `sort_orders_for_lijn`:
      1. Blocks: orders with the same `product.identiek_groep` (not None) form one block,
         members sorted `aantal` ascending (smallest first — planner rule). Every other
         order is a block of one.
      2. Block kapvorm key = `kapvorm_sleutel(first member)`, or the single bucket
         `"__geen__"` when None (this is today's `NVT` bucket — on L2 it is everything,
         so the layer "falls through").
      3. Kapvorm groups sorted exactly as today: `(max allergie_niveau, -sum aantal)`.
      4. Inside a group, blocks sorted by `(max allergie_niveau in block, -total aantal,
         min fte_base)` — for singleton blocks this equals today's key.
      5. Flatten, then `apply_positie_regels` as today.
      Update the docstring (it is Dutch — keep it Dutch) to describe the two layers.
      **Verify:** `py -m pytest -q tests/test_positie_regels.py tests/test_day_build.py` passes.

- [x] **Step 8: Tests for the day sort**
      **Files:** `tests/test_grouping_rules.py` (modify)
      **What:** Using `make_product`/`make_order` from conftest and the `_names` helper
      pattern from `tests/test_positie_regels.py`: (a) two identical products with a
      third product between them in input order end up adjacent, smaller order first;
      (b) two `H22` products separated by an `H25` in input end up adjacent;
      (c) an identical pair with different kapvorm stays adjacent (identical beats kapvorm);
      (d) all-`NVT` orders keep the pure allergen-ascending order of today.
      **Verify:** `py -m pytest -q tests/test_grouping_rules.py` passes.

- [x] **Step 9: Sequencer — grouping bonus and idle term**
      **Files:** `src/day_sequencer.py` (modify)
      **What:** Add module constants next to `_POSITIE_BONUS`:
      `IDENTIEK_BONUS = int(os.environ.get("FRIGI_SEQ_IDENTIEK_BONUS", "400"))`,
      `KAPVORM_BONUS = int(os.environ.get("FRIGI_SEQ_KAPVORM_BONUS", "60"))`,
      `IDLE_WEIGHT = int(os.environ.get("FRIGI_SEQ_IDLE_WEIGHT", "1"))`, each with a comment
      stating its place in the weight order (see Architecture Decisions).
      In `solve_optimal_sequence`:
      - `adj_bonus` = Σ over `order_arc[(i, j)]`: `IDENTIEK_BONUS` if both products share a
        non-None `identiek_groep` and `aantal_i <= aantal_j`; plus `KAPVORM_BONUS` if
        `kapvorm_sleutel` equal and not None.
      - `day_end = model.NewIntVar(start_time_min, max_end_min, "day_end")`;
        `model.Add(day_end >= finish[i]).OnlyEnforceIf(skip[i].Not())` for all i.
      - `idle = day_end - start_time_min - Σ durations[i]*(1 - skip[i]) - total_cost`
        (linear expression; it is ≥ 0 by construction).
      - Objective: `sum(included)*100_000 + positie_bonus*_POSITIE_BONUS + adj_bonus
        - total_cost - IDLE_WEIGHT*idle`. Update the comment above `model.Maximize`.
      Update the module docstring to mention grouping and compaction.
      **Verify:** `py -m pytest -q tests/test_day_build.py tests/test_sate_exclusive.py tests/test_positie_regels.py` passes.

- [x] **Step 10: Shared day-quality score**
      **Files:** `src/day_quality.py` (create), `src/scheduler.py` (modify)
      **What:** Implement the `src/day_quality.py` interface (imports the three weights from
      `src.day_sequencer`, `kapvorm_sleutel` from grouping_rules, `get_earliest_start` from
      `src.constraints.calendar_rules`). In `src/scheduler.py`:
      - `_maybe_recover_drops`: replace the `better = …` expression with
        `better = line_quality(retried, lijn_nr) > line_quality(greedy_result, lijn_nr)`
        (more orders still wins first; with equal count the higher score wins — this
        subsumes the old "less ombouw" rule). Keep the `force` gate for running the solver
        at all. Update the docstring's "Replaces the greedy result if…" paragraph.
      - `_pair_quality`: return `(n_orders, line_score(L1) + line_score(L2))` and update its
        docstring (tie still keeps L1 first because comparison is strict `>`).
      **Verify:** `py -m pytest -q` passes.

- [x] **Step 11: Tests for compaction and grouping in the built day**
      **Files:** `tests/test_day_quality.py` (create)
      **What:** Follow `tests/test_day_build.py` fixtures. (a) `idle_minutes` on a
      hand-built list of `PlannedOrder` with one 60-min gap returns 60. (b) Call
      `solve_optimal_sequence` for an L2 day with three orders where one needs `gernal` and
      `blocking={"gernal": [(10*60+30, 21*60+50)]}`, `toestellen_avail={"gernal": 1}`,
      and that order has the highest allergen level: assert the gernal order is placed
      before 10:30, not after 21:50 (the 2026-09-28 case). (c) Two identical products +
      one other on the same line: `schedule_day(..., force_optimal_sequence=True)` returns
      them adjacent.
      **Verify:** `py -m pytest -q tests/test_day_quality.py` passes.

- [x] **Step 12: `_align_sate_tail` may move a block earlier**
      **Files:** `src/scheduler.py` (modify)
      **What:** Add keyword `allow_earlier: bool = False` to `_align_sate_tail`. When True,
      a negative shift is allowed but clamped so the block's window start
      (`planned[i].start - planned[i].ombouw_voor`) is never before
      `planned[i-1].eind` (or the line start from `get_earliest_start(lijn)` when `i == 0`).
      Keep the device check (refuse the move if `stagger_around` would move any block
      order). Warning text for an earlier move: `f"Sate vervroegd naar {start:%H:%M} — start direct na de andere lijn"`.
      Default behaviour (allow_earlier=False) must stay byte-identical.
      **Verify:** `py -m pytest -q tests/test_sate_exclusive.py` passes unchanged.

- [x] **Step 13: Sate handover in `schedule_day`**
      **Files:** `src/scheduler.py` (modify)
      **What:** Add `_handover_target(closer: list[PlannedOrder], other: list[PlannedOrder], max_end: time) -> time`:
      `i = sate_tail_index(closer)`; if None → `max_end`. `window_len = minutes(closer[-1].eind) - (minutes(closer[i].start) - closer[i].ombouw_voor)`.
      `other_end` = latest `eind` on `other` (if `other` is empty → return the block's
      current end: nothing to protect, so no slide). Return
      `min(max_end, other_end + window_len)` as a time.
      In `schedule_day`'s final A5 re-align block (the one after `_enforce_positie_regels`,
      commented "Put the queue back"): when `sate_window(by_line[other]) is None` (only the
      closer has sate), replace `_align_sate_tail(by_line[closer], max_end, …)` with
      `_align_sate_tail(by_line[closer], _handover_target(by_line[closer], by_line[other], max_end), …, allow_earlier=True)`.
      Two-sate days keep the existing code path untouched. Update the `A5` comment block
      above `_SATE_EXCLUSIVE` ("Which is why the sate block is slid to closing time…") to
      say: slid as late as needed for the other line to finish, and no later.
      **Verify:** `py -m pytest -q tests/test_sate_exclusive.py` — `test_sate_runs_against_the_shift_end`
      is EXPECTED to fail now (it asserts the old behaviour: sate on L1 ends at shift end
      while L2 has only one small order). Proceed to Step 14.
      **Note:** `test_two_sate_orders_on_one_line_stay_one_block` also fails for the same reason (asserts the block ends at shift end with only a short L2 order) — rewrite it in Step 14 too.

- [x] **Step 14: Update and extend the sate tests**
      **Files:** `tests/test_sate_exclusive.py` (modify)
      **What:** Rename/rewrite `test_sate_runs_against_the_shift_end` →
      `test_sate_starts_when_the_other_line_stops`: assert the sate window opens exactly at
      the other line's last `eind` and nothing overlaps (`check_sate_conflict` empty). Add
      `test_sate_still_closes_the_day_when_the_other_line_runs_long`: other line has work
      until past where a closing-time sate would open → sate ends at `config.shift_end`.
      Add `test_handover_never_moves_sate_before_its_own_predecessor`.
      **Note:** observed while writing the long-day test: when the other line's orders are coarse (e.g. 110 min each), it stops well before the cut-off and the handover pulls the sate earlier by that slack — both lines then finish early instead of the other line idling. Intended (the 'seamless handover' choice).
      **Verify:** `py -m pytest -q tests/test_sate_exclusive.py` passes.

- [x] **Step 15: Assignment model — identical and kapvorm terms**
      **Files:** `src/scheduler_ortools_v3.py` (modify)
      **What:** Next to `_FORMAAT_SPREAD_PENALTY` add
      `_IDENTIEK_SPLIT_PENALTY = int(_os.environ.get("FRIGI_IDENTIEK_SPLIT_PENALTY", "40"))`
      and `_KAPVORM_SPREAD_PENALTY = int(_os.environ.get("FRIGI_KAPVORM_SPREAD_PENALTY", "3"))`
      with comments in the style of the `_OFF_PRIO_LINE_PENALTY` block (why, and where each
      sits in the weight order; cite the 02/10 meeting). Next to `order_formaat` (built
      around the `order_formaat[o_idx] = schaal_formaat(product.schaal)` line) build
      `order_ident[o_idx] = product.identiek_groep` and
      `order_kap[o_idx] = kapvorm_sleutel(product)`; then set either to None when fewer
      than 2 `valid_orders` share that key (no variables for singletons). In
      `build_and_solve`, add `presence_by_line_ident` and `presence_by_line_kap`
      (`defaultdict(list)` keyed `(di, lijn, key)`) and append `p` to them in BOTH the
      single-chunk and multi-chunk branches, right where `presence_by_line_formaat` is
      appended (skip when key is None). After `formaat_active`, create `ident_active` and
      `kap_active` BoolVars with `AddMaxEquality` exactly like `formaat_active`, and append
      `ia * _IDENTIEK_SPLIT_PENALTY * efficiency_weight` / `ka * _KAPVORM_SPREAD_PENALTY * efficiency_weight`
      to `penalties` right after the `formaat_active` penalty loop.
      **Verify:** `py -m pytest -q tests/test_optimizer_objective.py tests/test_feedback_optimizer.py` passes.

- [x] **Step 16: Test the assignment terms**
      **Files:** `tests/test_optimizer_objective.py` (modify)
      **What:** Following `test_setup_efficiency_precedes_delivery_proximity` in that file:
      (a) two identical-group orders with delivery dates 3 workdays apart, both fitting on
      one day → assigned to the same (day, line); (b) two same-kapvorm (`H22`) L1 orders
      plus enough room → same day; (c) an identical pair where one product is not allowed on
      the other's only legal weekday → both still scheduled (split), none rejected.
      **Verify:** `py -m pytest -q tests/test_optimizer_objective.py` passes.

- [x] **Step 17: Layout metrics in every run**
      **Files:** `src/layout_metrics.py` (create), `create_plan.py` (modify), `tests/test_layout_metrics.py` (create)
      **What:** Implement `line_day_metrics` and `plan_layout_summary` per the interface
      (reuse `day_quality.idle_minutes`; `config.shift_end` for end gap). In `create_plan.py`
      right after the `totals.report_imbalance` loop, compute the summary over the day
      schedules up to `commit_end` and print one line:
      `[LAYOUT] commit-venster: idle {..} min, eind-gap {..} min, {..} lijn-dagen, identiek gesplitst {..}, identiek niet aansluitend {..}, kapvorm-breuken {..}`.
      Also store the dict under `stats["layout"]` so it reaches chart_data JSON (no template
      change in this blueprint). Test: two hand-built line-days → expected numbers.
      **Verify:** `py -m pytest -q tests/test_layout_metrics.py` passes and
      `py create_plan.py --week 45 --year 2026 --dry-run --reset` prints a `[LAYOUT]` line.

- [x] **Step 18: Measure and choose the proxy buffer**
      **Files:** `src/scheduler_ortools_v3.py` (modify the `_PROXY_BUFFER_MIN` default + comment), this blueprint (add results)
      **What:** For `FRIGI_PROXY_BUFFER` in 60, 40, 30, run TWICE each:
      `$env:FRIGI_PROXY_BUFFER='40'; py create_plan.py --week 45 --year 2026 --dry-run --reset`
      (PowerShell; clear the var afterwards). Record per run: scheduled/rejected orders,
      rejected units, DAY-BUILD-DROP count, and the `[LAYOUT]` idle + end-gap. Write the
      table under this step. Decision rule: pick the LOWEST buffer whose real rejections
      (excluding "Artikel niet in de productdatabase") are within the 60-arm's range; if
      none qualifies keep 60 and say so. Set it as the default and replace the comment on
      `_PROXY_BUFFER_MIN` with the measured table (style: the `_PLANNING_RESERVE` comment in
      `src/config.py`).
      **Verify:** table present in this file; `py -m pytest -q` passes (benchmark tests may
      have fixture expectations — if `tests/test_optimizer_benchmark.py` fails purely on a
      changed number, note it here rather than editing the fixture).

- [x] **Step 19: Registry and generated constraints doc**
      **Files:** `src/constraints/registry.py` (modify), `docs/constraints.md` (regenerate)
      **What:** G1 → enforced: severity SOFT, point `FILTER` (`src.scheduler_ortools_v3.assign_production_dates_feedback`),
      data source `Planning info.xlsx col 'identiek product' -> grouping_rules.identiek_groepen`,
      description covering assignment penalty + sort blocks + sequencer adjacency. Add
      `G2` "Same kapvorm on the same day/line, back to back" (SOFT, FILTER). Update `S1`
      text (identical blocks then kapvorm), `O4` (idle term), `A5` (handover), `M1` note
      (gernal/leonard now 2 → never staggered), and add `O6` "No idle gaps inside a line-day"
      (SOFT, BUILD, `src.day_sequencer.solve_optimal_sequence`). Regenerate:
      `py -m src.constraints.registry`.
      **Verify:** `py -m pytest -q tests/test_registry.py` passes and `git diff docs/constraints.md` shows G1 under enforced rules.

- [x] **Step 20: Explanation files**
      **Files:** `HOW_IT_WORKS.md`, `ARCHITECTURE.md` (modify)
      **What:** Search both for the sate "closing time"/"slid" explanation, the kapvorm
      sorting description and the proxy buffer value; update them to the new behaviour in
      the existing tone. No new sections beyond a short "Groepering" paragraph.
      **Verify:** `rg -n "60 min|closing time|sluitingstijd|kapvorm" HOW_IT_WORKS.md ARCHITECTURE.md` shows no stale statements.

- [x] **Step 21: End-to-end check against the planner's examples**
      **Files:** this blueprint (add results)
      **What:** `py create_plan.py --week 45 --year 2026 --dry-run --reset` twice. In the
      resulting `output/planning_wk40.xlsx` (dry-run output path — check what `--dry-run`
      writes) inspect: 2026-10-02 and 2026-09-30 (sate window opens at the other line's last
      end), 2026-09-28 L2 (no order starting after 21:00 unless the line is full), and that
      the bami pair 63901900/63931900 and DMB pairs (e.g. 65107600/65107700) share a day+line
      when both are in the horizon. Record the `[LAYOUT]` lines of both runs next to the
      Step 18 baseline. Dry-run must not modify `data/generated/plan_state.json`
      (`git diff --stat data/generated/plan_state.json` empty).
      **Verify:** findings written under this step; full `py -m pytest -q` green.
      **Findings (week 45, buffer 30, `--dry-run --reset --warmup 0`):**
      - Sate handover works on all 7 sate days of the committed window: the sate window opens at the other line's last order end, or right after the sate line's own predecessor + its changeover (40 min Leonard→Leonard). No dead gap between lines.
      - Holes > 40 min inside a line-day: one (45 min, Nov 12 L1) — the sate's own 40-min changeover after waiting 5 min for L2. Idle 25–115 min over 40 line-days (was 275–405 at the old code default).
      - 2 Gernals / 2 Leonards: neither is staggered any more (scarce list printed by create_plan no longer contains them).
      - Identical groups: 3 of 9 together in the committed window; split groups are never adjacent-broken inside a day (`identiek niet aansluitend 0`). The other 6 stay split. Investigated: the soft-objective stage ends ~3x above its bound, and more time made it worse; a regrouped hint made it return nothing. Added `_regroup_identical` (exact neighbourhood: only the group's members free, full model) — it improves the open-tail objective but nothing in the committed window, because there the joins are infeasible: a line-day already full in proxy minutes, or a twin the iteration left unplaced (completeness lock). Remaining splits are capacity / loop dynamics, not a missing rule. Same-article repeat orders (e.g. 64255000 ×3, 26,810 each) count as a "group" too and genuinely cannot share one day.
      - `plan_state.json` untouched by all dry-runs.

## Tests

New: `tests/test_product_setup_columns.py`, `tests/test_grouping_rules.py`,
`tests/test_day_quality.py`, `tests/test_layout_metrics.py`. Extended:
`tests/test_sate_exclusive.py` (one test intentionally rewritten — old behaviour),
`tests/test_optimizer_objective.py`. Fixture helper: `make_product(..., identiek_groep=...)`
in `tests/conftest.py`. Run everything with `py -m pytest -q`.

## Verification

1. `py -m pytest -q` — green.
2. `py create_plan.py --week 45 --year 2026 --dry-run --reset` ×2: `[LAYOUT]` idle and
   end-gap clearly below the Step 18 buffer-60 baseline, identical groups split lower,
   rejections inside the baseline range.
3. Planner examples (Step 21): no sate dead gap between lines; identical pairs together;
   same kapvorm contiguous on L1; L2 compacted (no multi-hour holes caused by a gernal
   order waiting for L1 — and with 2 gernals that stagger is gone entirely).
4. `config.device_availability()` reports gernal 2, leonard 2.
