---
name: explainability-report
description: "Generates a self-contained HTML explainability report for the Frigilunch planning system. Reads the actual source code to extract current rules, constraints, weights, and pipeline stages — no documentation is trusted as source of truth. Run this whenever the codebase changes (new constraints, changed weights, new stages) or when a client-facing explainability document is needed. Output: documentation/explainability_report.html"
---

# Explainability Report Generator

Produces `documentation/explainability_report.html` — a single-file, self-contained HTML document explaining the entire Frigilunch planning pipeline to a client audience. Every fact in the report must be grounded in the current source code, not in prior docs or memory.

---

## Phase 1: Extract (read source, no agents needed for small scope)

Read these files in order. For each, extract the specific data listed below.

### 1. `src/config.py`
Extract: all shift start/end times, planning reserve values, line capacity calculations, any global numeric constants.

### 2. `data/setup_times.json`
Extract: all changeover times (format changes, kapvorm, machine floors, allergen cleaning, CFS transition, Daesang exception).

### 3. `data/capacity.json`
Extract: line capacities, staffing config (`max_bezetting_delta`), `production_timing` block (min_days, grace_days, feedback_timeout), device availability counts.

### 4. `src/loader.py`
Extract: FTE complexity table, speed derivation logic, line exclusion derivation (CFS/spiral → `l1_uitgesloten`/`alleen_l2`).

### 5. `src/order_filter.py`
Extract: the three pre-flight filter functions and their rejection reason strings.

### 6. `src/validator.py`
Extract: all validation checks, rejection categories, sate warning gap.

### 7. `src/constraints/slots.py`
Extract: all legal-slot constraints (C3, C4, C5, D1, L1/L2), with exact logic and empty-slot rejection reasons.

### 8. `src/scheduler_ortools_v3.py`
**Do NOT read this file in full** — it is ~2,500 lines and will exhaust the context budget before Phase 2.
Instead use Grep to extract only the needed sections:
- Run `Grep pattern="_PENALTY|_WEIGHT|_MAX_ITERS|_FAR_|_TWO_PHASE|_RETRY|num_search_workers|FRIGI_" path="src/scheduler_ortools_v3.py" output_mode="content"` to get all constants and env-var names with their defaults.
- Read lines 1–120 only (for the module docstring and the first block of constants).
- The objective function structure, phase dispatch logic, and capacity constraint formulas can be inferred from the constant values alone — do not read further.

### 9. `src/scheduler.py`
Extract: all BUILD constraints — shift window enforcement, changeover calculation (how MAX is applied), allergen ascending rule, position rules (S2), sequence sort logic (S1), staffing ramp (P1), device stagger (M1). Quote exact function names and their logic.

### 10. `src/constraints/allergen_rules.py`
Extract: allergen level mapping (symbol → integer), sate threshold, cross-contamination logic.

### 11. `src/constraints/changeover.py`
Extract: the changeover matrix lookup and MAX aggregation.

### 12. `src/constraints/sequence_rules.py`
Extract: all sequence constraints and their enforcement points.

### 13. `src/day_sequencer.py`
Extract: TSP formulation, time limit, positie bonus value, kapvorm penalty value, staffing arc pruning.

### 14. `src/campaign_regroup.py`
Extract: campaign grouping logic, move/swap criteria.

### 15. `src/plan_state.py`
Extract: `COMMIT_WEEKS`, frozen boundary logic, tiered horizon split, backfill pass.

### 16. `src/plan_score.py`
Extract: all fields of `PlanScore` in order, with descriptions.

### 17. `src/constraints/registry.py`
Extract: all constraints listed as `point=NONE` or otherwise not enforced. These go in the "not yet enforced" section.

---

## Phase 2: Generate main HTML report

**Do NOT invoke the `diagram-design` skill.** Write both SVGs inline directly in the HTML file — spawning sub-agents to generate diagrams puts their output back into context and triggers the API streaming limit before the Write call completes.

### Diagram A — Pipeline Overview (write inline SVG)

A left-to-right flow, two rows of five nodes. Use SVG `<rect>`, `<text>`, and `<line>` with open arrowheads (`<marker>`). No `filter: drop-shadow`. Exact spec:

**Row 1 (y=20):** Data Ingestion → Pre-flight Filter → Plan Lock → FILTER Constraints → CP-SAT Optimizer  
**Row 2 (y=120):** Oracle Day Builder → Day Sequencer → Post-solve Regroup → Plan Scoring → Commit & Export  
**Vertical arrow** from CP-SAT Optimizer down to Oracle Day Builder (feedback start).  
**Horizontal arrow** from Oracle Day Builder left-to-right across row 2 back toward CP-SAT Optimizer (draw as a return arc labelled "no-good cut / done").

Node fills by layer:
- Ingestion: `#dcfce7` fill, `#166534` text
- Filter (Pre-flight Filter, Plan Lock, FILTER Constraints): `#fef9c3` fill, `#854d0e` text
- Solve (CP-SAT Optimizer, Oracle Day Builder, Day Sequencer, Post-solve Regroup): `#dbeafe` fill, `#1e40af` text — CP-SAT node gets `stroke:#2563eb stroke-width:2`
- Output (Plan Scoring, Commit & Export): `#f3f4f6` fill, `#374151` text

Node size: 120×40. Gap between nodes: 20. Total SVG: `viewBox="0 0 720 200"`.

### Diagram B — CP-SAT Feedback Loop (write inline SVG)

A minimal sequence diagram, vertical reading direction. Use SVG `<line>`, `<text>`, `<polygon>` (arrowheads), `<rect>` (activation boxes). No `filter`. Exact spec:

Two lifelines: "CP-SAT Model" (x=120) and "Oracle" (x=480). Lifelines as vertical `<line>` from y=60 to y=360.  
Activation box on CP-SAT lifeline: `<rect x=112 y=60 width=16 height=200 fill="#dbeafe" stroke="#2563eb"/>`.

Messages (horizontal arrows with label above):
1. y=100 — CP-SAT → Oracle: "Assignment proposal" (right-pointing arrow)
2. y=160 — Oracle → CP-SAT: "Orders dropped?" (left-pointing arrow)
3. y=220 — CP-SAT self-loop (arc right of lifeline, x=136 to x=180, arc back): label "Add no-good cut", stroke `#2563eb`
4. y=300 — Oracle → CP-SAT: "Done (or budget exhausted)" (left-pointing arrow)

Alt box: `<rect>` around y=190–250, dashed stroke `#6b7280`, label "[alt: yes]` at top-left corner.  
Total SVG: `viewBox="0 0 600 380"`.

---

## Phase 3: Write the HTML file in four sequential steps

**Do NOT write the entire file in one Write call** — the response will exceed the API streaming limit every time.
Instead, write the file as a skeleton first, then fill it in with three Edit calls. Each call must stay under ~4 000 characters of generated content.

### Design system (applies to all four steps)
- Fonts: `@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500&family=Geist+Mono&display=swap')`
- Page background `#f3f4f6`; card background `#ffffff`; text `#111827`; muted `#6b7280`
- Accent (CP-SAT heading border only): `#2563eb`
- Status badges: enforced `#166534`/`#dcfce7`, not-enforced `#854d0e`/`#fef9c3`, configurable `#1e40af`/`#dbeafe`
- Sidebar: fixed 220 px left; `margin-left:240px` on main
- Cards: `border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,.08)`; coloured `4px solid` left border
- Tables: `border-collapse:collapse`; header `#f9fafb`; alternating rows
- Code: `font-family:Geist Mono; background:#f9fafb; border-radius:4px; padding:2px 6px`
- Print: `@media print{.sidebar{display:none}main{margin:0}}`

---

### Step 3A — Write skeleton (Write tool, new file)

Write `documentation/explainability_report.html` containing:
- Full `<!DOCTYPE html>` + `<head>` with all CSS (full design system above, sidebar, cards, tables, badges, code)
- `<nav class="sidebar">` with all anchor links grouped: Overview · Pipeline (pipeline, rolling-lock) · Stages (stage-1 … stage-9) · Reference (constraints, constants, not-enforced)
- `<main>` containing:
  - `<header>`: system name "Frigilunch Production Planner" (Instrument Serif 24 px), subtitle "Explainability Report" (Geist 14 px muted), report date 2026-10-25, note "Based on source code read on 2026-10-25"
  - `<section id="overview">`: 2–3 sentences describing the system (two lines, rolling 4-week commit, Excel in → schedule out)
  - Four placeholder comments exactly as written:
    ```
    <!-- PART-B -->
    <!-- PART-C -->
    <!-- PART-D -->
    <!-- PART-E -->
    ```
- Closing `</main></body></html>`

---

### Step 3B — Edit: replace `<!-- PART-B -->` with pipeline + rolling-lock + stages 1–3

Replace the literal string `<!-- PART-B -->` with:

- **`#pipeline`** card: heading "Pipeline Overview", Diagram A SVG inline (from Phase 2 spec), caption.
- **`#rolling-lock`** card: heading "Rolling Commit Mechanic", small CSS-grid calendar strip (5 rows × 5 cols, no JS — inline `<style>` scoped to this section is fine):
  - Row 1: grey `#e5e7eb` "Completed"
  - Rows 2–5: blue `#1e40af` white text "Committed (locked)"
  - Rows 6–7: light blue `#dbeafe` "Open (re-solved each run)"
  Then prose: COMMIT_WEEKS=4 (from code); frozen days replayed byte-for-byte from plan_state.json; cold start = no frozen days.
- **`#stage-1`** card (green left border `#166534`): Data Ingestion — bullet about Planning info.xlsx regeneration; FTE complexity table (6 rows, exact values from loader.py); bullet about CFS/spiral → `l1_uitgesloten=True`.
- **`#stage-2`** card (amber left border `#854d0e`): Pre-flight Filtering — three sub-cards for filter_by_known_date, filter_by_delivery_range, data-quality; summary table Filter|Rejection reason|Effect.
- **`#stage-3`** card (amber left border): Plan Lock — COMMIT_WEEKS=4; tiered horizon table (Horizon|Budget s|Max iters: committed 240 s/15 iters, open-tail 90 s/5 iters); backfill pass prose.

---

### Step 3C — Edit: replace `<!-- PART-C -->` with stages 4–6

Replace the literal string `<!-- PART-C -->` with:

- **`#stage-4`** card (amber): FILTER Constraints — intro sentence; table ID|Constraint|Numeric detail for C3, C4, C5, D1, L1, L2; three styled rejection-reason badges: ARRIVED-AFTER-FREEZE (amber), WINDOW-PASSED (red), LINE-DAY-RESTRICTION (orange).
- **`#stage-5`** card (blue left border `#1e40af`, heading with `#2563eb` left border): CP-SAT Optimizer —
  - Diagram B SVG inline (from Phase 2 spec) with caption.
  - Phase dispatch: three numbered sub-cards (Phase 1 line-restricted / Phase 2 flexible / Phase 3 retry).
  - Objective: two side-by-side cards — Completeness (formula in `<code>`) and Efficiency (table Penalty|Weight|Trigger with all 9 terms and exact values from code).
  - Capacity constraints: four `<pre>` blocks (line cap, device cap, sate cross-line, no-good cut formulae).
  - Solver params table: Parameter|Value|Notes (8 workers, 10 s iter, 240 s committed, 90 s open-tail, 15/5 max iters).
  - Tunable flags table: Variable|Default|Category|Description — all FRIGI_* flags from scheduler_ortools_v3.py grouped Algorithm/Penalties/Budget.
- **`#stage-6`** card (blue): Oracle Day Builder —
  - Shift windows table: Line|Start|Normal end|Friday end|Normal cap (min)|Friday cap (min).
  - Changeover table with MAX rule heading; all rows from setup_times.json; CFS→non-CFS row in `<strong>`.
  - Allergen scale: inline SVG strip (7 segments, 50 px wide each, colour ramp white→amber→red, symbol above, level below).
  - Position rules table: opstart_moet / opstart_kan_niet / afsluit_moet / afsluit_kan_niet.
  - Sate exclusion card (amber border): A5 rule, allergie_niveau≥6, sate to end-of-day preference.
  - Staffing ramp card: max_bezetting_delta=3, exempt if duration>4 h.
  - Device contention card: M1 stagger logic.

---

### Step 3D — Edit: replace `<!-- PART-D -->` with stages 7–9

Replace the literal string `<!-- PART-D -->` with:

- **`#stage-7`** card (blue): Day Sequencer — TSP/circuit formulation; time limit 2.0 s; objective table (positie bonus 1 000, kapvorm penalty 50 general / 2 000 on L1); staffing arc pruning; re-verification note.
- **`#stage-8`** card (blue): Post-solve Regroup — campaign map description; move logic; swap fallback.
- **`#stage-9`** card (grey): Plan Scoring — table Priority|Field|Description (6 rows, lower=better); Priority 1 row `#dbeafe` background.

---

### Step 3E — Edit: replace `<!-- PART-E -->` with reference sections

Replace the literal string `<!-- PART-E -->` with:

- **`#constraints`** card: Full Constraint Reference — table ID|Description|Stage|Status for all 26 constraints from registry.py; status cells as coloured badges (enforced green, not-enforced amber, configurable blue).
- **`#constants`** card: Numeric Constants — table Parameter|Value|Unit|Source grouped by category (Shift windows / Changeover times / Solver weights / Solver budget / Staffing).
- **`#not-enforced`** card (amber left border, `#fef9c3` background): opening note; table ID|Rule|Reason for all 9 `point=NONE` entries from registry.py.

---

## Phase 4: Verify (renamed from old Phase 4)

After writing the file:
1. Check that every numeric value (weights, times, capacities) matches what you read from source in Phase 1. Do not use values from memory — use the actual values you read.
2. Confirm both SVG diagrams are embedded inline — no `<img src=...>` or `<object>` pointing to external files.
3. Confirm no external JS. Google Fonts `@import` is the only external dependency.
4. Confirm the file is self-contained and works if moved to another folder.
5. Report the output path and file size to the user.

---

## Notes for re-runs

When re-running after a code update:
- Always re-read the source files in Phase 1. Do not reuse values from a previous run.
- Rewrite both inline SVGs in Phase 2 if the pipeline structure changes (new stages, renamed nodes).
- If a constraint was added to registry.py, add it to the constraint reference table.
- If a penalty weight changed in scheduler_ortools_v3.py, update the objective function section.
- The report date in the header must reflect when the report was generated, not when the skill was written.
